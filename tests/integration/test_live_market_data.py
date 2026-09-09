"""Phase 1 end to end: a user selects NIFTY and sees a live price.

The path under test is the whole of it — instrument master, subscription, feed,
normalisation, quality, live store, API — with one substitution: the transport
is scripted rather than a socket to a broker. Everything downstream of the frame
is the production code, so a test that passes here is a test of the platform
rather than of a mock.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.instruments.enums import AssetClass
from domains.instruments.service import InstrumentService
from domains.market_data.live import LiveMarketDataService
from domains.market_data.providers.upstox import UpstoxMarketDataProvider
from domains.market_data.streaming.feed import FeedEntry
from domains.market_data.streaming.live_state import LiveMarketStore
from domains.market_data.streaming.manager import MarketStreamManager, StreamOptions
from tests.conftest import register_and_login

NIFTY_KEY = "NSE_INDEX|Nifty 50"
OPTION_KEY = "NSE_FO|46833"

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
        "segment": "NSE_FO",
        "exchange": "NSE",
        "expiry": 1758186000000,
        "instrument_type": "CE",
        "underlying_symbol": "NIFTY",
        "underlying_key": NIFTY_KEY,
        "instrument_key": OPTION_KEY,
        "lot_size": 75,
        "tick_size": 5.0,
        "trading_symbol": "NIFTY 25400 CE 18 SEP 25",
        "strike_price": 25400.0,
    },
]


def quote_entry(timestamp: datetime, last: float, bid: float, ask: float, oi: int = 0):
    """A provider full-quote entry, shaped as the live spec expects it."""
    return {
        "instrument_token": NIFTY_KEY,
        "symbol": "Nifty 50",
        "last_price": last,
        "volume": 128_400,
        "oi": oi,
        "timestamp": timestamp.isoformat(),
        "ohlc": {"open": 24480.0, "high": 24530.1, "low": 24460.0, "close": 24475.5},
        "depth": {
            "buy": [{"quantity": 50, "price": bid, "orders": 3}],
            "sell": [{"quantity": 75, "price": ask, "orders": 5}],
        },
    }


class _NullDirectory:
    async def provider_key(self, instrument_id):
        return None

    async def instrument(self, instrument_id):
        return None

    async def by_provider_key(self, key):
        return None

    async def option_contracts(self, underlying_id, expiry=None):
        return ()


@pytest.fixture
def live_settings(app_environment):
    """Configured for a live provider, so the routes behave as they would."""
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
    """The same store the API reads, so the test writes where the app looks."""
    from infrastructure.cache.client import get_cache

    return LiveMarketStore(get_cache(live_settings), live_settings.live_quote_ttl_seconds)


@pytest.fixture
async def instruments(db_session, live_settings, store):
    """Load the instrument master, as the refresh job would."""
    service = LiveMarketDataService(InstrumentService(db_session), store, source="upstox")
    result = await service.load_instrument_master(MASTER_ROWS)
    await db_session.commit()
    return service, result


async def feed(store: LiveMarketStore, entries: list[dict], instrument, key: str):
    """Push provider entries through the real manager into the live store."""
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
    return manager


async def _token() -> str:
    return "unused"


class TestTheInstrumentMasterBecomesPlatformInstruments:
    async def test_the_underlying_and_its_contract_are_both_created(self, instruments):
        _service, result = instruments
        assert result.conserved is True
        assert result.accepted == 2
        assert {item.asset_class for item in result.instruments} == {
            AssetClass.INDEX,
            AssetClass.OPTION,
        }

    async def test_the_contract_is_attached_to_the_underlying(self, instruments):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        option = next(i for i in result.instruments if i.is_option)
        assert option.underlying_id == index.id

    async def test_the_providers_identifier_round_trips(self, instruments, db_session):
        service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)

        assert await service.directory.provider_key(index.id) == NIFTY_KEY
        found = await service.directory.by_provider_key(NIFTY_KEY)
        assert found is not None and found.id == index.id


class TestSelectingNiftyAndSeeingALivePrice:
    """The Phase 1 acceptance criterion, in one test class."""

    async def test_the_user_can_find_nifty_by_symbol(self, live_client, instruments):
        _user, header = await register_and_login(live_client)
        response = await live_client.get(
            "/instruments",
            params={"symbol": "NIFTY", "asset_class": "INDEX"},
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        assert [item["symbol"] for item in response.json()["items"]] == ["NIFTY"]

    async def test_subscribing_registers_interest_the_feed_worker_can_see(
        self, live_client, instruments, store
    ):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        _user, header = await register_and_login(live_client)

        response = await live_client.post(
            "/live/subscriptions",
            json={"instrument_ids": [str(index.id)]},
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        assert response.json()["instrument_ids"] == [str(index.id)]

        # The worker runs in another process and reads exactly this.
        assert await store.interest("upstox") == {index.id}

    async def test_a_live_price_appears_with_bid_ask_volume_and_open_interest(
        self, live_client, instruments, store
    ):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        now = datetime.now(UTC)
        await feed(
            store,
            [quote_entry(now, 24512.35, 24512.30, 24512.40, oi=1_250_000)],
            index,
            NIFTY_KEY,
        )

        _user, header = await register_and_login(live_client)
        response = await live_client.get(
            "/live/quotes",
            params={"instrument_ids": [str(index.id)]},
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["unavailable"] == []

        quote = body["items"][0]
        assert quote["symbol"] == "NIFTY"
        assert Decimal(quote["last_price"]) == Decimal("24512.35")
        assert Decimal(quote["bid_price"]) == Decimal("24512.3")
        assert Decimal(quote["ask_price"]) == Decimal("24512.4")
        assert Decimal(quote["volume"]) == Decimal(128_400)
        assert Decimal(quote["open_interest"]) == Decimal(1_250_000)
        assert quote["quality"]["overall_score"] > 0

    async def test_every_live_price_carries_its_own_age(self, live_client, instruments, store):
        """A price with no visible age gets treated as current whatever it
        actually is, which is the entire failure mode of a live feed."""
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        stale_moment = datetime.now(UTC) - timedelta(minutes=4)
        await feed(store, [quote_entry(stale_moment, 24500.0, 24499.0, 24501.0)], index, NIFTY_KEY)

        _user, header = await register_and_login(live_client)
        response = await live_client.get(
            f"/live/quotes/{index.id}", headers={"Authorization": header}
        )
        assert response.json()["age_seconds"] > 200

    async def test_the_price_updates_as_the_feed_delivers(self, live_client, instruments, store):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        start = datetime.now(UTC) - timedelta(seconds=2)
        await feed(
            store,
            [
                quote_entry(start, 24500.0, 24499.5, 24500.5),
                quote_entry(start + timedelta(seconds=1), 24515.0, 24514.5, 24515.5),
            ],
            index,
            NIFTY_KEY,
        )

        _user, header = await register_and_login(live_client)
        response = await live_client.get(
            f"/live/quotes/{index.id}", headers={"Authorization": header}
        )
        assert Decimal(response.json()["last_price"]) == Decimal("24515.0")

    async def test_an_instrument_with_no_live_price_is_named_not_omitted(
        self, live_client, instruments
    ):
        """A short answer must never be mistaken for a complete one."""
        _service, result = instruments
        option = next(i for i in result.instruments if i.is_option)
        _user, header = await register_and_login(live_client)

        response = await live_client.get(
            "/live/quotes",
            params={"instrument_ids": [str(option.id)]},
            headers={"Authorization": header},
        )
        assert response.json()["items"] == []
        assert response.json()["unavailable"] == [str(option.id)]


class TestTheLiveMarketStateIsTheJoinToEverythingElse:
    async def test_a_snapshot_is_assembled_from_live_prices(self, live_client, instruments, store):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        await feed(
            store,
            [quote_entry(datetime.now(UTC), 24512.35, 24512.30, 24512.40)],
            index,
            NIFTY_KEY,
        )

        _user, header = await register_and_login(live_client)
        response = await live_client.get(
            "/live/state",
            params={"instrument_ids": [str(index.id)], "include_quotes": True},
            headers={"Authorization": header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state_id"].startswith("state:")
        assert body["quote_count"] == 1
        assert body["sources"] == ["upstox"]
        assert str(index.id) in body["quotes"]

    async def test_one_moment_and_one_set_of_prices_is_always_the_same_id(
        self, live_client, instruments, store
    ):
        """Content-addressed, so two calculations reporting one id provably saw
        the same inputs — the property the whole architecture rests on.

        The moment is part of that identity, so it has to be pinned to compare:
        two different instants are two different snapshots even if nothing moved,
        which is why ``as_of`` exists on this endpoint at all.
        """
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        observed = datetime.now(UTC) - timedelta(seconds=5)
        await feed(store, [quote_entry(observed, 24512.35, 24512.30, 24512.40)], index, NIFTY_KEY)

        _user, header = await register_and_login(live_client)
        params = {"instrument_ids": [str(index.id)], "as_of": datetime.now(UTC).isoformat()}

        first = await live_client.get(
            "/live/state", params=params, headers={"Authorization": header}
        )
        second = await live_client.get(
            "/live/state", params=params, headers={"Authorization": header}
        )
        assert first.json()["state_id"] == second.json()["state_id"]

        # And a price that moved is a different snapshot of the same moment.
        await feed(
            store,
            [quote_entry(observed + timedelta(seconds=1), 24600.0, 24599.5, 24600.5)],
            index,
            NIFTY_KEY,
        )
        moved = await live_client.get(
            "/live/state", params=params, headers={"Authorization": header}
        )
        assert moved.json()["state_id"] != first.json()["state_id"]

    async def test_a_quote_stamped_after_the_snapshot_is_not_in_it(
        self, live_client, instruments, store
    ):
        """A state that claims to be a moment must not contain something that
        had not happened yet."""
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        future = datetime.now(UTC) + timedelta(minutes=5)
        await feed(store, [quote_entry(future, 24512.35, 24512.30, 24512.40)], index, NIFTY_KEY)

        _user, header = await register_and_login(live_client)
        response = await live_client.get(
            "/live/state",
            params={"instrument_ids": [str(index.id)], "as_of": datetime.now(UTC).isoformat()},
            headers={"Authorization": header},
        )
        assert response.json()["quote_count"] == 0
        assert response.json()["unavailable"] == [str(index.id)]

    async def test_an_index_gets_a_spot_price_from_its_own_level(
        self, live_client, instruments, store
    ):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        service = LiveMarketDataService(_service._instruments, store, source="upstox")
        await feed(
            store,
            [quote_entry(datetime.now(UTC), 24512.35, 24512.30, 24512.40)],
            index,
            NIFTY_KEY,
        )

        state, _missing = await service.live_market_state([index.id])
        assert state.spot_prices[index.id] == Decimal("24512.35")


class TestTheFeedSaysWhatItIsDoing:
    async def test_status_reports_the_provider_transport_and_health(
        self, live_client, instruments, store
    ):
        _service, result = instruments
        index = next(i for i in result.instruments if i.asset_class is AssetClass.INDEX)
        await feed(
            store, [quote_entry(datetime.now(UTC), 24512.35, 24512.3, 24512.4)], index, NIFTY_KEY
        )

        _user, header = await register_and_login(live_client)
        response = await live_client.get("/live/status", headers={"Authorization": header})
        body = response.json()

        assert body["provider"] == "upstox"
        assert body["transport"] == "polling"
        assert body["health"]["events_received"] == 1

    async def test_a_polling_transport_does_not_claim_to_deliver_every_tick(
        self, live_client, instruments
    ):
        """Stated rather than assumed: a queue or intensity model built on
        samples would be a model of a book nobody saw."""
        _user, header = await register_and_login(live_client)
        response = await live_client.get("/live/status", headers={"Authorization": header})
        assert response.json()["delivers_every_update"] is False
        assert response.json()["poll_interval_seconds"] == 1.0

    async def test_with_no_worker_reporting_the_reason_is_named(self, live_client, instruments):
        _user, header = await register_and_login(live_client)
        response = await live_client.get("/live/status", headers={"Authorization": header})
        assert "stream worker" in response.json()["unavailable_reason"]

    async def test_a_synthetic_deployment_says_its_prices_are_not_real(
        self, client, app_environment
    ):
        _user, header = await register_and_login(client)
        response = await client.get("/live/status", headers={"Authorization": header})
        assert response.json()["provider"] == "synthetic"
        assert "describe nothing real" in response.json()["unavailable_reason"]


class TestOwnershipAndAccess:
    async def test_every_live_route_requires_a_signed_in_caller(self, live_client):
        for path in ("/live/status", "/live/quotes", "/live/state"):
            response = await live_client.get(path, params={"instrument_ids": [str(uuid.uuid4())]})
            assert response.status_code == 401, path

    async def test_subscribing_to_an_unknown_instrument_is_a_404(self, live_client, instruments):
        _user, header = await register_and_login(live_client)
        response = await live_client.post(
            "/live/subscriptions",
            json={"instrument_ids": [str(uuid.uuid4())]},
            headers={"Authorization": header},
        )
        assert response.status_code == 404
