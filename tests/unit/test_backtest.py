"""The backtest engine, its costs, and the ways a backtest lies.

Three families of test, and they map onto the three ways a backtest reports a
return nobody could have earned:

* it saw the future — covered by the fill-timing tests;
* it traded for free — covered by the cost and slippage tests;
* its accounting was wrong — covered by the buy-and-hold benchmark and the
  attribution identity, which have to agree with an independently computed
  answer rather than merely with themselves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.research.attribution import attribute
from domains.research.costs import (
    NO_COST_MODEL,
    NO_SLIPPAGE_MODEL,
    CostBasis,
    CostComponent,
    CostSchedule,
    CostSide,
    SlippageModel,
    schedule_from_components,
)
from domains.research.engine import (
    BacktestConfig,
    BacktestError,
    BacktestWarning,
    run,
)
from domains.research.features import BarSeries
from domains.research.metrics import evaluate, max_drawdown, periods_per_year
from domains.research.models import ExecutionTiming, Position
from domains.research.strategies import (
    BuyAndHold,
    MeanReversion,
    Momentum,
    MovingAverageCrossover,
    build,
)

INSTRUMENT = uuid.UUID(int=9)
START = datetime(2026, 1, 1, tzinfo=UTC)
CASH = Decimal(1_000_000)


def series(closes: list[float], opens: list[float] | None = None) -> BarSeries:
    close_values = [Decimal(str(round(value, 6))) for value in closes]
    open_values = (
        [Decimal(str(round(value, 6))) for value in opens] if opens else list(close_values)
    )
    return BarSeries(
        instrument_id=INSTRUMENT,
        timestamps=tuple(START + timedelta(days=index) for index in range(len(close_values))),
        open=tuple(open_values),
        high=tuple(
            max(a, b) * Decimal("1.001") for a, b in zip(open_values, close_values, strict=True)
        ),
        low=tuple(
            min(a, b) * Decimal("0.999") for a, b in zip(open_values, close_values, strict=True)
        ),
        close=tuple(close_values),
        volume=tuple(Decimal(100_000) for _ in close_values),
    )


def rising(count: int = 120, rate: float = 0.002) -> BarSeries:
    return series([100 * (1 + rate) ** index for index in range(count)])


class TestTheAccountingBenchmark:
    """Buy-and-hold has to earn the instrument's return. When it does not, the
    bug is in the engine and no Sharpe ratio would have found it."""

    def test_buy_and_hold_trades_once(self):
        result = run(rising(), BuyAndHold(), BacktestConfig(initial_cash=CASH))
        assert len(result.fills) == 1

    def test_its_final_equity_is_cash_plus_the_position(self):
        bars = rising()
        result = run(bars, BuyAndHold(), BacktestConfig(initial_cash=CASH))
        fill = result.fills[0]

        expected = (CASH - fill.notional - fill.cost.total) + fill.quantity * bars.close[-1]
        assert result.final_equity == expected

    def test_its_return_matches_the_instrument_over_the_held_window(self):
        bars = rising()
        result = run(bars, BuyAndHold(), BacktestConfig(initial_cash=CASH))
        fill = result.fills[0]

        instrument_return = float(bars.close[-1] / fill.price - 1)
        book_return = float(result.final_equity / CASH - 1)
        # The gap is the idle cash left by whole-unit rounding, and nothing else.
        assert book_return == pytest.approx(instrument_return, rel=0.002)

    def test_a_short_series_cannot_be_backtested(self):
        with pytest.raises(BacktestError, match="two bars"):
            run(series([100.0]), BuyAndHold())


class TestFillsCannotSeeTheFuture:
    def test_a_decision_is_filled_on_the_next_bar(self):
        bars = rising(10)
        result = run(bars, BuyAndHold(), BacktestConfig(timing=ExecutionTiming.NEXT_OPEN))
        fill = result.fills[0]

        assert fill.timestamp == bars.timestamps[1]
        assert fill.reference_price == bars.open[1]

    def test_next_close_timing_fills_at_the_next_close(self):
        bars = rising(10)
        result = run(bars, BuyAndHold(), BacktestConfig(timing=ExecutionTiming.NEXT_CLOSE))
        assert result.fills[0].reference_price == bars.close[1]

    def test_the_fill_price_is_never_the_bar_the_decision_used(self):
        """The most common backtest error, and the reason the engine offers no
        'same bar's close' option at all."""
        bars = series([100, 200, 300, 400], opens=[100, 200, 300, 400])
        result = run(bars, BuyAndHold(), BacktestConfig())
        assert result.fills[0].reference_price != bars.close[0]

    def test_a_signal_on_the_final_bar_is_recorded_and_not_executed(self):
        """Filling it would be a free trade at a price the decision already saw."""
        bars = rising(30)
        result = run(bars, MovingAverageCrossover(fast=3, slow=5), BacktestConfig())
        assert BacktestWarning.FINAL_SIGNAL_NOT_EXECUTED in result.warnings
        assert all(fill.timestamp < bars.timestamps[-1] for fill in result.fills[:-1])


class TestCosts:
    def _schedule(self) -> CostSchedule:
        return schedule_from_components(
            "test",
            [
                {"name": "brokerage", "basis": "TURNOVER", "rate": "0.0003", "maximum": "20"},
                {"name": "stt", "basis": "TURNOVER", "rate": "0.001", "side": "SELL"},
                {
                    "name": "gst",
                    "basis": "ON_OTHER_COMPONENTS",
                    "rate": "0.18",
                    "applies_to": ["brokerage"],
                },
            ],
            source="illustrative only",
        )

    def test_no_schedule_means_gross_and_says_so(self):
        """Not zero cost. A backtest that silently assumed free trading is the
        commonest way a strategy reports returns that do not exist."""
        result = run(rising(), BuyAndHold(), BacktestConfig(costs=NO_COST_MODEL))
        assert result.gross_of_costs is True
        assert BacktestWarning.COSTS_NOT_MODELLED in result.warnings
        assert result.total_costs == Decimal(0)
        assert result.fills[0].cost.modelled is False

    def test_a_supplied_schedule_is_charged_and_leaves_the_book(self):
        gross = run(rising(), BuyAndHold(), BacktestConfig(initial_cash=CASH))
        net = run(rising(), BuyAndHold(), BacktestConfig(initial_cash=CASH, costs=self._schedule()))
        assert net.total_costs > 0
        assert net.final_equity < gross.final_equity
        assert net.gross_of_costs is False

    def test_a_sell_only_component_is_not_charged_on_a_buy(self):
        schedule = self._schedule()
        buy = schedule.charge(Decimal(100), Decimal(100), is_buy=True)
        sell = schedule.charge(Decimal(100), Decimal(100), is_buy=False)
        assert {item.name for item in buy.components} == {"brokerage", "gst"}
        assert "stt" in {item.name for item in sell.components}

    def test_a_cap_binds(self):
        schedule = CostSchedule(
            "capped",
            (CostComponent("brokerage", CostBasis.TURNOVER, Decimal("0.01"), maximum=Decimal(20)),),
        )
        charged = schedule.charge(Decimal(1000), Decimal(1000), is_buy=True)
        assert charged.total == Decimal(20)

    def test_a_derived_component_sees_only_what_it_applies_to(self):
        schedule = CostSchedule(
            "derived",
            (
                CostComponent("a", CostBasis.PER_ORDER, Decimal(100)),
                CostComponent("b", CostBasis.PER_ORDER, Decimal(200)),
                CostComponent(
                    "tax",
                    CostBasis.ON_OTHER_COMPONENTS,
                    Decimal("0.10"),
                    applies_to=("a",),
                ),
            ),
        )
        charged = schedule.charge(Decimal(1), Decimal(1), is_buy=True)
        assert dict((item.name, item.amount) for item in charged.components)["tax"] == Decimal(10)

    def test_a_per_unit_component_scales_with_quantity_not_value(self):
        schedule = CostSchedule(
            "per-unit", (CostComponent("fee", CostBasis.PER_UNIT, Decimal("0.05")),)
        )
        assert schedule.charge(Decimal(10), Decimal(100), True).total == Decimal(5)
        assert schedule.charge(Decimal(1000), Decimal(100), True).total == Decimal(5)

    def test_side_filtering_is_explicit(self):
        component = CostComponent("x", CostBasis.PER_ORDER, Decimal(1), side=CostSide.SELL)
        assert component.applies(is_buy=False) is True
        assert component.applies(is_buy=True) is False


class TestSlippage:
    def test_no_slippage_is_labelled_not_silent(self):
        result = run(rising(), BuyAndHold(), BacktestConfig(slippage=NO_SLIPPAGE_MODEL))
        assert BacktestWarning.SLIPPAGE_NOT_MODELLED in result.warnings
        assert result.total_slippage == Decimal(0)

    def test_slippage_always_moves_against_the_trader(self):
        model = SlippageModel(basis_points=Decimal(10), source="assumed")
        assert model.apply(Decimal(100), is_buy=True) > Decimal(100)
        assert model.apply(Decimal(100), is_buy=False) < Decimal(100)

    def test_a_buy_pays_more_than_the_reference_price(self):
        result = run(
            rising(),
            BuyAndHold(),
            BacktestConfig(slippage=SlippageModel(Decimal(25), source="assumed 25bp")),
        )
        fill = result.fills[0]
        assert fill.price > fill.reference_price
        assert fill.slippage_cost > 0


class TestPositionAccounting:
    def test_adding_moves_the_average_price(self):
        position = Position(instrument_id=INSTRUMENT)
        position.apply(Decimal(100), Decimal(10))
        position.apply(Decimal(100), Decimal(20))
        assert position.average_price == Decimal(15)

    def test_reducing_realises_against_the_average(self):
        position = Position(instrument_id=INSTRUMENT)
        position.apply(Decimal(200), Decimal(15))
        realised = position.apply(Decimal(-100), Decimal(25))
        assert realised == Decimal(1000)
        assert position.average_price == Decimal(15)

    def test_crossing_through_zero_opens_the_new_side_at_the_fill_price(self):
        """Anything else leaves an average price mixing a long and a short."""
        position = Position(instrument_id=INSTRUMENT)
        position.apply(Decimal(100), Decimal(10))
        position.apply(Decimal(-300), Decimal(20))
        assert position.quantity == Decimal(-200)
        assert position.average_price == Decimal(20)
        assert position.realised_pnl == Decimal(1000)


class TestLimits:
    def test_a_weight_beyond_the_limit_is_clamped_and_reported(self):
        """A strategy whose weights are being cut is not the strategy that was
        described, so the clamp is never silent."""
        result = run(
            rising(),
            BuyAndHold(weight=Decimal(3)),
            BacktestConfig(initial_cash=CASH, max_position_weight=Decimal(1)),
        )
        assert BacktestWarning.WEIGHT_LIMITED in result.warnings
        assert result.fills[0].notional <= CASH

    def test_a_trade_below_the_rebalance_threshold_is_skipped(self):
        flat = series([100.0] * 50)
        result = run(
            flat,
            MovingAverageCrossover(fast=3, slow=5),
            BacktestConfig(rebalance_threshold=Decimal("0.5")),
        )
        assert len(result.fills) <= 1

    def test_whole_units_are_enforced_by_default(self):
        result = run(rising(), BuyAndHold(), BacktestConfig(initial_cash=CASH))
        assert result.fills[0].quantity == result.fills[0].quantity.to_integral_value()


class TestFlaggedBars:
    def test_a_flagged_bar_is_marked_but_not_traded_on(self):
        bars = rising(60)
        flags = [index == 30 for index in range(60)]
        result = run(bars, MovingAverageCrossover(fast=3, slow=5), BacktestConfig(), flags)

        assert result.bars_flagged == 1
        assert result.bars_used == 59
        # The curve stays continuous: dropping the bar would put a hole in it.
        assert len(result.equity_curve) == 60

    def test_including_them_is_a_choice_that_is_recorded(self):
        bars = rising(60)
        flags = [index == 30 for index in range(60)]
        result = run(
            bars,
            MovingAverageCrossover(fast=3, slow=5),
            BacktestConfig(include_flagged_bars=True),
            flags,
        )
        assert BacktestWarning.FLAGGED_BARS_INCLUDED in result.warnings
        assert result.bars_used == 60


class TestAttribution:
    def test_the_identity_closes(self):
        bars = rising(200)
        result = run(
            bars,
            MovingAverageCrossover(fast=10, slow=30),
            BacktestConfig(
                initial_cash=CASH,
                costs=schedule_from_components(
                    "t", [{"name": "brokerage", "basis": "TURNOVER", "rate": "0.0005"}]
                ),
                slippage=SlippageModel(Decimal(5), source="assumed"),
            ),
        )
        report = attribute(
            result.initial_equity,
            result.final_equity,
            result.fills,
            result.realised_by_instrument,
            result.unrealised_by_instrument,
        )
        assert report.reconciles, report.to_dict()
        assert abs(report.residual) < Decimal("0.01")

    def test_slippage_is_reported_but_not_subtracted_twice(self):
        """It is already inside the fill prices. Subtracting it again would
        double-count it into the residual, and the residual would absorb it."""
        bars = rising(100)
        result = run(
            bars,
            MovingAverageCrossover(fast=5, slow=20),
            BacktestConfig(slippage=SlippageModel(Decimal(50), source="assumed")),
        )
        report = attribute(
            result.initial_equity,
            result.final_equity,
            result.fills,
            result.realised_by_instrument,
            result.unrealised_by_instrument,
        )
        assert report.slippage > 0
        assert report.reconciles

    def test_greeks_are_absent_rather_than_zero(self):
        """A zero theta reads as 'no time decay', not as 'not applicable'."""
        result = run(rising(30), BuyAndHold(), BacktestConfig())
        report = attribute(
            result.initial_equity,
            result.final_equity,
            result.fills,
            result.realised_by_instrument,
            result.unrealised_by_instrument,
        )
        payload = report.to_dict()
        assert "greeks" in payload["not_attributed"]
        assert "theta" not in payload


class TestMetrics:
    def test_the_annualisation_factor_comes_from_the_timestamps(self):
        """Assuming 252 on a weekly series would be wrong by a factor of seven,
        and nothing about the output would say so."""
        daily = [START + timedelta(days=index) for index in range(50)]
        weekly = [START + timedelta(weeks=index) for index in range(50)]
        assert periods_per_year(daily) == pytest.approx(365.25, rel=0.01)
        assert periods_per_year(weekly) == pytest.approx(52.18, rel=0.01)

    def test_a_short_window_gets_no_cagr(self):
        """Annualising a six-week return produces a number about a year nobody
        observed, and it is invariably the largest number in the report."""
        equity = [Decimal(100 + index) for index in range(30)]
        stamps = [START + timedelta(days=index) for index in range(30)]
        metrics = evaluate(equity, stamps)
        assert metrics.cagr is None
        assert "METRICS_WINDOW_TOO_SHORT_FOR_CAGR" in metrics.warnings

    def test_a_short_sample_is_reported_with_its_count_not_withheld(self):
        equity = [Decimal(100 + index) for index in range(10)]
        stamps = [START + timedelta(days=index) for index in range(10)]
        metrics = evaluate(equity, stamps)
        assert metrics.observations == 9
        assert metrics.is_reliable is False
        assert "METRICS_SHORT_SAMPLE" in metrics.warnings

    def test_a_curve_that_only_rises_has_no_sortino(self):
        """No downside observations means no downside deviation, and a Sortino
        of infinity is not a number worth printing."""
        equity = [Decimal(100 + index) for index in range(400)]
        stamps = [START + timedelta(days=index) for index in range(400)]
        metrics = evaluate(equity, stamps)
        assert metrics.sortino is None
        assert "METRICS_NO_DOWNSIDE_OBSERVATIONS" in metrics.warnings

    def test_the_drawdown_names_its_peak_trough_and_recovery(self):
        values = [Decimal(str(value)) for value in [100, 120, 90, 110, 130]]
        stamps = [START + timedelta(days=index) for index in range(5)]
        drawdown = max_drawdown(values, stamps)
        assert drawdown.depth == pytest.approx(-0.25)
        assert drawdown.peak_timestamp == stamps[1]
        assert drawdown.trough_timestamp == stamps[2]
        assert drawdown.recovery_bars == 2

    def test_a_curve_that_never_recovers_says_so(self):
        values = [Decimal(str(value)) for value in [100, 120, 90, 95]]
        stamps = [START + timedelta(days=index) for index in range(4)]
        assert max_drawdown(values, stamps).recovery_bars is None

    def test_gross_results_are_flagged_in_the_metrics_too(self):
        equity = [Decimal(100 + index) for index in range(400)]
        stamps = [START + timedelta(days=index) for index in range(400)]
        metrics = evaluate(equity, stamps, gross_of_costs=True)
        assert metrics.gross_of_costs is True
        assert "METRICS_GROSS_OF_COSTS" in metrics.warnings

    def test_a_benchmark_gives_beta_and_tracking_error(self):
        equity = [Decimal(str(100 * (1.001**index))) for index in range(300)]
        stamps = [START + timedelta(days=index) for index in range(300)]
        benchmark = [0.001] * 299
        metrics = evaluate(equity, stamps, benchmark_returns=benchmark)
        assert metrics.benchmark is not None
        assert metrics.benchmark.observations == 299


class TestStrategies:
    def test_every_shipped_strategy_declares_its_features(self):
        for name in ("buy_and_hold", "moving_average_crossover", "momentum", "mean_reversion"):
            strategy = build(name)
            assert isinstance(strategy.features(), tuple)
            assert strategy.identifier.endswith("@1.0.0")

    def test_a_strategy_rebuilds_from_its_recorded_parameters(self):
        original = MovingAverageCrossover(fast=8, slow=21)
        rebuilt = build("moving_average_crossover", original.parameters())
        assert rebuilt == original

    def test_a_crossover_with_the_windows_the_wrong_way_round_is_refused(self):
        with pytest.raises(ValueError, match="shorter"):
            MovingAverageCrossover(fast=50, slow=20)

    def test_mean_reversion_needs_hysteresis(self):
        """A single threshold would trade on every crossing of it, and the
        turnover would be an artefact of the rule rather than of the signal."""
        with pytest.raises(ValueError, match="inside"):
            MeanReversion(entry=1.0, exit=1.5)

    def test_momentum_stays_flat_rather_than_shorting_unless_asked(self):
        falling = series([100 * (0.99**index) for index in range(120)])
        result = run(falling, Momentum(lookback=20, allow_short=False), BacktestConfig())
        assert all(fill.quantity >= 0 for fill in result.fills)

    def test_a_strategy_in_warmup_expresses_no_view(self):
        """``None`` and a zero weight are different: the first says no view, the
        second says hold nothing."""
        strategy = MovingAverageCrossover(fast=5, slow=20)
        result = run(rising(40), strategy, BacktestConfig())
        assert all(signal.timestamp >= START + timedelta(days=19) for signal in result.signals)
