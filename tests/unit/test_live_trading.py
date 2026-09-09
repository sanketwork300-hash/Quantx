"""Phase 7: the live adapter, the arming gates and the schedule.

Every test here runs the real adapter against a recorded payload through a
transport that cannot reach a network. That is the point of the seam: the
normalisation under test is production code, and no test in this repository can
place a real order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.execution.brokers.base import BrokerCapability, BrokerError, BrokerRejected
from domains.execution.brokers.upstox import (
    DEFAULT_STATUS_MAP,
    LiveMappingUnverified,
    UnknownBrokerStatus,
    UpstoxBroker,
    UpstoxOrderEndpoints,
)
from domains.execution.oms.algorithms import (
    AlgorithmError,
    WorkingWindow,
    plan_parent_order,
)
from domains.execution.oms.models import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)

NOW = datetime(2026, 3, 2, 4, 0, tzinfo=UTC)
INSTRUMENT = uuid.uuid4()
KEY = "NSE_EQ|INE002A01018"


class RecordedTransport:
    """Replays a recorded payload and records what was sent."""

    def __init__(self, status: int = 200, payload=None) -> None:
        self.status = status
        self.payload = payload
        self.calls: list[dict] = []

    async def request_json(self, method, url, token, json_body=None, params=None):
        self.calls.append(
            {"method": method, "url": url, "body": json_body, "params": params, "token": token}
        )
        return self.status, self.payload


class Keys:
    async def provider_key(self, instrument_id):
        return KEY if instrument_id == INSTRUMENT else None

    async def by_provider_key(self, key):
        return None


async def _token() -> str:
    return "recorded-token"


def broker(transport: RecordedTransport, *, verified: bool = True, status_map=None) -> UpstoxBroker:
    return UpstoxBroker(
        transport,
        _token,
        Keys(),
        endpoints=UpstoxOrderEndpoints(verified_against_documentation=verified),
        status_map=status_map,
    )


def request(
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        instrument_id=INSTRUMENT,
        side=side,
        quantity=Decimal("10"),
        order_type=order_type,
        limit_price=Decimal(limit) if limit else None,
    )


class TestTheMappingHasToBeChecked:
    """The gate that exists exactly where a mistake costs money."""

    async def test_an_unverified_adapter_will_not_place_a_live_order(self):
        transport = RecordedTransport(payload={"data": {"order_id": "x"}})
        with pytest.raises(LiveMappingUnverified, match="confirmed against"):
            await broker(transport, verified=False).place_order(request())
        assert transport.calls == [], "nothing should have been sent"

    async def test_the_default_endpoint_set_is_unverified(self):
        """Nothing in this repository has read the broker's published contract."""
        assert UpstoxOrderEndpoints().verified_against_documentation is False

    async def test_an_unknown_status_is_an_error_not_a_guess(self):
        """An order in an unknown state is a position of unknown size."""
        transport = RecordedTransport(
            payload={"data": {"order_id": "1", "status": "after market order req received"}}
        )
        with pytest.raises(UnknownBrokerStatus, match="no mapping for"):
            await broker(transport).get_orders()

    async def test_the_error_lists_the_states_it_does_know(self):
        transport = RecordedTransport(payload={"data": {"order_id": "1", "status": "weird"}})
        with pytest.raises(UnknownBrokerStatus, match="complete"):
            await broker(transport).get_orders()

    async def test_complete_and_cancelled_are_not_collapsed(self):
        """Both terminal, and treating one as the other loses or invents a position."""
        assert DEFAULT_STATUS_MAP["complete"] is OrderStatus.FILLED
        assert DEFAULT_STATUS_MAP["cancelled"] is OrderStatus.CANCELLED


class TestPlacement:
    async def test_a_placement_acknowledgement_is_not_read_as_working(self):
        """An id and nothing else means acknowledged, not filled and not resting."""
        transport = RecordedTransport(payload={"data": {"order_id": "240301010101"}})
        update = await broker(transport).place_order(request())
        assert update.status is OrderStatus.ACKNOWLEDGED
        assert update.broker_order_id == "240301010101"
        assert update.fills == ()

    async def test_the_request_carries_the_instrument_key_from_the_master(self):
        transport = RecordedTransport(payload={"data": {"order_id": "1"}})
        await broker(transport).place_order(request())
        assert transport.calls[0]["body"]["instrument_token"] == KEY
        assert transport.calls[0]["body"]["transaction_type"] == "BUY"
        assert transport.calls[0]["body"]["order_type"] == "MARKET"

    async def test_an_immediate_or_cancel_order_says_so_in_its_validity(self):
        transport = RecordedTransport(payload={"data": {"order_id": "1"}})
        payload = OrderRequest(
            instrument_id=INSTRUMENT,
            side=OrderSide.SELL,
            quantity=Decimal("5"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("1400"),
            time_in_force=TimeInForce.IMMEDIATE_OR_CANCEL,
        )
        await broker(transport).place_order(payload)
        assert transport.calls[0]["body"]["validity"] == "IOC"
        assert transport.calls[0]["body"]["price"] == 1400.0

    async def test_an_instrument_with_no_broker_key_is_refused_before_sending(self):
        transport = RecordedTransport(payload={"data": {"order_id": "1"}})
        payload = OrderRequest(
            instrument_id=uuid.uuid4(),
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MARKET,
        )
        with pytest.raises(BrokerRejected, match="no upstox key"):
            await broker(transport).place_order(payload)
        assert transport.calls == []

    async def test_a_broker_error_payload_becomes_a_rejection_with_its_code(self):
        transport = RecordedTransport(
            status=400,
            payload={"errors": [{"errorCode": "UDAPI1010", "message": "insufficient funds"}]},
        )
        with pytest.raises(BrokerRejected, match="insufficient funds") as caught:
            await broker(transport).place_order(request())
        assert caught.value.code == "UDAPI1010"

    async def test_a_server_error_is_an_unknown_outcome_not_a_rejection(self):
        """The order may or may not have arrived; saying which would be a guess."""
        transport = RecordedTransport(status=502, payload=None)
        with pytest.raises(BrokerError, match="unknown"):
            await broker(transport).place_order(request())

    async def test_a_refused_credential_says_no_order_was_placed(self):
        transport = RecordedTransport(status=401, payload={"message": "invalid token"})
        with pytest.raises(BrokerError, match="no order was placed"):
            await broker(transport).place_order(request())


class TestReadingBackAnOrder:
    def order_payload(self, **overrides) -> dict:
        payload = {
            "order_id": "240301010101",
            "status": "complete",
            "quantity": 10,
            "filled_quantity": 10,
            "average_price": 1402.35,
            "transaction_type": "BUY",
            "exchange_timestamp": "2026-03-02T04:15:00+00:00",
            "exchange_order_id": "1300000012345678",
        }
        payload.update(overrides)
        return payload

    async def test_a_completed_order_produces_a_fill_named_for_what_it_is(self):
        """The broker's average across executions is not a single trade's price."""
        transport = RecordedTransport(payload={"data": self.order_payload()})
        update = (await broker(transport).get_orders())[0]
        assert update.status is OrderStatus.FILLED
        assert update.fills[0].price == Decimal("1402.35")
        assert update.fills[0].price_basis == "BROKER_REPORTED_AVERAGE"
        assert update.fills[0].quantity == Decimal("10")

    async def test_a_sell_is_booked_negative_from_the_brokers_own_field(self):
        """A fill booked on the wrong side inverts a position."""
        transport = RecordedTransport(payload={"data": self.order_payload(transaction_type="SELL")})
        update = (await broker(transport).get_orders())[0]
        assert update.fills[0].quantity == Decimal("-10")

    async def test_a_completed_order_with_nothing_filled_is_refused(self):
        """Either the mapping or the payload is wrong, and both need looking at."""
        transport = RecordedTransport(
            payload={"data": self.order_payload(filled_quantity=0, average_price=0)}
        )
        with pytest.raises(BrokerError, match="completed order with nothing filled"):
            await broker(transport).get_orders()

    async def test_a_completed_order_short_of_its_quantity_is_partially_filled(self):
        transport = RecordedTransport(payload={"data": self.order_payload(filled_quantity=4)})
        update = (await broker(transport).get_orders())[0]
        assert update.status is OrderStatus.PARTIALLY_FILLED

    async def test_fields_the_adapter_does_not_map_are_reported(self):
        """A field that appears and is silently dropped is a silent schema change."""
        transport = RecordedTransport(
            payload={"data": self.order_payload(variety="AMO", tag="something-new")}
        )
        update = (await broker(transport).get_orders())[0]
        assert "variety" in update.unmapped_fields
        assert "tag" in update.unmapped_fields

    async def test_the_brokers_own_payload_is_kept(self):
        """When a mapping turns out wrong, this is the only evidence of what was said."""
        transport = RecordedTransport(payload={"data": self.order_payload()})
        update = (await broker(transport).get_orders())[0]
        assert update.raw["order_id"] == "240301010101"

    async def test_a_rejection_carries_the_brokers_message(self):
        transport = RecordedTransport(
            payload={
                "data": self.order_payload(
                    status="rejected",
                    filled_quantity=0,
                    average_price=0,
                    status_message="RMS: price outside band",
                )
            }
        )
        update = (await broker(transport).get_orders())[0]
        assert update.status is OrderStatus.REJECTED
        assert "price outside band" in update.rejection_detail


class TestBalancesArePassedThroughAsReports:
    async def test_margin_figures_keep_their_attribution(self):
        """The platform computes no margin; what is here is the broker's own."""
        transport = RecordedTransport(
            payload={
                "data": {
                    "equity": {
                        "available_margin": 152340.55,
                        "used_margin": 41200.0,
                        "payin_amount": 0.0,
                        "span_margin": 1234.0,
                    }
                }
            }
        )
        account = await broker(transport).get_account()
        assert account.broker == "upstox"
        assert account.reported_available_margin == Decimal("152340.55")
        assert "span_margin" in account.unmapped_fields
        payload = account.to_dict()
        assert set(payload) >= {"reported_available_margin", "reported_at", "broker"}
        assert not any(key.startswith("margin") for key in payload)


class TestCapabilities:
    async def test_modify_is_not_offered(self):
        """An amendment that silently becomes a no-op leaves an order at terms
        nobody chose."""
        adapter = broker(RecordedTransport())
        assert not adapter.supports(BrokerCapability.MODIFY)
        with pytest.raises(NotImplementedError, match="Cancel and replace"):
            await adapter.modify_order("1")


class TestTheSchedule:
    def window(self, **overrides) -> WorkingWindow:
        base = {
            "start": NOW,
            "end": NOW + timedelta(hours=2),
            "slices": 4,
            "reference_price": Decimal("1400"),
            "average_daily_volume": 1_000_000.0,
            "volatility": 0.2,
        }
        base.update(overrides)
        return WorkingWindow(**base)

    def plan(self, strategy: str = "TWAP", **overrides):
        return plan_parent_order(
            parent_client_order_id="parent-1",
            instrument_id=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Decimal("400"),
            strategy=strategy,
            window=overrides.pop("window", self.window()),
            **overrides,
        )

    def test_a_twap_splits_the_quantity_evenly(self):
        plan = self.plan()
        assert [item.quantity for item in plan.schedule.slices] == [Decimal(100)] * 4
        assert sum(item.quantity for item in plan.schedule.slices) == Decimal("400")

    def test_child_ids_are_derived_so_a_retry_places_each_slice_once(self):
        plan = self.plan()
        assert plan.child_client_order_id(0) == "parent-1-000"
        assert plan.child_client_order_id(3) == "parent-1-003"

    def test_a_child_order_is_immediate_or_cancel(self):
        """One that rested past its interval would still be working when the next
        slice arrived, and the schedule would deliver more than it planned."""
        child = self.plan().child_request(0)
        assert child.time_in_force is TimeInForce.IMMEDIATE_OR_CANCEL
        assert child.metadata["parent_client_order_id"] == "parent-1"

    def test_only_the_open_slice_is_due(self):
        plan = self.plan()
        due = plan.due_slices(NOW + timedelta(minutes=45))
        assert [item.index for item in due] == [1]

    def test_a_closed_window_is_missed_not_placed_late(self):
        """Dropping a missed interval into the market at once is the opposite of
        what a schedule is for."""
        plan = self.plan()
        missed = plan.missed_slices(NOW + timedelta(minutes=95), placed={0})
        assert [item.index for item in missed] == [1, 2]
        assert plan.due_slices(NOW + timedelta(minutes=95))[0].index == 3

    def test_a_volume_profile_that_does_not_line_up_is_refused(self):
        with pytest.raises(AlgorithmError, match="does not line up"):
            self.plan(window=self.window(expected_volumes=(1.0, 2.0)))

    def test_a_supplied_profile_is_recorded_as_supplied(self):
        plan = self.plan("VWAP", window=self.window(expected_volumes=(1.0, 3.0, 3.0, 1.0)))
        assert plan.volume_profile_supplied
        # Weighted by the profile, not evenly.
        assert plan.schedule.slices[1].quantity > plan.schedule.slices[0].quantity

    def test_a_strategy_that_needs_a_profile_and_has_none_is_refused(self):
        """Falling back to TWAP would answer a different question and label the
        answer with the name of the question."""
        with pytest.raises(AlgorithmError):
            self.plan("VWAP")

    def test_the_schedules_assumptions_travel_with_the_plan(self):
        plan = self.plan()
        assert isinstance(plan.schedule.assumptions, tuple)
        assert plan.to_dict()["schedule"]["strategy"].startswith("TWAP@")

    def test_a_zero_quantity_parent_is_refused(self):
        with pytest.raises(AlgorithmError, match="positive quantity"):
            plan_parent_order(
                parent_client_order_id="p",
                instrument_id=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=Decimal("0"),
                strategy="TWAP",
                window=self.window(),
            )


class TestTheThirdGateIsARejectionNotAnUnknownOutcome:
    """An unverified adapter sends nothing, so the outcome is entirely known.

    Left to raise from inside ``place_order`` it would land in the
    broker-unreachable branch and be reported as an outcome nobody knows, which
    is the one thing that must not be said about an order that was never sent.
    """

    def service(self, *, enabled: bool):
        from types import SimpleNamespace

        from domains.execution.oms.service import OrderManagementService

        settings = SimpleNamespace(
            live_trading_enabled=enabled,
            code_commit="test",
            market_data_provider="upstox",
        )
        return OrderManagementService(None, None, None, settings)

    def account(self, **overrides):
        from domains.execution.brokers.paper_fills import PaperFillPolicy
        from domains.execution.oms.models import OrderVenue
        from domains.execution.oms.risk_gate import RiskLimits
        from domains.execution.oms.service import AccountView
        from domains.research.costs import NO_COST_MODEL

        base = {
            "id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "name": "live",
            "venue": OrderVenue.LIVE,
            "broker": "upstox",
            "base_currency": "INR",
            "cash": Decimal("1000"),
            "opening_cash": Decimal("1000"),
            "cost_schedule": NO_COST_MODEL,
            "fill_policy": PaperFillPolicy.QUOTE_ONLY,
            "max_quote_age_seconds": 30,
            "risk_limits": RiskLimits(),
            "kill_switch_engaged_at": None,
            "kill_switch_reason": None,
            "live_armed_at": NOW,
            "created_at": NOW,
        }
        base.update(overrides)
        return AccountView(**base)

    def test_an_armed_account_with_an_unverified_adapter_is_refused(self):
        refusal = self.service(enabled=True)._live_refusal(
            self.account(), broker(RecordedTransport(), verified=False)
        )
        assert refusal is not None
        assert refusal.observed["verified_against_documentation"] is False
        assert "not been confirmed" in refusal.detail

    def test_a_verified_adapter_on_an_armed_enabled_account_passes(self):
        assert (
            self.service(enabled=True)._live_refusal(
                self.account(), broker(RecordedTransport(), verified=True)
            )
            is None
        )

    def test_a_disabled_deployment_refuses_whatever_the_adapter_says(self):
        refusal = self.service(enabled=False)._live_refusal(
            self.account(), broker(RecordedTransport(), verified=True)
        )
        assert refusal is not None
        assert refusal.observed["setting"] == "live_trading_enabled"

    def test_an_unarmed_account_refuses_whatever_the_adapter_says(self):
        refusal = self.service(enabled=True)._live_refusal(
            self.account(live_armed_at=None), broker(RecordedTransport(), verified=True)
        )
        assert refusal is not None
        assert refusal.observed["live_armed_at"] is None
