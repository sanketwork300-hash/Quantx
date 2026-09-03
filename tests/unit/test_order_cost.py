"""The forward order-cost estimate, and the convention it shares with Phase 8.

The Phase 8 simulator prices a schedule against a path the market printed; the
Phase 11 estimator prices one against a reference held flat because the future
has no path. They must agree wherever the two questions coincide — on a flat
path — or the platform has two notions of what an order costs and only one of
them is tested.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.execution.benchmarks import MarketObservation, MarketWindow
from domains.execution.impact import SquareRootImpactModel
from domains.execution.models import OrderType, Side
from domains.execution.orders import (
    IMMEDIATE,
    Marketability,
    OrderCostError,
    OrderCostRequest,
    OrderCostWarning,
    estimate_order_cost,
)
from domains.execution.simulation import simulate
from domains.execution.strategies import build_strategy, uniform_intervals

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
REFERENCE = Decimal("100.00")
SPREAD = Decimal("0.40")


def request(**overrides) -> OrderCostRequest:
    defaults = {
        "instrument_id": uuid.uuid4(),
        "side": Side.BUY,
        "quantity": Decimal(600),
        "multiplier": Decimal(1),
        "currency": "INR",
        "reference_price": REFERENCE,
        "as_of": AS_OF,
        "bid": REFERENCE - SPREAD / 2,
        "ask": REFERENCE + SPREAD / 2,
        "horizon_seconds": 1800.0,
        "intervals": 6,
        "strategies": (IMMEDIATE, "TWAP"),
        "volatility": 0.25,
        "average_daily_volume": 100_000.0,
    }
    return OrderCostRequest(**{**defaults, **overrides})


class TestOneConvention:
    """The estimate and the simulator price the same schedule the same way."""

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    def test_the_forward_estimate_agrees_with_the_simulator_on_a_flat_path(self, side):
        ask = request(side=side)
        context = _context(ask)
        schedule = build_strategy("TWAP").generate_schedule(ask.quantity, ask.side, context)
        impact = SquareRootImpactModel()

        window = MarketWindow(
            instrument_id=ask.instrument_id,
            start=ask.as_of,
            end=ask.as_of + timedelta(seconds=ask.horizon_seconds),
            observations=tuple(
                MarketObservation(
                    timestamp=item.start,
                    price=REFERENCE,
                    source="flat",
                    spread=SPREAD,
                )
                for item in schedule.slices
            ),
            source="flat",
            staleness_tolerance_seconds=ask.horizon_seconds * 10,
        )
        simulated = simulate(schedule, window, context, impact)
        estimated = estimate_order_cost(ask, impact)
        twap = next(item for item in estimated.strategies if item.strategy.startswith("TWAP"))

        assert len(simulated.fills) == len(twap.slices) == len(schedule.slices)
        for fill, priced in zip(simulated.fills, twap.slices, strict=True):
            assert fill.fill_price == priced.fill_price
            assert fill.temporary_impact_per_unit == priced.temporary_impact_per_unit
            assert fill.permanent_impact_per_unit == priced.permanent_impact_per_unit
            assert fill.spread_cost_per_unit == priced.spread_cost_per_unit


class TestTheCostIsHandCheckable:
    def test_an_immediate_order_with_no_impact_pays_exactly_the_half_spread(self):
        """Zero coefficients leave the spread, which is arithmetic anyone can do."""
        ask = request(quantity=Decimal(100), strategies=(IMMEDIATE,))
        estimate = estimate_order_cost(ask, SquareRootImpactModel(0.0, 0.0))
        immediate = estimate.strategies[0]

        assert immediate.estimated_slippage_per_unit == SPREAD / 2
        assert immediate.estimated_slippage_currency == pytest.approx(float(SPREAD / 2 * 100))
        assert immediate.estimated_slippage_basis_points == pytest.approx(20.0)
        assert immediate.impact_component_currency == pytest.approx(0.0)

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    def test_a_cost_is_positive_whichever_way_the_order_goes(self, side):
        estimate = estimate_order_cost(request(side=side), SquareRootImpactModel())
        for priced in estimate.strategies:
            assert priced.estimated_slippage_currency > 0
            assert priced.spread_component_currency > 0
            assert priced.impact_component_currency > 0

    def test_the_multiplier_scales_the_currency_cost_and_nothing_else(self):
        one = estimate_order_cost(request(), SquareRootImpactModel()).strategies[0]
        many = estimate_order_cost(
            request(multiplier=Decimal(75)), SquareRootImpactModel()
        ).strategies[0]
        assert many.estimated_slippage_per_unit == one.estimated_slippage_per_unit
        assert many.estimated_slippage_currency == pytest.approx(
            75 * one.estimated_slippage_currency
        )
        assert many.estimated_slippage_basis_points == pytest.approx(
            one.estimated_slippage_basis_points
        )

    def test_this_model_does_not_say_that_working_an_order_is_cheaper(self):
        """The two impact terms move in opposite directions as a schedule is split.

        Permanent impact is evaluated per slice and accumulates, so it grows
        with the slice count; temporary impact is paid on the slice and shrinks.
        Neither wins by construction — it depends on coefficients nobody here
        has calibrated — and this test exists so that a change making one of
        them dominate silently cannot pass as "slicing is cheaper".
        """

        def twap(estimate) -> float:
            return next(
                item for item in estimate.strategies if item.strategy.startswith("TWAP")
            ).impact_component_currency

        permanent_only = SquareRootImpactModel(1.0, 0.0)
        assert twap(estimate_order_cost(request(intervals=12), permanent_only)) > twap(
            estimate_order_cost(request(intervals=1), permanent_only)
        )

        temporary_only = SquareRootImpactModel(0.0, 1.0)
        assert twap(estimate_order_cost(request(intervals=12), temporary_only)) < twap(
            estimate_order_cost(request(intervals=1), temporary_only)
        )


class TestWhatCannotBeEstimated:
    def test_without_a_daily_volume_the_impact_is_absent_and_so_is_the_total(self):
        estimate = estimate_order_cost(request(average_daily_volume=0.0), SquareRootImpactModel())
        assert OrderCostWarning.NO_ADV in estimate.warnings
        for priced in estimate.strategies:
            assert priced.impact_component_currency is None
            assert priced.estimated_slippage_currency is None
            assert priced.estimated_slippage_per_unit is None
            # The measurable half is still measured.
            assert priced.spread_component_currency == pytest.approx(
                float(SPREAD / 2 * priced.slices[0].quantity * len(priced.slices))
            )

    def test_without_a_volatility_the_impact_is_absent_rather_than_zero(self):
        """A zero impact would read as an order that moves nothing."""
        estimate = estimate_order_cost(request(volatility=0.0), SquareRootImpactModel())
        assert OrderCostWarning.NO_VOLATILITY in estimate.warnings
        for priced in estimate.strategies:
            assert priced.impact_component_currency is None
            assert priced.estimated_slippage_currency is None

    def test_without_a_two_sided_quote_the_spread_is_absent_and_so_is_the_total(self):
        estimate = estimate_order_cost(request(bid=None, ask=None), SquareRootImpactModel())
        assert OrderCostWarning.NO_SPREAD in estimate.warnings
        for priced in estimate.strategies:
            assert priced.spread_component_currency is None
            assert priced.estimated_slippage_currency is None
            assert priced.impact_component_currency is not None

    def test_a_strategy_that_cannot_be_built_is_listed_with_its_reason(self):
        estimate = estimate_order_cost(
            request(strategies=(IMMEDIATE, "POV")), SquareRootImpactModel()
        )
        assert [name for name, _ in estimate.unavailable] == ["POV"]
        reason = dict(estimate.unavailable)["POV"]
        assert "volume forecast" in reason
        assert OrderCostWarning.STRATEGY_UNAVAILABLE in estimate.warnings

    def test_an_order_for_nothing_is_refused(self):
        with pytest.raises(OrderCostError):
            request(quantity=Decimal(0))

    def test_a_negative_quantity_is_refused_rather_than_read_as_a_sell(self):
        with pytest.raises(OrderCostError):
            request(quantity=Decimal(-10))


class TestMarketability:
    def test_a_market_order_crosses(self):
        estimate = estimate_order_cost(request(), SquareRootImpactModel())
        assert estimate.marketability is Marketability.MARKETABLE
        assert OrderCostWarning.PASSIVE_FILL_NOT_MODELLED not in estimate.warnings

    @pytest.mark.parametrize(
        ("side", "limit", "expected"),
        [
            (Side.BUY, REFERENCE + SPREAD, Marketability.MARKETABLE),
            (Side.BUY, REFERENCE - SPREAD, Marketability.PASSIVE),
            (Side.SELL, REFERENCE - SPREAD, Marketability.MARKETABLE),
            (Side.SELL, REFERENCE + SPREAD, Marketability.PASSIVE),
        ],
    )
    def test_a_limit_is_classified_against_the_touch_it_would_have_to_cross(
        self, side, limit, expected
    ):
        estimate = estimate_order_cost(
            request(side=side, order_type=OrderType.LIMIT, limit_price=limit),
            SquareRootImpactModel(),
        )
        assert estimate.marketability is expected
        assert str(limit) in estimate.marketability_basis

    def test_a_passive_order_says_its_fill_is_not_modelled(self):
        estimate = estimate_order_cost(
            request(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=REFERENCE - SPREAD),
            SquareRootImpactModel(),
        )
        assert OrderCostWarning.PASSIVE_FILL_NOT_MODELLED in estimate.warnings
        assert any("queue" in item for item in estimate.assumptions)

    def test_without_a_two_sided_quote_marketability_is_unknown_not_assumed(self):
        estimate = estimate_order_cost(
            request(
                bid=None,
                ask=None,
                order_type=OrderType.LIMIT,
                limit_price=REFERENCE,
            ),
            SquareRootImpactModel(),
        )
        assert estimate.marketability is Marketability.UNKNOWN


class TestLanguage:
    def test_nothing_in_the_payload_ranks_or_recommends_a_schedule(self):
        payload = estimate_order_cost(request(), SquareRootImpactModel()).to_dict(
            include_slices=True
        )
        blob = str(payload).lower()
        for phrase in ("optimal", "recommend", "best", "should use", "advis"):
            assert phrase not in blob, phrase
        assert "not ranked" in payload["interpretation"]

    def test_every_result_says_it_is_a_counterfactual_on_a_flat_reference(self):
        estimate = estimate_order_cost(request(), SquareRootImpactModel())
        assert OrderCostWarning.COUNTERFACTUAL in estimate.warnings
        assert OrderCostWarning.FLAT_REFERENCE in estimate.warnings
        assert "has not been placed" in estimate.to_dict()["caveat"]

    def test_an_uncalibrated_coefficient_is_flagged_on_every_schedule(self):
        estimate = estimate_order_cost(request(), SquareRootImpactModel())
        assert OrderCostWarning.IMPACT_NOT_CALIBRATED in estimate.warnings
        for priced in estimate.strategies:
            assert OrderCostWarning.IMPACT_NOT_CALIBRATED in priced.warnings

    def test_a_calibrated_coefficient_is_not_flagged(self):
        estimate = estimate_order_cost(request(), SquareRootImpactModel(0.5, 0.7))
        assert OrderCostWarning.IMPACT_NOT_CALIBRATED not in estimate.warnings


def _context(ask: OrderCostRequest):
    from domains.execution.strategies import MarketContext

    return MarketContext(
        intervals=uniform_intervals(
            ask.as_of,
            ask.as_of + timedelta(seconds=ask.horizon_seconds),
            ask.intervals,
            ask.expected_volumes,
        ),
        reference_price=ask.reference_price,
        volatility=ask.volatility,
        average_daily_volume=ask.average_daily_volume,
        lot_size=ask.lot_size,
    )
