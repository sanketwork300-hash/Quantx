"""Impact models, schedules and the counterfactual simulator.

Carries the Phase 8 acceptance criteria from docs/backlog.md: every simulated
result is labelled a counterfactual estimate; schedules sum to the parent
quantity; impact models are unit-tested against closed-form expectations.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from domains.execution.benchmarks import MarketObservation, MarketWindow
from domains.execution.impact import (
    UNCALIBRATED_COEFFICIENT,
    ImpactError,
    ImpactWarning,
    LinearImpactModel,
    SquareRootImpactModel,
    ZeroImpactModel,
    build_impact_model,
)
from domains.execution.models import Side
from domains.execution.simulation import (
    COUNTERFACTUAL,
    SimulationWarning,
    compare,
    simulate,
)
from domains.execution.strategies import (
    LiquidityAdaptiveStrategy,
    MarketContext,
    POVStrategy,
    ScheduleError,
    StrategyUnavailable,
    TWAPStrategy,
    VWAPStrategy,
    allocate,
    build_strategy,
    uniform_intervals,
)

INSTRUMENT = uuid.uuid4()
T0 = datetime(2026, 9, 24, 9, 15, tzinfo=UTC)
T1 = T0 + timedelta(hours=6)

#: A U-shaped intraday profile — the shape that makes VWAP differ from TWAP.
VOLUMES = [30_000.0, 12_000.0, 8_000.0, 7_000.0, 9_000.0, 25_000.0]

#: A path drifting up 0.10 every five minutes, with a constant 0.20 spread, so
#: every simulated number below can be traced back to something exact.
PATH = tuple(
    MarketObservation(
        timestamp=T0 + timedelta(minutes=5 * k),
        price=Decimal("100") + Decimal("0.10") * k,
        source="synthetic",
        volume=Decimal(5_000),
        spread=Decimal("0.20"),
    )
    for k in range(73)
)


def window(observations=PATH, tolerance: float = 300.0) -> MarketWindow:
    return MarketWindow(
        instrument_id=INSTRUMENT,
        start=T0,
        end=T1,
        observations=observations,
        source="synthetic",
        staleness_tolerance_seconds=tolerance,
    )


def context(volumes=VOLUMES, spreads=None, vols=None, lot: str = "100") -> MarketContext:
    intervals = uniform_intervals(T0, T1, 6, volumes)
    if spreads or vols:
        from domains.execution.strategies import IntervalContext

        intervals = tuple(
            IntervalContext(
                start=item.start,
                end=item.end,
                expected_volume=item.expected_volume,
                spread=spreads[index] if spreads else None,
                volatility=vols[index] if vols else None,
            )
            for index, item in enumerate(intervals)
        )
    return MarketContext(
        intervals=intervals,
        reference_price=Decimal(100),
        volatility=0.18,
        average_daily_volume=500_000.0,
        lot_size=Decimal(lot),
    )


class TestImpactAgainstClosedForms:
    """Phase 8 acceptance: unit-tested against what the formulas say."""

    @pytest.mark.parametrize(
        ("quantity", "adv", "volatility"),
        [(10_000, 1_000_000, 0.20), (250, 50_000, 0.35), (400_000, 1_000_000, 0.15)],
    )
    def test_the_square_root_law_is_the_square_root_law(self, quantity, adv, volatility):
        estimate = SquareRootImpactModel(0.3, 0.2).estimate(
            quantity=quantity,
            average_daily_volume=adv,
            volatility=volatility,
            reference_price=100.0,
        )
        ratio = quantity / adv
        assert estimate.permanent == pytest.approx(0.3 * volatility * math.sqrt(ratio))
        assert estimate.temporary == pytest.approx(0.2 * volatility * math.sqrt(ratio))

    @pytest.mark.parametrize(
        ("quantity", "adv", "volatility"),
        [(10_000, 1_000_000, 0.20), (250, 50_000, 0.35)],
    )
    def test_the_linear_model_is_linear(self, quantity, adv, volatility):
        estimate = LinearImpactModel(0.3, 0.2).estimate(
            quantity=quantity,
            average_daily_volume=adv,
            volatility=volatility,
            reference_price=100.0,
        )
        ratio = quantity / adv
        assert estimate.permanent == pytest.approx(0.3 * volatility * ratio)
        assert estimate.temporary == pytest.approx(0.2 * volatility * ratio)

    def test_impact_scales_with_the_square_root_of_size_not_with_size(self):
        model = SquareRootImpactModel(0.3, 0.2)
        small = model.estimate(10_000, 1_000_000, 0.2, 100.0).permanent
        quadruple = model.estimate(40_000, 1_000_000, 0.2, 100.0).permanent
        assert quadruple == pytest.approx(2.0 * small)

    def test_the_two_models_agree_only_where_the_ratio_is_one(self):
        square = SquareRootImpactModel(0.3, 0.2)
        linear = LinearImpactModel(0.3, 0.2)
        at_one = (
            square.estimate(1_000_000, 1_000_000, 0.2, 100.0).permanent,
            linear.estimate(1_000_000, 1_000_000, 0.2, 100.0).permanent,
        )
        assert at_one[0] == pytest.approx(at_one[1])
        # Below full participation the concave model says more, not less.
        assert (
            square.estimate(10_000, 1_000_000, 0.2, 100.0).permanent
            > linear.estimate(10_000, 1_000_000, 0.2, 100.0).permanent
        )

    def test_price_units_are_the_fraction_times_the_reference(self):
        estimate = SquareRootImpactModel(0.3, 0.2).estimate(10_000, 1_000_000, 0.2, 250.0)
        assert estimate.total_price == pytest.approx(estimate.total * 250.0)
        assert estimate.to_dict()["total_basis_points"] == pytest.approx(estimate.total * 10_000)

    def test_zero_impact_is_zero_and_says_it_is_not_a_claim_that_trading_is_free(self):
        estimate = ZeroImpactModel().estimate(10_000, 1_000_000, 0.2, 100.0)
        assert estimate.total == 0.0
        assert "not a claim that trading is free" in estimate.basis

    def test_the_participation_rate_drives_the_temporary_term_only(self):
        model = SquareRootImpactModel(0.3, 0.2)
        slow = model.estimate(10_000, 1_000_000, 0.2, 100.0, participation=0.01)
        fast = model.estimate(10_000, 1_000_000, 0.2, 100.0, participation=0.25)
        assert fast.temporary > slow.temporary
        assert fast.permanent == pytest.approx(slow.permanent)


class TestImpactRefusesToInvent:
    def test_the_default_coefficient_is_the_identity_and_is_flagged(self):
        estimate = SquareRootImpactModel().estimate(10_000, 1_000_000, 0.2, 100.0)
        assert estimate.parameters["permanent_coefficient"] == UNCALIBRATED_COEFFICIENT
        assert ImpactWarning.NOT_CALIBRATED in estimate.warnings
        assert estimate.is_calibrated is False

    def test_supplying_a_coefficient_clears_the_flag(self):
        estimate = SquareRootImpactModel(0.31, 0.14).estimate(10_000, 1_000_000, 0.2, 100.0)
        assert ImpactWarning.NOT_CALIBRATED not in estimate.warnings
        assert estimate.is_calibrated is True

    def test_the_result_says_it_is_a_model_not_a_measurement(self):
        payload = SquareRootImpactModel().estimate(10_000, 1_000_000, 0.2, 100.0).to_dict()
        assert "not a measurement of what it did" in payload["caveat"]

    def test_the_basis_states_every_number_that_went_into_it(self):
        basis = SquareRootImpactModel(0.3, 0.2).estimate(10_000, 1_000_000, 0.2, 100.0).basis
        assert "eta=0.3" in basis and "gamma=0.2" in basis and "sigma=0.2" in basis

    def test_a_day_that_traded_nothing_has_no_ratio_to_be_large_relative_to(self):
        with pytest.raises(ImpactError, match="must be positive"):
            SquareRootImpactModel().estimate(10_000, 0, 0.2, 100.0)

    def test_an_order_larger_than_the_day_is_flagged(self):
        estimate = SquareRootImpactModel(0.3, 0.2).estimate(2_000_000, 1_000_000, 0.2, 100.0)
        assert ImpactWarning.PARTICIPATION_ABOVE_ONE in estimate.warnings

    def test_a_negative_coefficient_is_refused(self):
        with pytest.raises(ImpactError, match="cannot be negative"):
            SquareRootImpactModel(-0.1)

    def test_an_unknown_model_is_refused_with_the_available_ones(self):
        with pytest.raises(ImpactError, match="unknown impact model"):
            build_impact_model("Almgren")


class TestSchedulesSumExactly:
    """Phase 8 acceptance: slices sum to the parent quantity."""

    def test_twap_needs_nothing_and_sums(self):
        schedule = TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())
        assert sum(item.quantity for item in schedule.slices) == Decimal(20_000)

    def test_vwap_follows_the_profile_and_sums(self):
        schedule = VWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())
        assert sum(item.quantity for item in schedule.slices) == Decimal(20_000)
        quantities = [item.quantity for item in schedule.slices]
        # The U-shaped profile must produce a U-shaped schedule.
        assert quantities[0] > quantities[3] < quantities[-1]

    def test_liquidity_adaptive_shifts_toward_the_cheap_intervals(self):
        schedule = LiquidityAdaptiveStrategy().generate_schedule(
            Decimal(20_000),
            Side.BUY,
            context(spreads=[1.0, 2.0, 4.0, 4.0, 2.0, 1.0], vols=[0.18] * 6),
        )
        assert sum(item.quantity for item in schedule.slices) == Decimal(20_000)
        quantities = [item.quantity for item in schedule.slices]
        assert quantities[0] > quantities[2]

    @given(
        weights=st.lists(
            st.floats(min_value=0.0, max_value=1000.0, allow_nan=False), min_size=1, max_size=12
        ).filter(lambda values: sum(values) > 0),
        total=st.integers(min_value=1, max_value=1_000_000),
        lot=st.sampled_from([1, 5, 25, 75, 100]),
    )
    @settings(max_examples=200, deadline=None)
    def test_allocation_is_exact_for_any_weights_and_lot_size(self, weights, total, lot):
        # An order smaller than one lot has its own test; here the interest is
        # whether the remainder is distributed without losing anything.
        assume(total >= lot)
        quantity = Decimal(total)
        allocation = allocate(quantity, weights, Decimal(lot))
        assert sum(allocation, Decimal(0)) == quantity
        assert all(item >= 0 for item in allocation)

    @given(
        quantity=st.integers(min_value=100, max_value=500_000),
        intervals=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_every_twap_schedule_sums_to_its_parent(self, quantity, intervals):
        ctx = MarketContext(
            intervals=uniform_intervals(T0, T1, intervals),
            reference_price=Decimal(100),
            volatility=0.18,
            average_daily_volume=500_000.0,
            lot_size=Decimal(100),
        )
        schedule = TWAPStrategy().generate_schedule(Decimal(quantity), Side.BUY, ctx)
        assert sum(item.quantity for item in schedule.slices) == Decimal(quantity)

    def test_an_order_smaller_than_one_lot_is_refused(self):
        with pytest.raises(ScheduleError, match="smaller than one lot"):
            allocate(Decimal(50), [1.0, 1.0], Decimal(100))

    def test_a_negative_weight_is_not_a_share_of_an_order(self):
        with pytest.raises(ScheduleError, match="negative weight"):
            allocate(Decimal(100), [1.0, -1.0], Decimal(1))

    def test_weights_that_sum_to_zero_allocate_nothing(self):
        with pytest.raises(ScheduleError, match="sum to zero"):
            allocate(Decimal(100), [0.0, 0.0], Decimal(1))


class TestStrategiesRefuseRatherThanDegrade:
    def test_vwap_without_a_profile_refuses(self):
        with pytest.raises(StrategyUnavailable, match="TWAP under"):
            VWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context(volumes=None))

    def test_vwap_on_a_flat_profile_refuses_because_it_would_be_twap(self):
        with pytest.raises(StrategyUnavailable, match="identical to a time-weighted"):
            VWAPStrategy().generate_schedule(
                Decimal(20_000), Side.BUY, context(volumes=[10_000.0] * 6)
            )

    def test_pov_refuses_when_the_window_cannot_absorb_the_order(self):
        with pytest.raises(StrategyUnavailable, match="short of the"):
            POVStrategy(0.01).generate_schedule(Decimal(20_000), Side.BUY, context())

    def test_pov_succeeds_when_it_can_and_stays_under_its_rate(self):
        schedule = POVStrategy(0.30).generate_schedule(Decimal(20_000), Side.BUY, context())
        assert sum(item.quantity for item in schedule.slices) == Decimal(20_000)
        assert schedule.peak_participation is not None

    def test_pov_says_that_it_coincides_with_vwap_here(self):
        """The two produce identical numbers, which reads as a bug unless said."""
        ctx = context()
        pov = POVStrategy(0.30).generate_schedule(Decimal(20_000), Side.BUY, ctx)
        vwap = VWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, ctx)

        assert [item.quantity for item in pov.slices] == [item.quantity for item in vwap.slices]
        assert any("coincide here" in item for item in pov.assumptions)

    def test_liquidity_adaptive_without_a_modulating_signal_refuses(self):
        with pytest.raises(StrategyUnavailable, match="credit inputs that did nothing"):
            LiquidityAdaptiveStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())

    def test_liquidity_adaptive_on_flat_signals_refuses_because_it_would_be_vwap(self):
        with pytest.raises(StrategyUnavailable, match="this is\\s+VWAP"):
            LiquidityAdaptiveStrategy().generate_schedule(
                Decimal(20_000), Side.BUY, context(spreads=[2.0] * 6, vols=[0.18] * 6)
            )

    def test_every_refusal_names_the_strategy_and_gives_a_reason(self):
        try:
            VWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context(volumes=None))
        except StrategyUnavailable as exc:
            assert exc.strategy.startswith("VWAP@")
            assert len(exc.reason) > 40

    def test_a_bad_participation_rate_is_refused(self):
        for rate in (0.0, -0.1, 1.5):
            with pytest.raises(ScheduleError, match="not a share of"):
                POVStrategy(rate)

    def test_an_unknown_strategy_is_refused(self):
        with pytest.raises(ScheduleError, match="unknown execution strategy"):
            build_strategy("AlmgrenChriss")

    def test_a_high_participation_schedule_says_the_impact_model_is_out_of_range(self):
        schedule = POVStrategy(0.9).generate_schedule(Decimal(70_000), Side.BUY, context())
        assert "SCHEDULE_HIGH_PARTICIPATION_RATE" in schedule.warnings
        assert any("say nothing reliable" in item for item in schedule.assumptions)


class TestEverySimulationIsLabelled:
    """Phase 8 acceptance: labelled a counterfactual estimate, every time."""

    @pytest.fixture
    def schedule(self):
        return TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())

    def test_the_label_is_on_the_result(self, schedule):
        result = simulate(schedule, window(), context(), ZeroImpactModel())
        assert result.is_counterfactual
        assert COUNTERFACTUAL in result.warnings

    def test_the_caveat_is_in_the_payload_not_in_a_footnote(self, schedule):
        payload = simulate(schedule, window(), context(), ZeroImpactModel()).to_dict()
        assert payload["counterfactual"] is True
        assert "never executed" in payload["caveat"]
        assert "would itself have moved that path" in payload["caveat"]

    def test_a_comparison_is_labelled_and_says_it_is_not_a_ranking(self, schedule):
        comparison = compare(
            {"TWAP": schedule}, {}, window(), context(), ZeroImpactModel()
        ).to_dict()
        assert comparison["counterfactual"] is True
        assert "no strategy is recommended" in comparison["comparison_caveat"]
        assert "not a ranking" in comparison["comparison_caveat"]

    @given(latency=st.floats(min_value=0.0, max_value=600.0))
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_the_label_survives_every_path_through_the_simulator(self, schedule, latency):
        result = simulate(
            schedule, window(), context(), SquareRootImpactModel(), latency_seconds=latency
        )
        assert COUNTERFACTUAL in result.warnings


class TestSimulationMechanics:
    @pytest.fixture
    def schedule(self):
        return TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())

    def test_with_no_impact_the_fill_is_the_observed_price_plus_half_the_spread(self, schedule):
        result = simulate(schedule, window(), context(), ZeroImpactModel())
        for fill in result.fills:
            assert fill.fill_price == fill.observed_price + Decimal("0.10")
            assert fill.drifted_price == fill.observed_price

    def test_a_sell_pays_the_spread_the_other_way(self):
        schedule = TWAPStrategy().generate_schedule(Decimal(20_000), Side.SELL, context())
        result = simulate(schedule, window(), context(), ZeroImpactModel())
        for fill in result.fills:
            assert fill.fill_price == fill.observed_price - Decimal("0.10")

    def test_impact_makes_a_buy_more_expensive_and_a_sell_cheaper(self):
        buy = TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())
        sell = TWAPStrategy().generate_schedule(Decimal(20_000), Side.SELL, context())
        model = SquareRootImpactModel(0.3, 0.2)

        assert (
            simulate(buy, window(), context(), model).average_price
            > simulate(buy, window(), context(), ZeroImpactModel()).average_price
        )
        assert (
            simulate(sell, window(), context(), model).average_price
            < simulate(sell, window(), context(), ZeroImpactModel()).average_price
        )

    def test_permanent_impact_accumulates_across_slices(self, schedule):
        result = simulate(schedule, window(), context(), SquareRootImpactModel(0.3, 0.0))
        drift = [fill.drifted_price - fill.observed_price for fill in result.fills]
        assert drift[0] == 0
        assert drift == sorted(drift)
        assert drift[-1] > 0

    def test_a_tiny_impact_does_not_round_away_to_nothing(self, schedule):
        result = simulate(schedule, window(), context(), SquareRootImpactModel(0.0001, 0.0001))
        assert result.modelled_impact_cost > 0

    def test_the_simulated_fills_are_scored_by_the_phase_7_machinery(self, schedule):
        result = simulate(schedule, window(), context(), ZeroImpactModel())
        assert result.analysis is not None
        assert result.analysis.primary_shortfall is not None
        assert len(result.analysis.benchmarks) == 4

    def test_latency_moves_the_price_the_slice_is_filled_at(self, schedule):
        prompt = simulate(schedule, window(), context(), ZeroImpactModel(), latency_seconds=0)
        delayed = simulate(schedule, window(), context(), ZeroImpactModel(), latency_seconds=1200)
        assert delayed.average_price > prompt.average_price

    def test_negative_latency_is_refused(self, schedule):
        with pytest.raises(ValueError, match="latency cannot be negative"):
            simulate(schedule, window(), context(), ZeroImpactModel(), latency_seconds=-1)


class TestUnfilledSlices:
    @pytest.fixture
    def schedule(self):
        return TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, context())

    def test_a_stale_price_leaves_the_slice_unfilled_rather_than_filling_it(self, schedule):
        """Unlike a portfolio mark, a hypothetical fill has nothing to preserve."""
        truncated = window(observations=PATH[:12])
        result = simulate(schedule, truncated, context(), ZeroImpactModel())

        assert result.completion_rate < 1.0
        assert result.unfilled
        assert SimulationWarning.INCOMPLETE in result.warnings
        assert "would assert liquidity nobody observed" in result.unfilled[0].reason

    def test_the_completion_rate_is_the_filled_fraction(self, schedule):
        result = simulate(schedule, window(observations=PATH[:12]), context(), ZeroImpactModel())
        assert result.completion_rate == pytest.approx(
            float(result.filled_quantity / result.ordered_quantity)
        )

    def test_a_generous_tolerance_fills_what_a_strict_one_leaves(self, schedule):
        truncated = window(observations=PATH[:12])
        strict = simulate(schedule, truncated, context(), ZeroImpactModel())
        generous = simulate(
            schedule,
            truncated,
            context(),
            ZeroImpactModel(),
            max_price_age_seconds=86_400.0,
        )
        assert generous.completion_rate > strict.completion_rate
        assert generous.completion_rate == 1.0

    def test_a_window_starting_after_the_order_leaves_everything_unfilled(self, schedule):
        later = MarketWindow(
            instrument_id=INSTRUMENT,
            start=T0,
            end=T1,
            observations=tuple(
                MarketObservation(item.timestamp + timedelta(days=1), item.price, "s")
                for item in PATH
            ),
            source="s",
        )
        result = simulate(schedule, later, context(), ZeroImpactModel())
        assert result.fills == ()
        assert result.completion_rate == 0.0
        assert result.average_price is None
        assert result.analysis is None
        assert "no observation at or before" in result.unfilled[0].reason


class TestComparison:
    def test_it_carries_both_what_ran_and_what_could_not(self):
        ctx = context()
        schedules, unavailable = {}, {}
        for strategy in (TWAPStrategy(), VWAPStrategy(), POVStrategy(0.01)):
            try:
                schedules[strategy.name] = strategy.generate_schedule(
                    Decimal(20_000), Side.BUY, ctx
                )
            except StrategyUnavailable as exc:
                unavailable[strategy.name] = exc.reason

        comparison = compare(schedules, unavailable, window(), ctx, ZeroImpactModel())
        assert len(comparison.results) == 2
        assert [name for name, _reason in comparison.unavailable] == ["POV"]
        payload = comparison.to_dict()
        assert payload["unavailable"][0]["reason"]

    def test_there_is_no_best_or_recommended_field(self):
        ctx = context()
        schedule = TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, ctx)
        payload = compare({"TWAP": schedule}, {}, window(), ctx, ZeroImpactModel()).to_dict()

        def keys(node) -> set[str]:
            if isinstance(node, dict):
                return set(node) | {key for value in node.values() for key in keys(value)}
            if isinstance(node, list):
                return {key for item in node for key in keys(item)}
            return set()

        for banned in ("best", "best_strategy", "recommended", "recommendation", "rank"):
            assert banned not in keys(payload)

        text = str(payload).lower()
        for phrase in ("best_strategy", "optimal execution", "you should", "we recommend"):
            assert phrase not in text

    def test_recommendation_is_only_ever_mentioned_to_deny_it(self):
        """The word may appear, but only in the sentence that refuses to make one."""
        ctx = context()
        schedule = TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, ctx)
        text = str(
            compare({"TWAP": schedule}, {}, window(), ctx, ZeroImpactModel()).to_dict()
        ).lower()
        start, seen = 0, 0
        while (found := text.find("recommend", start)) != -1:
            seen += 1
            assert "no strategy is " in text[max(0, found - 20) : found], text[
                max(0, found - 40) : found + 30
            ]
            start = found + 1
        assert seen, "the denial should be in the payload"

    def test_different_schedules_pay_different_prices_on_the_same_path(self):
        ctx = context()
        twap = TWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, ctx)
        vwap = VWAPStrategy().generate_schedule(Decimal(20_000), Side.BUY, ctx)
        comparison = compare(
            {"TWAP": twap, "VWAP": vwap}, {}, window(), ctx, SquareRootImpactModel(0.3, 0.2)
        )
        prices = [item.average_price for item in comparison.results]
        assert prices[0] != prices[1]
