"""Phase 6 end to end: signal, risk check, paper order, fill, portfolio, P&L.

The acceptance path of build spec §46, with one substitution: the market frames
are scripted rather than arriving from a socket. Everything downstream — the
normalisation, the quality flags, the live store, the gate, the fill engine, the
book, the API — is the production code.

What is deliberately *not* mocked is the fill. The paper broker reads the same
live store the API serves quotes from, so a fill in these tests happens against
a quote a user would have seen.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.instruments.service import InstrumentService
from domains.market_data.live import LiveMarketDataService
from domains.market_data.providers.upstox import UpstoxMarketDataProvider
from domains.market_data.streaming.feed import FeedEntry
from domains.market_data.streaming.live_state import LiveMarketStore
from domains.market_data.streaming.manager import MarketStreamManager, StreamOptions
from tests.conftest import register_and_login

NIFTY_KEY = "NSE_INDEX|Nifty 50"
RELIANCE_KEY = "NSE_EQ|INE002A01018"

MASTER_ROWS = [
    {
        "segment": "NSE_INDEX",
        "name": "Nifty 50",
        "exchange": "NSE",
        "instrument_type": "INDEX",
        "instrument_key": NIFTY_KEY,
        "trading_symbol": "NIFTY",
    },
    {
        "segment": "NSE_EQ",
        "name": "Reliance Industries",
        "exchange": "NSE",
        "instrument_type": "EQ",
        "instrument_key": RELIANCE_KEY,
        "trading_symbol": "RELIANCE",
    },
]


def quote_entry(
    key: str,
    timestamp: datetime,
    last: float,
    bid: float | None,
    ask: float | None,
    bid_size: int = 500,
    ask_size: int = 500,
) -> dict:
    depth: dict[str, list] = {"buy": [], "sell": []}
    if bid is not None:
        depth["buy"] = [{"quantity": bid_size, "price": bid, "orders": 3}]
    if ask is not None:
        depth["sell"] = [{"quantity": ask_size, "price": ask, "orders": 5}]
    return {
        "instrument_token": key,
        "symbol": key.split("|")[-1],
        "last_price": last,
        "volume": 128_400,
        "oi": 0,
        "timestamp": timestamp.isoformat(),
        "ohlc": {"open": last, "high": last, "low": last, "close": last},
        "depth": depth,
    }


class _NullDirectory:
    async def provider_key(self, instrument_id):
        return None

    async def instrument(self, instrument_id):
        return None

    async def by_provider_key(self, key):
        return None


async def _token() -> str:
    return "test-token"


@pytest.fixture
def live_settings(app_environment):
    from infrastructure.settings import get_settings, reset_settings_cache

    previous = os.environ.get("QIP_MARKET_DATA_PROVIDER")
    os.environ["QIP_MARKET_DATA_PROVIDER"] = "upstox"
    reset_settings_cache()
    yield get_settings()
    if previous is None:
        os.environ.pop("QIP_MARKET_DATA_PROVIDER", None)
    else:
        os.environ["QIP_MARKET_DATA_PROVIDER"] = previous
    reset_settings_cache()


@pytest.fixture
async def live_client(live_settings) -> AsyncIterator:
    import httpx

    from apps.api.main import create_app

    app = create_app(live_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver/api/v1"
    ) as client:
        yield client


@pytest.fixture
def store(live_settings) -> LiveMarketStore:
    from infrastructure.cache.client import get_cache

    return LiveMarketStore(get_cache(live_settings), live_settings.live_quote_ttl_seconds)


@pytest.fixture
async def market(db_session, live_settings, store):
    """The instrument master loaded, as the refresh job would leave it."""
    service = LiveMarketDataService(InstrumentService(db_session), store, source="upstox")
    await service.load_instrument_master(MASTER_ROWS)
    await db_session.commit()
    instruments = InstrumentService(db_session)
    return {
        "NIFTY": await instruments.find_by_alias("upstox", NIFTY_KEY),
        "RELIANCE": await instruments.find_by_alias("upstox", RELIANCE_KEY),
    }


async def publish(store: LiveMarketStore, instrument, key: str, entries: list[dict]) -> None:
    """Push provider frames through the real manager into the live store."""
    provider = UpstoxMarketDataProvider(
        directory=_NullDirectory(), token_source=_token, transport=None
    )
    manager = MarketStreamManager(
        transport=None,
        store=store,
        read_quote=provider.quote_from_entry,
        instrument_for_key=lambda seen: instrument if seen == key else None,
        options=StreamOptions(feed_name="upstox"),
    )
    manager.subscribe([(instrument, key)])
    for payload in entries:
        await manager._ingest([FeedEntry(provider_key=key, fields=payload)])
    await manager._write_health()


async def make_account(client, header, **overrides) -> dict:
    body = {
        "name": overrides.pop("name", f"paper-{uuid.uuid4().hex[:8]}"),
        "opening_cash": "1000000",
        **overrides,
    }
    response = await client.post("/trading/accounts", json=body, headers={"Authorization": header})
    assert response.status_code == 201, response.text
    return response.json()


async def submit(client, header, account_id: str, **body) -> dict:
    body.setdefault("order_type", "MARKET")
    response = await client.post(
        f"/trading/accounts/{account_id}/orders",
        json=body,
        headers={"Authorization": header},
    )
    return response


@pytest.fixture
async def ready(live_client, market, store):
    """A logged-in user, a funded paper account, and a live NIFTY quote."""
    _user, header = await register_and_login(live_client)
    account = await make_account(live_client, header)
    await publish(
        store,
        market["NIFTY"],
        NIFTY_KEY,
        [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0)],
    )
    return header, account, market


class TestTheAcceptancePath:
    """Signal, risk check, paper order, fill, portfolio, P&L."""

    async def test_an_order_fills_against_the_live_quote_and_reaches_the_pnl(
        self, ready, live_client
    ):
        header, account, market = ready
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="10",
        )
        assert response.status_code == 201, response.text
        body = response.json()
        order = body["results"]["order"]

        assert order["status"] == "FILLED"
        assert Decimal(order["filled_quantity"]) == 10
        # Paid the ask, not the mid and not the last trade.
        assert Decimal(order["average_fill_price"]) == Decimal("24501")
        assert order["fills"][0]["price_basis"] == "MARKET_ASK"

        pnl = await live_client.get(
            f"/trading/accounts/{account['id']}/pnl", headers={"Authorization": header}
        )
        assert pnl.status_code == 200, pnl.text
        book = pnl.json()["results"]
        assert len(book["positions"]) == 1
        held = book["positions"][0]
        assert Decimal(held["quantity"]) == 10
        assert Decimal(held["average_price"]) == Decimal("24501")
        # Bought at the ask and marked at the mid: down half the spread.
        assert Decimal(held["unrealised_pnl"]) == Decimal("-10")
        assert Decimal(book["cash"]) == Decimal("1000000") - Decimal("245010")

    async def test_the_gate_is_reported_on_an_order_that_passed(self, ready, live_client):
        """An interface that shows its checks only on failure teaches false safety."""
        header, account, market = ready
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        checks = {check["name"] for check in response.json()["results"]["gate"]["checks"]}
        assert "order_notional" in checks
        assert "kill_switch" in checks

    async def test_every_paper_fill_is_marked_counterfactual(self, ready, live_client):
        header, account, market = ready
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        fill = response.json()["results"]["order"]["fills"][0]
        assert "PAPER_FILL_COUNTERFACTUAL" in fill["flags"]
        # And it carries the exchange timestamp of the quote it was decided against.
        assert fill["quote_exchange_timestamp"] is not None

    async def test_a_sell_realises_against_the_average_and_the_book_agrees(
        self, ready, live_client, store
    ):
        header, account, market = ready
        await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="10",
        )
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24600.0, 24599.0, 24601.0)],
        )
        await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="SELL",
            quantity="4",
        )

        pnl = await live_client.get(
            f"/trading/accounts/{account['id']}/pnl", headers={"Authorization": header}
        )
        held = pnl.json()["results"]["positions"][0]
        assert Decimal(held["quantity"]) == 6
        # Sold four at the bid of 24599 against an average of 24501.
        assert Decimal(held["realised_pnl"]) == (Decimal("24599") - Decimal("24501")) * 4
        # The average price of what is left does not move on a reduction.
        assert Decimal(held["average_price"]) == Decimal("24501")


class TestTheGateRefusesRatherThanAdjusts:
    async def test_an_order_over_the_limit_is_recorded_as_rejected(self, ready, live_client):
        """A rejection is a thing that happened to an order, not an error with no record."""
        header, account, market = ready
        await live_client.put(
            f"/trading/accounts/{account['id']}/risk-limits",
            json={"max_order_notional": "1000"},
            headers={"Authorization": header},
        )
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="10",
        )
        assert response.status_code == 201
        order = response.json()["results"]["order"]
        assert order["status"] == "REJECTED"
        assert order["rejection"]["reason"] == "ORDER_NOTIONAL_LIMIT"
        assert Decimal(order["filled_quantity"]) == 0

        listed = await live_client.get(
            f"/trading/accounts/{account['id']}/orders",
            headers={"Authorization": header},
        )
        assert [item["status"] for item in listed.json()] == ["REJECTED"]

    async def test_the_kill_switch_halts_the_account_and_cancels_what_rests(
        self, ready, live_client
    ):
        header, account, market = ready
        resting = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="5",
            order_type="LIMIT",
            limit_price="20000",
        )
        assert resting.json()["results"]["order"]["status"] == "ACKNOWLEDGED"

        halted = await live_client.post(
            f"/trading/accounts/{account['id']}/kill-switch",
            json={"reason": "a strategy started behaving oddly"},
            headers={"Authorization": header},
        )
        assert halted.status_code == 200
        assert halted.json()["kill_switch_engaged"] is True

        order_id = resting.json()["results"]["order"]["id"]
        after = await live_client.get(
            f"/trading/orders/{order_id}", headers={"Authorization": header}
        )
        assert after.json()["status"] == "CANCELLED"

        blocked = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        assert blocked.json()["results"]["order"]["rejection"]["reason"] == "KILL_SWITCH_ENGAGED"

    async def test_a_kill_switch_without_a_reason_is_refused(self, ready, live_client):
        header, account, _market = ready
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/kill-switch",
            json={"reason": "   "},
            headers={"Authorization": header},
        )
        assert response.status_code == 422


class TestWhatAFillWillNotRestOn:
    async def test_a_market_order_with_no_offer_does_not_fill(self, live_client, market, store):
        """The rule behind ``Quote.mid_price`` returning None, at the fill."""
        _user, header = await register_and_login(live_client)
        account = await make_account(live_client, header)
        await publish(
            store,
            market["RELIANCE"],
            RELIANCE_KEY,
            [quote_entry(RELIANCE_KEY, datetime.now(UTC), 1400.0, 1399.0, None)],
        )
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["RELIANCE"].id),
            side="BUY",
            quantity="10",
        )
        order = response.json()["results"]["order"]
        assert order["status"] == "REJECTED"
        assert order["rejection"]["reason"] == "NO_TWO_SIDED_MARKET"

    async def test_the_permissive_policy_has_to_be_chosen_on_the_account(
        self, live_client, market, store
    ):
        _user, header = await register_and_login(live_client)
        account = await make_account(live_client, header, fill_policy="ALLOW_LAST_TRADE")
        await publish(
            store,
            market["RELIANCE"],
            RELIANCE_KEY,
            [quote_entry(RELIANCE_KEY, datetime.now(UTC), 1400.0, 1399.0, None)],
        )
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["RELIANCE"].id),
            side="BUY",
            quantity="10",
        )
        order = response.json()["results"]["order"]
        assert order["status"] == "FILLED"
        assert order["fills"][0]["price_basis"] == "LAST_TRADE"
        assert "LAST_TRADE_NOT_A_QUOTE" in order["fills"][0]["flags"]

    async def test_a_fill_capped_by_depth_leaves_the_rest_resting(self, live_client, market, store):
        _user, header = await register_and_login(live_client)
        account = await make_account(live_client, header)
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0, ask_size=4)],
        )
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="10",
        )
        order = response.json()["results"]["order"]
        assert order["status"] == "PARTIALLY_FILLED"
        assert Decimal(order["filled_quantity"]) == 4
        assert Decimal(order["remaining_quantity"]) == 6
        assert "LIMITED_BY_DEPTH" in order["fills"][0]["flags"]

    async def test_an_unpriced_position_suppresses_the_equity_figure(
        self, live_client, market, store
    ):
        """A total that quietly omits a position looks like an answer."""
        _user, header = await register_and_login(live_client)
        account = await make_account(live_client, header)
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0)],
        )
        await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        # The quote goes one-sided, so there is no mid to mark against.
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, None)],
        )
        pnl = await live_client.get(
            f"/trading/accounts/{account['id']}/pnl", headers={"Authorization": header}
        )
        body = pnl.json()
        assert body["results"]["equity"] is None
        assert body["results"]["unpriced"]
        assert "TRADING_UNPRICED_POSITIONS" in {w["code"] for w in body["warnings"]}


class TestRestingOrders:
    async def test_a_limit_order_rests_and_fills_when_the_market_reaches_it(
        self, ready, live_client, store
    ):
        header, account, market = ready
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="5",
            order_type="LIMIT",
            limit_price="24400",
        )
        order_id = placed.json()["results"]["order"]["id"]
        assert placed.json()["results"]["order"]["status"] == "ACKNOWLEDGED"

        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24350.0, 24349.0, 24351.0)],
        )
        worked = await live_client.post(
            f"/trading/accounts/{account['id']}/work", headers={"Authorization": header}
        )
        assert worked.status_code == 200
        assert [item["status"] for item in worked.json()] == ["FILLED"]

        final = await live_client.get(
            f"/trading/orders/{order_id}", headers={"Authorization": header}
        )
        # Filled at the ask that was there, not at its own limit.
        assert Decimal(final.json()["average_fill_price"]) == Decimal("24351")

    async def test_a_resting_order_is_not_rejected_for_being_unfillable(self, ready, live_client):
        """It has not failed; it is doing what it was sent to do."""
        header, account, market = ready
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="5",
            order_type="LIMIT",
            limit_price="20000",
        )
        order_id = placed.json()["results"]["order"]["id"]
        await live_client.post(
            f"/trading/accounts/{account['id']}/work", headers={"Authorization": header}
        )
        final = await live_client.get(
            f"/trading/orders/{order_id}", headers={"Authorization": header}
        )
        assert final.json()["status"] == "ACKNOWLEDGED"

    async def test_a_cancelled_order_cannot_be_cancelled_again(self, ready, live_client):
        header, account, market = ready
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="5",
            order_type="LIMIT",
            limit_price="20000",
        )
        order_id = placed.json()["results"]["order"]["id"]
        first = await live_client.post(
            f"/trading/orders/{order_id}/cancel",
            json={"reason": "changed my mind"},
            headers={"Authorization": header},
        )
        assert first.status_code == 200
        second = await live_client.post(
            f"/trading/orders/{order_id}/cancel",
            json={"reason": "again"},
            headers={"Authorization": header},
        )
        assert second.status_code == 409


class TestIdempotency:
    async def test_a_repeated_client_order_id_does_not_place_a_second_order(
        self, ready, live_client
    ):
        """What makes a retried submission safe."""
        header, account, market = ready
        body = {
            "instrument_id": str(market["NIFTY"].id),
            "side": "BUY",
            "quantity": "3",
            "order_type": "MARKET",
            "client_order_id": "retry-me",
        }
        first = await submit(live_client, header, account["id"], **body)
        second = await submit(live_client, header, account["id"], **body)

        assert first.json()["results"]["order"]["id"] == second.json()["results"]["order"]["id"]
        assert second.json()["results"]["replayed"] is True
        assert "TRADING_IDEMPOTENT_REPLAY" in {w["code"] for w in second.json()["warnings"]}

        listed = await live_client.get(
            f"/trading/accounts/{account['id']}/orders",
            headers={"Authorization": header},
        )
        assert len(listed.json()) == 1


class TestCostsAndProvenance:
    async def test_with_no_schedule_the_pnl_says_it_is_gross(self, ready, live_client):
        header, account, market = ready
        await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        pnl = await live_client.get(
            f"/trading/accounts/{account['id']}/pnl", headers={"Authorization": header}
        )
        body = pnl.json()
        assert body["results"]["gross_of_costs"] is True
        assert "TRADING_NO_COST_SCHEDULE" in {w["code"] for w in body["warnings"]}
        assert body["provenance"]["parameters"]["costs_modelled"] is False

    async def test_a_supplied_schedule_is_charged_and_travels_into_provenance(
        self, live_client, market, store
    ):
        _user, header = await register_and_login(live_client)
        account = await make_account(
            live_client,
            header,
            cost_schedule_name="illustrative",
            cost_schedule_source="rates supplied by the account owner for this test",
            cost_components=[
                {"name": "brokerage", "basis": "TURNOVER", "rate": "0.0003", "maximum": "20"},
                {
                    "name": "gst",
                    "basis": "ON_OTHER_COMPONENTS",
                    "rate": "0.18",
                    "applies_to": ["brokerage"],
                },
            ],
        )
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0)],
        )
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="10",
        )
        fill = response.json()["results"]["order"]["fills"][0]
        # Brokerage is capped at 20, and GST is charged on the capped figure.
        assert fill["cost"]["modelled"] is True
        assert Decimal(fill["cost"]["total"]) == Decimal("20") + Decimal("3.60")

        pnl = await live_client.get(
            f"/trading/accounts/{account['id']}/pnl", headers={"Authorization": header}
        )
        assert pnl.json()["results"]["gross_of_costs"] is False
        assert pnl.json()["provenance"]["parameters"]["cost_schedule"] == "illustrative"


class TestTheAuditTrail:
    async def test_every_decision_about_an_order_is_recorded(self, ready, live_client):
        header, account, market = ready
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="2",
        )
        order_id = placed.json()["results"]["order"]["id"]
        trail = await live_client.get(
            f"/trading/accounts/{account['id']}/audit",
            params={"order_id": order_id},
            headers={"Authorization": header},
        )
        kinds = {event["event_type"] for event in trail.json()}
        assert {"SUBMITTED", "GATE_DECISION", "BROKER_REQUEST", "BROKER_RESPONSE", "FILL"} <= kinds
        transitions = [
            (event["from_status"], event["to_status"])
            for event in trail.json()
            if event["event_type"] == "TRANSITION"
        ]
        assert ("NEW", "ACKNOWLEDGED") in transitions
        assert ("ACKNOWLEDGED", "FILLED") in transitions

    async def test_a_rejection_records_why_before_anything_else_happens(self, ready, live_client):
        header, account, market = ready
        await live_client.put(
            f"/trading/accounts/{account['id']}/risk-limits",
            json={"max_position_quantity": "1"},
            headers={"Authorization": header},
        )
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="50",
        )
        order_id = placed.json()["results"]["order"]["id"]
        trail = await live_client.get(
            f"/trading/accounts/{account['id']}/audit",
            params={"order_id": order_id},
            headers={"Authorization": header},
        )
        kinds = [event["event_type"] for event in trail.json()]
        assert "BROKER_REQUEST" not in kinds
        gate = next(event for event in trail.json() if event["event_type"] == "GATE_DECISION")
        assert gate["payload"]["rejection"]["reason"] == "POSITION_QUANTITY_LIMIT"


class TestRebalancePreview:
    async def test_it_differences_the_book_against_a_target_the_caller_supplied(
        self, ready, live_client
    ):
        header, account, market = ready
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/rebalance-preview",
            json={"targets": [{"instrument_id": str(market["NIFTY"].id), "weight": 0.5}]},
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        plan = response.json()["results"]
        trade = plan["trades"][0]
        assert trade["side"] == "BUY"
        # Half of a million at a mid of 24500 is 20.4 units, rounded down to 20.
        assert Decimal(trade["target_quantity"]) == 20
        assert Decimal(trade["rounding_residual"]) > 0
        assert any("becomes an order only when it is submitted" in note for note in plan["notes"])

    async def test_no_response_field_recommends_anything(self, ready, live_client):
        header, account, market = ready
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/rebalance-preview",
            json={"targets": [{"instrument_id": str(market["NIFTY"].id), "weight": 0.5}]},
            headers={"Authorization": header},
        )
        text = response.text.lower()
        for word in ("recommend", "fair value", "underpriced", "arbitrage", "optimal execution"):
            assert word not in text

    async def test_a_duplicated_target_is_refused_rather_than_deduplicated(
        self, ready, live_client
    ):
        header, account, market = ready
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/rebalance-preview",
            json={
                "targets": [
                    {"instrument_id": str(market["NIFTY"].id), "weight": 0.5},
                    {"instrument_id": str(market["NIFTY"].id), "weight": 0.2},
                ]
            },
            headers={"Authorization": header},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "DUPLICATE_TARGET"


class TestOwnership:
    async def test_another_user_cannot_see_the_account(self, ready, live_client):
        _header, account, _market = ready
        _other, other_header = await register_and_login(live_client, "other@example.com")
        response = await live_client.get(
            f"/trading/accounts/{account['id']}", headers={"Authorization": other_header}
        )
        assert response.status_code == 404

    async def test_an_anonymous_caller_cannot_place_an_order(self, live_client, market):
        response = await live_client.post(
            f"/trading/accounts/{uuid.uuid4()}/orders",
            json={
                "instrument_id": str(market["NIFTY"].id),
                "side": "BUY",
                "quantity": "1",
                "order_type": "MARKET",
            },
        )
        assert response.status_code == 401


class TestTheBookIsReconstructible:
    async def test_replaying_the_fills_reproduces_the_stored_position(
        self, ready, live_client, db_session, store
    ):
        """The stored book is a cache of the fills, and this is what says so.

        Positions are kept rather than replayed because a live risk view cannot
        walk a year of fills per request. That optimisation is only safe while
        the two agree, so the agreement is asserted rather than assumed.
        """
        from domains.execution.oms.repository import TradingRepository
        from domains.research.costs import TradeCost
        from domains.research.models import Book, Fill

        header, account, market = ready
        for side, quantity in (("BUY", "10"), ("SELL", "4"), ("BUY", "7"), ("SELL", "13")):
            await publish(
                store,
                market["NIFTY"],
                NIFTY_KEY,
                [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0)],
            )
            await submit(
                live_client,
                header,
                account["id"],
                instrument_id=str(market["NIFTY"].id),
                side=side,
                quantity=quantity,
            )

        repository = TradingRepository(db_session)
        account_id = uuid.UUID(account["id"])
        stored = await repository.positions(account_id)
        assert stored, "the account should hold a position row"

        replayed = Book(cash=Decimal(account["opening_cash"]))
        for row in await repository.fills_for_account(account_id):
            replayed.apply(
                Fill(
                    instrument_id=row.instrument_id,
                    timestamp=row.filled_at,
                    quantity=row.quantity,
                    price=row.price,
                    reference_price=row.reference_price or row.price,
                    cost=TradeCost(modelled=row.costs_modelled),
                )
            )

        for row in stored:
            rebuilt = replayed.position(row.instrument_id)
            assert rebuilt.quantity == row.quantity
            assert rebuilt.realised_pnl == row.realised_pnl
            if rebuilt.quantity != 0:
                assert rebuilt.average_price == row.average_price

    async def test_the_database_refuses_an_order_that_filled_more_than_it_asked(
        self, ready, live_client, db_session
    ):
        """A CHECK that compares decimals has to compare them as numbers.

        ``DecimalType`` is TEXT on SQLite, so an uncast ``filled_quantity <=
        quantity`` would compare strings, where ``'4' <= '10'`` is false and
        ``'40' <= '10'`` is true — the constraint would reject the honest case
        and admit the impossible one. This asserts it bites on the dialect where
        it would otherwise silently invert.
        """
        import sqlalchemy as sa

        header, account, market = ready
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="10",
        )
        order_id = placed.json()["results"]["order"]["id"]

        with pytest.raises(Exception, match="ck_order_filled_within_quantity"):
            await db_session.execute(
                sa.text("update trading_orders set filled_quantity = '40' where id = :order"),
                {"order": uuid.UUID(order_id).hex},
            )
            await db_session.flush()


class TestLiveTradingIsGatedTwice:
    """Phase 7. Two independent gates, and a paper account behind neither."""

    async def live_account(self, client, header, **overrides) -> dict:
        body = {
            "name": f"live-{uuid.uuid4().hex[:8]}",
            "opening_cash": "1000000",
            "venue": "LIVE",
            "risk_limits": {"max_order_notional": "500000"},
            "cost_schedule_name": "illustrative",
            "cost_schedule_source": "supplied for this test",
            "cost_components": [
                {"name": "brokerage", "basis": "TURNOVER", "rate": "0.0003", "maximum": "20"}
            ],
            **overrides,
        }
        response = await client.post(
            "/trading/accounts", json=body, headers={"Authorization": header}
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_a_live_account_is_created_unarmed(self, live_client):
        """Creating a book is not the same act as deciding it may trade money."""
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header)
        assert account["live_armed_at"] is None

    async def test_arming_reports_every_obstacle_at_once(self, live_client):
        """Arming is done once, carefully; one obstacle at a time wastes the care."""
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header, risk_limits=None, cost_components=[])
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/arm", headers={"Authorization": header}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["armed"] is False
        joined = " ".join(body["refusals"])
        assert "live trading is disabled for this deployment" in joined
        assert "no risk limits are set" in joined
        assert "no cost schedule is set" in joined

    async def test_a_paper_account_cannot_be_armed(self, ready, live_client):
        header, account, _market = ready
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/arm", headers={"Authorization": header}
        )
        assert response.json()["armed"] is False
        assert any("paper account" in reason for reason in response.json()["refusals"])

    async def test_an_unarmed_live_order_is_refused_and_recorded(self, live_client, market, store):
        """The order exists, rejected, with the reason on it.

        The quote matters. Without one the *gate* refuses first, for want of a
        price to measure the notional limit against, and the test would pass
        without ever reaching the live-trading check it is about.
        """
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header)
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0)],
        )
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        assert response.status_code == 201, response.text
        order = response.json()["results"]["order"]
        assert order["status"] == "REJECTED"
        assert order["rejection"]["reason"] == "LIVE_TRADING_DISABLED"
        assert order["venue"] == "LIVE"

    async def test_a_live_order_never_reaches_a_broker_while_disabled(
        self, live_client, market, store
    ):
        """The absence of a BROKER_REQUEST is the evidence nothing was sent."""
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header)
        await publish(
            store,
            market["NIFTY"],
            NIFTY_KEY,
            [quote_entry(NIFTY_KEY, datetime.now(UTC), 24500.0, 24499.0, 24501.0)],
        )
        placed = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        trail = await live_client.get(
            f"/trading/accounts/{account['id']}/audit",
            params={"order_id": placed.json()["results"]["order"]["id"]},
            headers={"Authorization": header},
        )
        assert "BROKER_REQUEST" not in {event["event_type"] for event in trail.json()}

    async def test_a_live_account_with_no_limits_is_refused_at_the_gate(self, live_client, market):
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header, risk_limits=None)
        response = await submit(
            live_client,
            header,
            account["id"],
            instrument_id=str(market["NIFTY"].id),
            side="BUY",
            quantity="1",
        )
        order = response.json()["results"]["order"]
        assert order["rejection"]["reason"] == "NO_RISK_LIMITS_CONFIGURED"

    async def test_disarming_is_not_the_kill_switch(self, live_client):
        """Disarming says "not now"; the kill switch says "stop" and cancels."""
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header)
        response = await live_client.request(
            "DELETE",
            f"/trading/accounts/{account['id']}/arm",
            headers={"Authorization": header},
        )
        assert response.status_code == 200
        assert response.json()["live_armed_at"] is None
        assert response.json()["kill_switch_engaged"] is False

    async def test_the_arming_attempt_is_in_the_audit_trail(self, live_client):
        _user, header = await register_and_login(live_client)
        account = await self.live_account(live_client, header)
        await live_client.post(
            f"/trading/accounts/{account['id']}/arm", headers={"Authorization": header}
        )
        trail = await live_client.get(
            f"/trading/accounts/{account['id']}/audit", headers={"Authorization": header}
        )
        armed = [event for event in trail.json() if event["event_type"] == "LIVE_ARMED"]
        assert armed and armed[0]["payload"]["armed"] is False
        assert armed[0]["payload"]["reasons"]


class TestWorkingAParentOrder:
    async def test_only_the_open_slice_is_placed(self, ready, live_client):
        """The rest of the window has not happened yet."""
        header, account, market = ready
        now = datetime.now(UTC)
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/parent-orders",
            json={
                "parent_client_order_id": "parent-a",
                "instrument_id": str(market["NIFTY"].id),
                "side": "BUY",
                "quantity": "40",
                "strategy": "TWAP",
                "window": {
                    "start": now.isoformat(),
                    "end": (now + timedelta(hours=4)).isoformat(),
                    "slices": 4,
                    "reference_price": "24500",
                },
            },
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results["slices_total"] == 4
        assert len(results["placed_now"]) == 1
        assert results["placed_now"][0]["quantity"] == "10"

    async def test_calling_it_twice_does_not_place_the_slice_twice(self, ready, live_client):
        """Child ids derive from the parent's, so idempotency does the work."""
        header, account, market = ready
        now = datetime.now(UTC)
        body = {
            "parent_client_order_id": "parent-b",
            "instrument_id": str(market["NIFTY"].id),
            "side": "BUY",
            "quantity": "40",
            "strategy": "TWAP",
            "window": {
                "start": now.isoformat(),
                "end": (now + timedelta(hours=4)).isoformat(),
                "slices": 4,
                "reference_price": "24500",
            },
        }
        first = await live_client.post(
            f"/trading/accounts/{account['id']}/parent-orders",
            json=body,
            headers={"Authorization": header},
        )
        second = await live_client.post(
            f"/trading/accounts/{account['id']}/parent-orders",
            json=body,
            headers={"Authorization": header},
        )
        assert len(first.json()["results"]["placed_now"]) == 1
        assert second.json()["results"]["placed_now"] == []
        assert second.json()["results"]["slices_already_placed"] == 1

        orders = await live_client.get(
            f"/trading/accounts/{account['id']}/orders",
            headers={"Authorization": header},
        )
        assert len(orders.json()) == 1

    async def test_a_closed_slice_is_reported_missed_not_placed_late(self, ready, live_client):
        header, account, market = ready
        started = datetime.now(UTC) - timedelta(hours=3)
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/parent-orders",
            json={
                "parent_client_order_id": "parent-c",
                "instrument_id": str(market["NIFTY"].id),
                "side": "BUY",
                "quantity": "40",
                "strategy": "TWAP",
                "window": {
                    "start": started.isoformat(),
                    "end": (started + timedelta(hours=4)).isoformat(),
                    "slices": 4,
                    "reference_price": "24500",
                },
            },
            headers={"Authorization": header},
        )
        results = response.json()["results"]
        assert results["missed"] == [0, 1, 2]
        assert "TRADING_SCHEDULE_SLICES_MISSED" in {
            item["code"] for item in response.json()["warnings"]
        }

    async def test_a_vwap_without_a_volume_profile_is_refused(self, ready, live_client):
        """Falling back to TWAP would answer a different question under this name."""
        header, account, market = ready
        now = datetime.now(UTC)
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/parent-orders",
            json={
                "parent_client_order_id": "parent-d",
                "instrument_id": str(market["NIFTY"].id),
                "side": "BUY",
                "quantity": "40",
                "strategy": "VWAP",
                "window": {
                    "start": now.isoformat(),
                    "end": (now + timedelta(hours=4)).isoformat(),
                    "slices": 4,
                    "reference_price": "24500",
                },
            },
            headers={"Authorization": header},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "SCHEDULE_REFUSED"
        assert "volume profile" in response.json()["detail"]

    async def test_no_response_here_claims_an_optimal_execution(self, ready, live_client):
        header, account, market = ready
        now = datetime.now(UTC)
        response = await live_client.post(
            f"/trading/accounts/{account['id']}/parent-orders",
            json={
                "parent_client_order_id": "parent-e",
                "instrument_id": str(market["NIFTY"].id),
                "side": "BUY",
                "quantity": "40",
                "strategy": "TWAP",
                "window": {
                    "start": now.isoformat(),
                    "end": (now + timedelta(hours=4)).isoformat(),
                    "slices": 4,
                    "reference_price": "24500",
                },
            },
            headers={"Authorization": header},
        )
        text = response.text.lower()
        for word in ("optimal execution", "best execution", "recommend"):
            assert word not in text
