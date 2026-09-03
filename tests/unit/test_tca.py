"""Benchmarks, implementation shortfall and the cost decomposition.

Carries the Phase 7 acceptance criteria from docs/backlog.md: deterministic
synthetic price paths produce hand-checkable IS values; every benchmark reports
its window, source and method; low data coverage degrades to a stated
unavailability rather than a confident wrong number.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domains.execution.benchmarks import (
    MIN_INTERVAL_OBSERVATIONS,
    MIN_SPAN_RATIO,
    BenchmarkFlag,
    BenchmarkKind,
    BenchmarkMethod,
    MarketObservation,
    MarketWindow,
    arrival_benchmark,
    close_benchmark,
    decision_benchmark,
    interval_twap,
    interval_vwap,
    prevailing_mid_benchmark,
)
from domains.execution.models import (
    Execution,
    ExecutionError,
    GroupingMethod,
    OrderType,
    ParentOrder,
    Side,
    group_executions,
)
from domains.execution.tca import (
    CostComponentStatus,
    TCAWarning,
    analyse,
    decompose,
    implementation_shortfall,
    opportunity_cost,
    spread_cost,
)

USER = uuid.uuid4()
INSTRUMENT = uuid.uuid4()
T0 = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)

#: A price path rising by exactly 1.00 every minute from 100.00 at T0, so every
#: benchmark below has an answer that can be worked out by hand.
PATH = tuple(
    MarketObservation(
        timestamp=T0 + timedelta(minutes=k),
        price=Decimal(100 + k),
        source="synthetic",
        volume=Decimal(1000),
        spread=Decimal("0.20"),
    )
    for k in range(31)
)


def fill(
    minute: int,
    quantity: str,
    price: str,
    side: Side = Side.BUY,
    submit: datetime | None = T0,
    parent: str | None = "P1",
    fees: str = "5",
    order_quantity: str | None = None,
) -> Execution:
    return Execution(
        id=uuid.uuid4(),
        user_id=USER,
        instrument_id=INSTRUMENT,
        side=side,
        quantity=Decimal(quantity),
        execution_price=Decimal(price),
        exchange_timestamp=T0 + timedelta(minutes=minute),
        submit_timestamp=submit,
        parent_order_key=parent,
        order_type=OrderType.LIMIT,
        fees=Decimal(fees),
        order_quantity=Decimal(order_quantity) if order_quantity else None,
    )


def parent_of(*executions: Execution, multiplier: str = "1") -> ParentOrder:
    return ParentOrder(
        key="P1",
        instrument_id=INSTRUMENT,
        side=executions[0].side,
        executions=executions,
        grouping_method=GroupingMethod.EXPLICIT,
        multiplier=Decimal(multiplier),
        currency="INR",
    )


def window_for(parent: ParentOrder, observations=PATH, source: str = "synthetic") -> MarketWindow:
    return MarketWindow(
        instrument_id=INSTRUMENT,
        start=parent.start,
        end=parent.end,
        observations=observations,
        source=source,
    )


@pytest.fixture
def buy() -> ParentOrder:
    """Buy 300 at an average of exactly 104, submitted when the path was 100."""
    return parent_of(
        fill(2, "100", "102", order_quantity="300"),
        fill(4, "100", "104", order_quantity="300"),
        fill(6, "100", "106", order_quantity="300"),
    )


class TestHandCheckableShortfall:
    """Phase 7 acceptance: a deterministic path with an answer you can verify."""

    def test_the_average_price_is_the_quantity_weighted_one(self, buy):
        assert buy.average_price == Decimal(104)
        assert buy.filled_quantity == Decimal(300)

    def test_arrival_is_the_path_value_at_the_submit_time(self, buy):
        benchmark = arrival_benchmark(window_for(buy), T0, buy.first_fill.execution_price)
        assert benchmark.price == Decimal(100)
        assert benchmark.method is BenchmarkMethod.QUOTE_MID_AT_TIMESTAMP

    def test_shortfall_in_all_three_units(self, buy):
        benchmark = arrival_benchmark(window_for(buy), T0, buy.first_fill.execution_price)
        shortfall = implementation_shortfall(buy, benchmark)

        # (104 - 100) * 300 * 1
        assert shortfall.currency_amount == Decimal(1200)
        # 4 / 100 = 4%, which is 400 basis points.
        assert shortfall.basis_points == pytest.approx(400.0)
        assert shortfall.percent == pytest.approx(4.0)

    def test_the_multiplier_scales_the_currency_amount_and_nothing_else(self):
        scaled = parent_of(
            fill(2, "100", "102"), fill(4, "100", "104"), fill(6, "100", "106"), multiplier="75"
        )
        benchmark = arrival_benchmark(window_for(scaled), T0, Decimal("102"))
        shortfall = implementation_shortfall(scaled, benchmark)

        assert shortfall.currency_amount == Decimal(1200) * 75
        assert shortfall.basis_points == pytest.approx(400.0)

    def test_selling_into_the_same_rising_path_is_a_gain(self):
        sell = parent_of(
            fill(2, "100", "102", side=Side.SELL),
            fill(4, "100", "104", side=Side.SELL),
            fill(6, "100", "106", side=Side.SELL),
        )
        benchmark = arrival_benchmark(window_for(sell), T0, Decimal("102"))
        shortfall = implementation_shortfall(sell, benchmark)

        assert shortfall.currency_amount == Decimal(-1200)
        assert "Positive is a cost" in shortfall.to_dict()["convention"]

    def test_a_buy_below_arrival_is_a_negative_cost(self, buy):
        late = parent_of(fill(2, "100", "99"))
        benchmark = arrival_benchmark(window_for(late), T0, Decimal("99"))
        assert implementation_shortfall(late, benchmark).currency_amount < 0

    def test_the_twap_is_the_step_weighted_mean_of_the_path(self, buy):
        """Window is T0 (submit) to T0+6m, so 100..105 each hold one minute."""
        benchmark = interval_twap(window_for(buy))
        assert benchmark.price == Decimal("102.5")
        assert benchmark.method is BenchmarkMethod.TIME_WEIGHTED_STEP

    def test_the_vwap_is_the_volume_weighted_mean(self, buy):
        """Every observation carries the same volume, so it is the plain mean."""
        benchmark = interval_vwap(window_for(buy))
        assert benchmark.price == pytest.approx(Decimal("103"))
        assert benchmark.method is BenchmarkMethod.VOLUME_WEIGHTED

    def test_the_prevailing_mid_is_weighted_by_fill_quantity(self):
        uneven = parent_of(fill(2, "100", "102"), fill(6, "300", "106"))
        benchmark = prevailing_mid_benchmark(
            window_for(uneven),
            [(item.exchange_timestamp, item.quantity) for item in uneven.ordered],
        )
        # (102 * 100 + 106 * 300) / 400
        assert benchmark.price == Decimal(105)

    def test_the_close_is_the_last_observation_at_or_after_the_window(self, buy):
        benchmark = close_benchmark(window_for(buy))
        assert benchmark.price == Decimal(130)
        assert benchmark.method is BenchmarkMethod.LAST_OBSERVATION_IN_WINDOW


class TestEveryBenchmarkDeclaresItself:
    """Phase 7 acceptance: window, source and method, on every one."""

    def test_all_of_them(self, buy):
        window = window_for(buy)
        benchmarks = [
            arrival_benchmark(window, T0, Decimal("102")),
            prevailing_mid_benchmark(window, [(T0 + timedelta(minutes=2), Decimal(100))]),
            interval_twap(window),
            interval_vwap(window),
            close_benchmark(window),
        ]
        for benchmark in benchmarks:
            payload = benchmark.to_dict()
            assert payload["method"] != str(BenchmarkMethod.NOT_AVAILABLE)
            assert payload["window"]["start"] is not None
            assert payload["window"]["end"] is not None
            assert payload["source"]

    def test_an_unavailable_benchmark_always_says_why(self, buy):
        empty = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=(),
            source="synthetic",
        )
        for factory in (interval_twap, interval_vwap, close_benchmark):
            benchmark = factory(empty)
            assert benchmark.available is False
            assert benchmark.unavailable_reason
            assert len(benchmark.unavailable_reason) > 20

    def test_a_missing_benchmark_produces_no_shortfall_rather_than_zero(self, buy):
        empty = MarketWindow(
            instrument_id=INSTRUMENT, start=buy.start, end=buy.end, observations=(), source="s"
        )
        assert implementation_shortfall(buy, interval_twap(empty)) is None


class TestArrivalProxy:
    def test_no_submit_timestamp_falls_back_and_flags_it(self):
        anonymous = parent_of(fill(2, "100", "102", submit=None))
        benchmark = arrival_benchmark(window_for(anonymous), None, Decimal("102"))

        assert benchmark.price == Decimal("102")
        assert benchmark.method is BenchmarkMethod.FIRST_FILL_PROXY
        assert BenchmarkFlag.ARRIVAL_PROXY_USED in benchmark.flags

    def test_the_proxy_understates_the_cost_which_is_why_it_is_flagged(self, buy):
        """Measuring against the first fill hides everything before it."""
        proper = implementation_shortfall(
            buy, arrival_benchmark(window_for(buy), T0, Decimal("102"))
        )
        proxied = implementation_shortfall(
            buy, arrival_benchmark(window_for(buy), None, Decimal("102"))
        )
        assert proxied.currency_amount < proper.currency_amount

    def test_a_stale_reference_quote_is_flagged(self, buy):
        sparse = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=(PATH[0],),
            source="synthetic",
            staleness_tolerance_seconds=60.0,
        )
        benchmark = arrival_benchmark(sparse, T0 + timedelta(minutes=10), Decimal("102"))
        assert BenchmarkFlag.STALE_REFERENCE_QUOTE in benchmark.flags

    def test_a_decision_benchmark_without_a_timestamp_is_unavailable(self, buy):
        benchmark = decision_benchmark(window_for(buy), None)
        assert benchmark.available is False
        assert BenchmarkFlag.NO_DECISION_TIMESTAMP in benchmark.flags
        assert "not estimated here" in benchmark.unavailable_reason

    def test_a_supplied_decision_price_is_recorded_as_supplied(self, buy):
        benchmark = decision_benchmark(window_for(buy), T0, Decimal("99"))
        assert benchmark.price == Decimal("99")
        assert benchmark.method is BenchmarkMethod.SUPPLIED_BY_CALLER


class TestDataCoverage:
    """Phase 7 acceptance: low coverage degrades, never to a confident number."""

    def test_a_handful_of_ticks_is_not_an_interval(self, buy):
        sparse = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=PATH[:2],
            source="synthetic",
        )
        benchmark = interval_twap(sparse)
        assert benchmark.available is False
        assert BenchmarkFlag.DATA_COVERAGE_LOW in benchmark.flags
        assert "not the interval" in benchmark.unavailable_reason

    def test_observations_clustered_in_a_corner_are_refused(self):
        """Enough observations, but they cover none of the window."""
        long_order = parent_of(fill(0, "100", "100", submit=T0), fill(30, "100", "130"))
        clustered = MarketWindow(
            instrument_id=INSTRUMENT,
            start=long_order.start,
            end=long_order.end,
            observations=PATH[:5],
            source="synthetic",
        )
        coverage = clustered.coverage
        assert coverage.observations >= MIN_INTERVAL_OBSERVATIONS
        assert coverage.span_ratio < MIN_SPAN_RATIO
        assert interval_twap(clustered).available is False

    def test_coverage_travels_with_the_window(self, buy):
        payload = window_for(buy).to_dict()["coverage"]
        assert payload["is_sufficient"] is True
        assert payload["minimum_observations"] == MIN_INTERVAL_OBSERVATIONS
        assert "not an interval price" in payload["policy"]

    def test_a_source_without_interval_volume_refuses_the_vwap_only(self, buy):
        """It does not quietly become a time-weighted average under that name."""
        no_volume = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=tuple(
                MarketObservation(item.timestamp, item.price, "chain_snapshots", None, item.spread)
                for item in PATH
            ),
            source="chain_snapshots",
        )
        vwap = interval_vwap(no_volume)
        assert vwap.available is False
        assert BenchmarkFlag.NO_VOLUME_DATA in vwap.flags
        assert "wearing this one's label" in vwap.unavailable_reason
        assert interval_twap(no_volume).available is True

    def test_zero_traded_volume_is_not_a_volume_weighted_price(self, buy):
        zeroed = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=tuple(
                MarketObservation(item.timestamp, item.price, "s", Decimal(0), item.spread)
                for item in PATH
            ),
            source="s",
        )
        assert interval_vwap(zeroed).available is False


class TestDecomposition:
    def test_the_components_reconcile_to_the_total(self, buy):
        window = window_for(buy)
        shortfall = implementation_shortfall(buy, arrival_benchmark(window, T0, Decimal("102")))
        decomposition, _warnings = decompose(buy, window, shortfall)
        amounts = {item.name: item.amount for item in decomposition.components}

        # Half of a 0.20 spread on 300 units, plus 3 fills at 5 each.
        assert amounts["spread"] == Decimal(30)
        assert amounts["fees"] == Decimal(15)
        assert amounts["timing_residual"] == shortfall.currency_amount - Decimal(45)
        assert decomposition.total == Decimal(1200)

    def test_impact_is_reported_as_not_modelled_rather_than_as_zero(self, buy):
        window = window_for(buy)
        shortfall = implementation_shortfall(buy, arrival_benchmark(window, T0, Decimal("102")))
        decomposition, warnings = decompose(buy, window, shortfall)

        impact = next(item for item in decomposition.components if item.name == "impact")
        assert impact.amount is None
        assert impact.status is CostComponentStatus.NOT_MODELLED
        assert "would be an invention" in impact.basis
        assert TCAWarning.IMPACT_NOT_MODELLED in warnings

    def test_the_residual_says_what_it_carries(self, buy):
        window = window_for(buy)
        shortfall = implementation_shortfall(buy, arrival_benchmark(window, T0, Decimal("102")))
        decomposition, _warnings = decompose(buy, window, shortfall)

        residual = next(item for item in decomposition.components if item.name == "timing_residual")
        assert residual.status is CostComponentStatus.RESIDUAL
        assert "market impact" in residual.basis
        assert "not a measurement" in decomposition.to_dict()["caveat"]

    def test_only_fees_are_labelled_as_measured(self, buy):
        window = window_for(buy)
        shortfall = implementation_shortfall(buy, arrival_benchmark(window, T0, Decimal("102")))
        decomposition, _warnings = decompose(buy, window, shortfall)
        measured = [
            item.name
            for item in decomposition.components
            if item.status is CostComponentStatus.MEASURED
        ]
        assert measured == ["fees"]

    def test_no_two_sided_quote_means_no_spread_charge_is_attributed(self, buy):
        no_spread = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=tuple(
                MarketObservation(item.timestamp, item.price, "s", item.volume, None)
                for item in PATH
            ),
            source="s",
        )
        amount, basis, covered = spread_cost(buy, no_spread)
        assert amount is None
        assert covered == 0
        assert "inside the residual" in basis

    def test_partial_spread_coverage_is_counted_in_the_basis(self, buy):
        partial = MarketWindow(
            instrument_id=INSTRUMENT,
            start=buy.start,
            end=buy.end,
            observations=(
                PATH[0],
                *[
                    MarketObservation(item.timestamp, item.price, "s", item.volume, None)
                    for item in PATH[1:]
                ],
            ),
            source="s",
        )
        _amount, basis, covered = spread_cost(buy, partial)
        assert covered < len(buy.executions) or "of 3 fills" in basis


class TestOpportunityCost:
    def test_an_unstated_order_quantity_is_not_assumed_to_be_zero(self):
        anonymous = parent_of(fill(2, "100", "102", order_quantity=None))
        window = window_for(anonymous)
        component = opportunity_cost(
            anonymous,
            arrival_benchmark(window, T0, Decimal("102")),
            close_benchmark(window),
        )
        assert component.amount is None
        assert component.status is CostComponentStatus.UNAVAILABLE
        assert "not assumed to be zero" in component.basis

    def test_a_fully_filled_order_has_none(self, buy):
        window = window_for(buy)
        component = opportunity_cost(
            buy, arrival_benchmark(window, T0, Decimal("102")), close_benchmark(window)
        )
        assert component.amount == Decimal(0)
        assert component.status is CostComponentStatus.MEASURED

    def test_an_unfilled_remainder_is_valued_at_the_later_move(self):
        short_filled = parent_of(fill(2, "100", "102", order_quantity="300"))
        window = window_for(short_filled)
        component = opportunity_cost(
            short_filled,
            arrival_benchmark(window, T0, Decimal("102")),
            close_benchmark(window),
        )
        # 200 unfilled, and the path moved from 100 at arrival to 130 at close.
        assert component.amount == Decimal(30) * 200
        assert component.status is CostComponentStatus.MODELLED


class TestGrouping:
    def test_an_explicit_parent_is_never_inferred(self):
        parents = group_executions([fill(0, "75", "100"), fill(200, "75", "101")])
        assert len(parents) == 1
        assert parents[0].grouping_method is GroupingMethod.EXPLICIT
        assert parents[0].is_inferred is False

    def test_an_explicit_parent_survives_any_gap(self):
        parents = group_executions([fill(0, "75", "100"), fill(600, "75", "101")])
        assert len(parents) == 1

    def test_fills_with_no_parent_are_grouped_by_time_and_flagged(self):
        parents = group_executions(
            [
                fill(0, "75", "100", parent=None),
                fill(2, "75", "101", parent=None),
                fill(30, "75", "110", parent=None),
            ],
            max_gap_seconds=300.0,
        )
        assert len(parents) == 2
        assert all(parent.is_inferred for parent in parents)
        assert {parent.grouping_method for parent in parents} == {GroupingMethod.INFERRED_BY_TIME}

    def test_the_gap_changes_the_grouping_which_is_why_it_is_recorded(self):
        fills = [fill(k, "75", "100", parent=None) for k in (0, 10, 20)]
        assert len(group_executions(fills, max_gap_seconds=300.0)) == 3
        assert len(group_executions(fills, max_gap_seconds=1200.0)) == 1

    def test_opposite_sides_never_join_one_parent(self):
        parents = group_executions(
            [
                fill(0, "75", "100", side=Side.BUY, parent=None),
                fill(1, "75", "100", side=Side.SELL, parent=None),
            ]
        )
        assert len(parents) == 2
        assert {parent.side for parent in parents} == {Side.BUY, Side.SELL}

    def test_a_two_sided_parent_is_refused_outright(self):
        with pytest.raises(ExecutionError, match="one side"):
            ParentOrder(
                key="P1",
                instrument_id=INSTRUMENT,
                side=Side.BUY,
                executions=(fill(0, "75", "100"), fill(1, "75", "100", side=Side.SELL)),
                grouping_method=GroupingMethod.EXPLICIT,
            )

    def test_the_window_starts_at_submission_not_at_the_first_fill(self, buy):
        assert buy.start == T0
        assert buy.start < buy.first_fill.exchange_timestamp
        assert buy.duration == timedelta(minutes=6)


class TestExecutionInvariants:
    @pytest.mark.parametrize("quantity", ["0", "-75"])
    def test_a_non_positive_quantity_is_refused(self, quantity):
        with pytest.raises(ExecutionError, match="must be positive"):
            fill(0, quantity, "100")

    def test_a_negative_price_is_refused(self):
        with pytest.raises(ExecutionError, match="not a fill"):
            fill(0, "75", "-1")

    def test_negative_fees_are_refused_as_a_disguised_rebate(self):
        with pytest.raises(ExecutionError, match="rebate"):
            fill(0, "75", "100", fees="-5")

    @pytest.mark.parametrize(
        ("token", "expected"),
        [("BUY", Side.BUY), ("b", Side.BUY), ("SLD", Side.SELL), ("sell", Side.SELL)],
    )
    def test_sides_parse_from_what_brokers_actually_write(self, token, expected):
        assert Side.parse(token) is expected

    def test_an_unreadable_side_is_refused_rather_than_defaulted(self):
        with pytest.raises(ValueError, match="unrecognised side"):
            Side.parse("HOLD")

    def test_the_sign_convention_lives_in_one_place(self):
        assert Side.BUY.sign == 1
        assert Side.SELL.sign == -1


class TestAnalysisWiring:
    def test_an_analysis_carries_every_benchmark_and_its_warnings(self, buy):
        window = window_for(buy)
        analysis = analyse(
            buy,
            window,
            [
                arrival_benchmark(window, T0, Decimal("102")),
                interval_twap(window),
                interval_vwap(window),
                close_benchmark(window),
            ],
        )
        assert len(analysis.benchmarks) == 4
        assert len(analysis.shortfalls) == 4
        assert analysis.primary is BenchmarkKind.ARRIVAL
        assert analysis.decomposition is not None
        assert TCAWarning.IMPACT_NOT_MODELLED in analysis.warnings

    def test_a_benchmark_with_no_price_lands_in_unavailable_with_its_reason(self, buy):
        empty = MarketWindow(
            instrument_id=INSTRUMENT, start=buy.start, end=buy.end, observations=(), source="s"
        )
        analysis = analyse(
            buy,
            empty,
            [interval_twap(empty), arrival_benchmark(empty, T0, Decimal("102"))],
        )
        assert any(item.benchmark is BenchmarkKind.INTERVAL_TWAP for item in analysis.unavailable)
        assert all(item.reason for item in analysis.unavailable)

    def test_with_nothing_available_the_analysis_says_so(self, buy):
        empty = MarketWindow(
            instrument_id=INSTRUMENT, start=buy.start, end=buy.end, observations=(), source="s"
        )
        analysis = analyse(buy, empty, [interval_twap(empty), interval_vwap(empty)])
        assert analysis.shortfalls == ()
        assert analysis.decomposition is None
        assert TCAWarning.NO_BENCHMARK in analysis.warnings

    def test_the_primary_falls_back_to_whatever_is_available(self, buy):
        window = window_for(buy)
        analysis = analyse(
            buy,
            window,
            [decision_benchmark(window, None), interval_twap(window)],
            primary=BenchmarkKind.DECISION,
        )
        assert analysis.primary is BenchmarkKind.INTERVAL_TWAP
        assert analysis.primary_shortfall is not None

    def test_an_inferred_grouping_is_reported_on_the_analysis(self):
        loose = group_executions([fill(0, "75", "100", parent=None)])[0]
        window = window_for(loose)
        analysis = analyse(loose, window, [arrival_benchmark(window, None, Decimal("100"))])
        assert TCAWarning.INFERRED_GROUPING in analysis.warnings

    @given(
        quantities=st.lists(st.integers(min_value=1, max_value=500), min_size=1, max_size=8),
        drift=st.integers(min_value=-30, max_value=30),
    )
    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_the_average_price_always_lies_between_the_best_and_worst_fill(self, quantities, drift):
        fills = tuple(
            fill(index, str(quantity), str(100 + drift + index))
            for index, quantity in enumerate(quantities)
        )
        parent = parent_of(*fills)
        prices = [item.execution_price for item in fills]
        assert min(prices) <= parent.average_price <= max(prices)

    @given(price=st.integers(min_value=1, max_value=500))
    @settings(max_examples=30, deadline=None)
    def test_a_shortfall_against_its_own_average_is_always_zero(self, price):
        """The measured thing cannot be its own benchmark and show a cost."""
        parent = parent_of(fill(2, "100", str(price)))
        window = window_for(parent)
        benchmark = arrival_benchmark(window, None, Decimal(price))
        assert implementation_shortfall(parent, benchmark).currency_amount == 0
