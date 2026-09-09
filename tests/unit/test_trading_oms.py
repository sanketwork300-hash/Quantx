"""The order lifecycle, the paper fill engine and the pre-trade gate.

Everything here is pure: no database, no event loop, no market. That is the
point of having split the judgement out of the service — the rules that decide
whether a fill is honest can be read and tested on their own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.execution.brokers.base import BrokerCapability
from domains.execution.brokers.paper import PaperBroker
from domains.execution.brokers.paper_fills import (
    FillContext,
    PaperFillPolicy,
    PriceBasis,
    decide_fill,
)
from domains.execution.oms.models import (
    TRANSITIONS,
    FillFlag,
    IllegalTransition,
    Order,
    OrderError,
    OrderFill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderVenue,
    RejectionReason,
    TimeInForce,
    check_transition,
)
from domains.execution.oms.risk_gate import AccountSnapshot, RiskLimits, evaluate
from domains.market_data.models import Quote
from domains.research.costs import NO_COST_MODEL, TradeCost

NOW = datetime(2026, 3, 2, 6, 30, tzinfo=UTC)
INSTRUMENT = uuid.uuid4()


def quote(
    bid: str | None = "100.00",
    ask: str | None = "100.20",
    bid_size: str | None = None,
    ask_size: str | None = None,
    last: str | None = None,
    age_seconds: float = 0.0,
) -> Quote:
    stamp = NOW - timedelta(seconds=age_seconds)
    return Quote(
        instrument_id=INSTRUMENT,
        exchange_timestamp=stamp,
        receive_timestamp=stamp,
        source="test",
        bid_price=Decimal(bid) if bid is not None else None,
        ask_price=Decimal(ask) if ask is not None else None,
        bid_size=Decimal(bid_size) if bid_size is not None else None,
        ask_size=Decimal(ask_size) if ask_size is not None else None,
        last_price=Decimal(last) if last is not None else None,
    )


def order(
    side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> FillContext:
    return FillContext(
        side=side,
        order_type=order_type,
        remaining_quantity=Decimal(quantity),
        time_in_force=time_in_force,
        limit_price=Decimal(limit_price) if limit_price else None,
    )


class TestTheLifecycle:
    """An order's state is a claim about what happened to it."""

    def test_the_six_states_are_the_six_the_spec_names(self):
        assert {str(item) for item in OrderStatus} == {
            "NEW",
            "ACKNOWLEDGED",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
        }

    def test_a_terminal_order_goes_nowhere(self):
        for status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            assert TRANSITIONS[status] == frozenset()
            assert status.is_terminal
            with pytest.raises(IllegalTransition, match="terminal"):
                check_transition(status, OrderStatus.ACKNOWLEDGED)

    def test_a_partial_fill_may_be_followed_by_another(self):
        """Two child fills are two transitions; collapsing them loses a timestamp."""
        check_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED)

    def test_a_partially_filled_order_cannot_be_rejected(self):
        """It has already traded. A rejection would deny a fill that happened."""
        with pytest.raises(IllegalTransition):
            check_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED)

    def test_an_illegal_transition_names_what_was_possible(self):
        with pytest.raises(IllegalTransition, match="ACKNOWLEDGED"):
            check_transition(OrderStatus.NEW, OrderStatus.NEW)

    def test_a_partially_filled_order_is_still_open(self):
        assert OrderStatus.PARTIALLY_FILLED.is_open
        assert not OrderStatus.FILLED.is_open


class TestTheOrderRequest:
    def test_quantity_is_positive_and_direction_is_the_side(self):
        with pytest.raises(OrderError, match="always positive"):
            OrderRequest(
                instrument_id=INSTRUMENT,
                side=OrderSide.SELL,
                quantity=Decimal("-5"),
                order_type=OrderType.MARKET,
            )

    def test_a_limit_order_needs_a_limit_price(self):
        with pytest.raises(OrderError, match="needs a limit price"):
            OrderRequest(
                instrument_id=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
            )

    def test_a_market_order_with_a_price_is_two_instructions(self):
        """Honouring either one silently is worse than refusing both."""
        with pytest.raises(OrderError, match="two different instructions"):
            OrderRequest(
                instrument_id=INSTRUMENT,
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                limit_price=Decimal("100"),
            )


class TestWhatAPaperFillRestsOn:
    def test_a_buy_pays_the_ask_and_a_sell_receives_the_bid(self):
        buy = decide_fill(order(OrderSide.BUY), quote(), NOW)
        sell = decide_fill(order(OrderSide.SELL), quote(), NOW)
        assert buy.price == Decimal("100.20")
        assert buy.price_basis is PriceBasis.MARKET_ASK
        assert sell.price == Decimal("100.00")
        assert sell.price_basis is PriceBasis.MARKET_BID

    def test_a_market_order_with_no_offer_does_not_fill(self):
        """The rule that makes ``Quote.mid_price`` return None, applied to fills."""
        decision = decide_fill(order(OrderSide.BUY), quote(ask=None, last="100.10"), NOW)
        assert not decision.fills
        assert decision.rejection is RejectionReason.NO_TWO_SIDED_MARKET
        assert "trade print is not a quote" in decision.detail

    def test_filling_at_the_last_trade_has_to_be_asked_for(self):
        decision = decide_fill(
            order(OrderSide.BUY),
            quote(ask=None, last="100.10"),
            NOW,
            policy=PaperFillPolicy.ALLOW_LAST_TRADE,
        )
        assert decision.fills
        assert decision.price_basis is PriceBasis.LAST_TRADE
        assert FillFlag.LAST_TRADE_NOT_A_QUOTE in decision.flags

    def test_the_permissive_policy_still_needs_a_last_trade(self):
        decision = decide_fill(
            order(OrderSide.BUY),
            quote(ask=None, last=None),
            NOW,
            policy=PaperFillPolicy.ALLOW_LAST_TRADE,
        )
        assert decision.rejection is RejectionReason.NO_QUOTE

    def test_a_stale_quote_does_not_fill(self):
        """A fill against an old quote asserts liquidity nobody has published since."""
        decision = decide_fill(order(), quote(age_seconds=120), NOW, max_quote_age_seconds=30)
        assert decision.rejection is RejectionReason.QUOTE_STALE
        assert "120s old" in decision.detail

    def test_the_staleness_tolerance_is_the_callers(self):
        decision = decide_fill(order(), quote(age_seconds=120), NOW, max_quote_age_seconds=300)
        assert decision.fills

    def test_a_full_fill_with_no_published_depth_is_flagged(self):
        """Asserting a complete fill without a size is asserting unseen liquidity."""
        decision = decide_fill(order(quantity="10000"), quote(), NOW)
        assert decision.quantity == Decimal("10000")
        assert FillFlag.DEPTH_NOT_REPORTED in decision.flags

    def test_a_fill_is_capped_by_the_size_on_offer(self):
        decision = decide_fill(order(quantity="500"), quote(ask_size="120"), NOW)
        assert decision.quantity == Decimal("120")
        assert FillFlag.LIMITED_BY_DEPTH in decision.flags
        assert decision.rests

    def test_every_paper_fill_says_it_is_counterfactual(self):
        assert FillFlag.COUNTERFACTUAL in decide_fill(order(), quote(), NOW).flags


class TestLimitOrders:
    def test_a_marketable_limit_pays_the_touch_not_its_own_limit(self):
        """Booking it at the limit would invent cost the market never charged."""
        decision = decide_fill(
            order(order_type=OrderType.LIMIT, limit_price="105.00"), quote(), NOW
        )
        assert decision.price == Decimal("100.20")

    def test_an_unmarketable_day_limit_rests(self):
        decision = decide_fill(order(order_type=OrderType.LIMIT, limit_price="99.00"), quote(), NOW)
        assert not decision.fills
        assert decision.rests
        assert decision.rejection is None

    def test_an_unmarketable_ioc_is_cancelled_not_rejected(self):
        decision = decide_fill(
            order(
                order_type=OrderType.LIMIT,
                limit_price="99.00",
                time_in_force=TimeInForce.IMMEDIATE_OR_CANCEL,
            ),
            quote(),
            NOW,
        )
        assert decision.cancel_remainder
        assert decision.rejection is None

    def test_a_resting_limit_order_survives_a_missing_quote(self):
        """It has not failed; it is doing what it was sent to do."""
        decision = decide_fill(order(order_type=OrderType.LIMIT, limit_price="99.00"), None, NOW)
        assert decision.rests
        assert decision.rejection is None

    def test_a_partially_filled_ioc_does_not_rest(self):
        decision = decide_fill(
            order(
                quantity="500",
                order_type=OrderType.LIMIT,
                limit_price="105.00",
                time_in_force=TimeInForce.IMMEDIATE_OR_CANCEL,
            ),
            quote(ask_size="100"),
            NOW,
        )
        assert decision.quantity == Decimal("100")
        assert not decision.rests


class TestTheGate:
    """Named refusals, and never a silent resize."""

    def make(self, **limits) -> RiskLimits:
        return RiskLimits(**limits)

    def request(self, quantity: str = "10", side: OrderSide = OrderSide.BUY) -> OrderRequest:
        return OrderRequest(
            instrument_id=INSTRUMENT,
            side=side,
            quantity=Decimal(quantity),
            order_type=OrderType.MARKET,
        )

    def account(self, **kwargs) -> AccountSnapshot:
        kwargs.setdefault("cash", Decimal("1000000"))
        return AccountSnapshot(**kwargs)

    def test_every_check_is_reported_even_when_it_passes(self):
        """A gate that reports only failures cannot answer 'was this checked?'."""
        decision = evaluate(
            self.request(), self.make(), self.account(), quote(), venue=OrderVenue.PAPER
        )
        assert decision.allowed
        names = {check.name for check in decision.checks}
        assert {"order_notional", "position_quantity", "gross_exposure", "daily_loss"} <= names
        assert all(check.not_configured for check in decision.checks if check.name == "daily_loss")

    def test_an_order_over_the_notional_limit_is_refused_whole(self):
        decision = evaluate(
            self.request("100"),
            self.make(max_order_notional=Decimal("500")),
            self.account(),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert decision.rejection.reason is RejectionReason.ORDER_NOTIONAL_LIMIT
        assert decision.rejection.observed["limit"] == "500"

    def test_a_position_limit_refuses_rather_than_trims(self):
        decision = evaluate(
            self.request("60"),
            self.make(max_position_quantity=Decimal("50")),
            self.account(positions={INSTRUMENT: Decimal("0")}),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert "refused whole rather than cut to fit" in decision.rejection.detail

    def test_the_position_check_looks_at_where_the_order_would_leave_it(self):
        decision = evaluate(
            self.request("10", OrderSide.SELL),
            self.make(max_position_quantity=Decimal("50")),
            self.account(positions={INSTRUMENT: Decimal("-45")}),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert decision.rejection.observed["after"] == "-55"

    def test_every_breach_is_listed_not_only_the_first(self):
        """Fixing limits one at a time is what a short-circuiting gate forces."""
        decision = evaluate(
            self.request("100"),
            self.make(
                max_order_notional=Decimal("100"),
                max_position_quantity=Decimal("5"),
                max_gross_exposure=Decimal("100"),
            ),
            self.account(),
            quote(),
            venue=OrderVenue.PAPER,
        )
        failed = [check.name for check in decision.checks if not check.passed]
        assert set(failed) == {"order_notional", "position_quantity", "gross_exposure"}
        assert decision.rejection.reason is RejectionReason.ORDER_NOTIONAL_LIMIT

    def test_an_unpriced_position_is_named_not_counted_as_flat(self):
        other = uuid.uuid4()
        decision = evaluate(
            self.request(),
            self.make(max_gross_exposure=Decimal("10000000")),
            self.account(positions={other: Decimal("100")}, prices={}),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert other in decision.unpriced
        gross = next(check for check in decision.checks if check.name == "gross_exposure")
        assert gross.observed["all_positions_priced"] is False

    def test_a_loss_limit_with_no_loss_supplied_refuses(self):
        """An unmeasured limit is not a limit."""
        decision = evaluate(
            self.request(),
            self.make(max_daily_loss=Decimal("1000")),
            self.account(session_loss=None),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert decision.rejection.reason is RejectionReason.DAILY_LOSS_LIMIT

    def test_the_price_band_is_measured_against_the_quote_mid(self):
        request = OrderRequest(
            instrument_id=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("150.00"),
        )
        decision = evaluate(
            request,
            self.make(max_price_deviation=Decimal("0.10")),
            self.account(),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert decision.rejection.reason is RejectionReason.PRICE_BAND
        assert decision.rejection.observed["anchor_basis"] == "QUOTE_MID"

    def test_the_band_is_not_applied_without_a_two_sided_quote(self):
        """It reports that it could not run rather than passing silently."""
        request = OrderRequest(
            instrument_id=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("150.00"),
        )
        decision = evaluate(
            request,
            self.make(max_price_deviation=Decimal("0.10")),
            self.account(),
            quote(bid=None),
            venue=OrderVenue.PAPER,
        )
        band = next(check for check in decision.checks if check.name == "price_band")
        assert band.passed and band.not_configured

    def test_the_kill_switch_refuses_before_anything_else(self):
        decision = evaluate(
            self.request(),
            self.make(),
            self.account(),
            quote(),
            venue=OrderVenue.PAPER,
            kill_switch_engaged=True,
            kill_switch_reason="a strategy ran away",
        )
        assert not decision.allowed
        assert decision.rejection.reason is RejectionReason.KILL_SWITCH_ENGAGED
        assert "ran away" in decision.rejection.detail

    def test_a_paper_account_may_run_without_limits(self):
        decision = evaluate(
            self.request(), self.make(), self.account(), quote(), venue=OrderVenue.PAPER
        )
        assert decision.allowed

    def test_a_live_account_may_not(self):
        """An unlimited live account is not a decision anybody makes deliberately."""
        decision = evaluate(
            self.request(), self.make(), self.account(), quote(), venue=OrderVenue.LIVE
        )
        assert not decision.allowed
        assert decision.rejection.reason is RejectionReason.NO_RISK_LIMITS

    def test_the_cash_check_says_it_is_not_a_margin_check(self):
        decision = evaluate(
            self.request("10000"),
            self.make(require_sufficient_cash=True),
            self.account(cash=Decimal("100")),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert "not a margin check" in decision.rejection.detail

    def test_a_notional_limit_with_no_price_refuses(self):
        decision = evaluate(
            self.request(),
            self.make(max_order_notional=Decimal("500")),
            self.account(),
            None,
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert "no price to measure against" in decision.rejection.detail

    def test_a_multiplier_that_was_assumed_is_named(self):
        decision = evaluate(
            self.request(), self.make(), self.account(), quote(), venue=OrderVenue.PAPER
        )
        assert INSTRUMENT in decision.multipliers_assumed

    def test_a_recorded_multiplier_scales_the_notional(self):
        decision = evaluate(
            self.request("10"),
            self.make(max_order_notional=Decimal("5000")),
            self.account(multipliers={INSTRUMENT: Decimal("50")}),
            quote(),
            venue=OrderVenue.PAPER,
        )
        assert not decision.allowed
        assert decision.rejection.observed["notional"] == "50050.00"


class TestSlippageNeedsABaseline:
    def fill(self, price: str, reference: str | None) -> OrderFill:
        return OrderFill(
            id=uuid.uuid4(),
            order_id=uuid.uuid4(),
            quantity=Decimal("10"),
            price=Decimal(price),
            filled_at=NOW,
            price_basis="MARKET_ASK",
            cost=TradeCost(modelled=False),
            reference_price=Decimal(reference) if reference else None,
            reference_basis="QUOTE_MID" if reference else None,
        )

    def test_with_no_reference_there_is_no_number(self):
        """Slippage against nothing is not a measurement."""
        assert self.fill("100.20", None).slippage_against_reference is None

    def test_a_buy_above_the_mid_has_paid_away(self):
        assert self.fill("100.20", "100.10").slippage_against_reference == Decimal("1.00")

    def test_an_order_with_no_decision_price_reports_no_slippage(self):
        built = Order(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            instrument_id=INSTRUMENT,
            client_order_id="c1",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            venue=OrderVenue.PAPER,
            broker="paper",
            created_at=NOW,
            updated_at=NOW,
            fills=(self.fill("100.20", "100.10"),),
        )
        assert built.slippage_against_decision is None


class TestTheBrokerInterface:
    def test_the_paper_broker_does_not_claim_to_modify(self):
        """An amendment's effect is a queue position, and there is no queue."""
        broker = PaperBroker(quotes=None)
        assert not broker.supports(BrokerCapability.MODIFY)

    def test_an_unsupported_instruction_is_named_before_it_is_sent(self):
        broker = PaperBroker(quotes=None)
        broker.capabilities = frozenset({BrokerCapability.LIMIT_ORDERS})
        request = OrderRequest(
            instrument_id=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Decimal("1"),
            order_type=OrderType.MARKET,
        )
        assert broker.missing_capability(request) is BrokerCapability.MARKET_ORDERS

    def test_the_paper_broker_reports_no_positions_of_its_own(self):
        """A reconciliation that compared our number to our number would agree always."""
        import asyncio

        broker = PaperBroker(quotes=None)
        assert asyncio.run(broker.get_positions()) == []


class TestCostsAreNeverInvented:
    def test_with_no_schedule_a_trade_is_gross_and_says_so(self):
        cost = NO_COST_MODEL.charge(Decimal("100"), Decimal("10"), is_buy=True)
        assert cost.total == Decimal(0)
        assert cost.modelled is False
