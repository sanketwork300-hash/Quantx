"""Backtest performance metrics, and the ones that refuse to be computed.

Every number here is a ratio of things that need saying out loud. A Sharpe ratio
is an excess return over *some* risk-free rate, annualised by *some* factor,
measured over *some* number of observations — and a Sharpe of 2.1 from eleven
weekly bars is not the same object as a Sharpe of 2.1 from six years of daily
ones, however identically they print.

So three rules run through this module.

**The annualisation factor is derived from the data, not assumed.** Bars are
timed; the median gap between them says how many there are in a year. A metric
annualised with 252 on a weekly series would be wrong by a factor of seven, and
nothing about the output would say so.

**The risk-free rate is supplied or it is zero and says so.** Sharpe against an
unstated rate is a number nobody can reconcile.

**A metric that needs more data than it has returns ``None``.** Not a small
number, not a clamped one. A CAGR extrapolated from three months is a statement
about a year that nobody observed.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import numpy as np

from quant.statistics.var import historical_tail_risk, losses_from_pnl

METRICS_MODEL_VERSION = "backtest-metrics@1.0.0"

SECONDS_PER_YEAR = 365.25 * 24 * 3600

#: Below this many returns, ratio metrics are reported but marked unreliable.
#: The same convention the surface characteristics use: the observation count
#: travels with the answer rather than the answer being withheld.
MIN_RELIABLE_OBSERVATIONS = 30

#: Below this fraction of a year, a compound annual growth rate is not computed.
#: Annualising a six-week return produces a number about a year nobody observed,
#: and it is invariably the largest number in the report.
MIN_YEARS_FOR_CAGR = 0.5


class MetricWarning:
    SHORT_SAMPLE = "METRICS_SHORT_SAMPLE"
    WINDOW_TOO_SHORT_FOR_CAGR = "METRICS_WINDOW_TOO_SHORT_FOR_CAGR"
    NO_DOWNSIDE_OBSERVATIONS = "METRICS_NO_DOWNSIDE_OBSERVATIONS"
    NO_DISPERSION = "METRICS_NO_DISPERSION"
    GROSS_OF_COSTS = "METRICS_GROSS_OF_COSTS"
    NO_BENCHMARK = "METRICS_NO_BENCHMARK"


@dataclass(frozen=True, slots=True)
class Drawdown:
    """The worst peak-to-trough fall, and when it happened."""

    depth: float
    peak_timestamp: datetime | None
    trough_timestamp: datetime | None
    #: Bars from trough back to the previous peak. ``None`` when the curve never
    #: recovered — which is a different statement from "recovered instantly".
    recovery_bars: int | None

    def to_dict(self) -> dict:
        return {
            "depth": self.depth,
            "peak_timestamp": self.peak_timestamp.isoformat() if self.peak_timestamp else None,
            "trough_timestamp": (
                self.trough_timestamp.isoformat() if self.trough_timestamp else None
            ),
            "recovery_bars": self.recovery_bars,
        }


@dataclass(frozen=True, slots=True)
class TradeStatistics:
    """What the trades themselves looked like."""

    count: int
    wins: int
    losses: int
    win_rate: float | None
    #: Gross profit over gross loss. ``None`` when nothing lost — an infinite
    #: profit factor is not a number worth printing.
    profit_factor: float | None
    average_trade: float | None
    best_trade: float | None
    worst_trade: float | None
    #: Traded notional over average equity, per year.
    turnover: float | None

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "average_trade": self.average_trade,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "turnover": self.turnover,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """Alpha, beta and the tracking of one series against another."""

    beta: float | None
    alpha_annualised: float | None
    tracking_error: float | None
    information_ratio: float | None
    correlation: float | None
    observations: int

    def to_dict(self) -> dict:
        return {
            "beta": self.beta,
            "alpha_annualised": self.alpha_annualised,
            "tracking_error": self.tracking_error,
            "information_ratio": self.information_ratio,
            "correlation": self.correlation,
            "observations": self.observations,
        }


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """A run's performance, with every convention it depended on stated."""

    observations: int
    years: float
    #: Bars per year, measured from the timestamps rather than assumed.
    periods_per_year: float
    risk_free_rate: float
    #: True when no cost schedule was modelled, so every return here is gross.
    gross_of_costs: bool

    total_return: float
    cagr: float | None
    annualised_volatility: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    max_drawdown: Drawdown
    value_at_risk: dict | None
    trades: TradeStatistics
    benchmark: BenchmarkComparison | None = None
    warnings: tuple[str, ...] = ()
    model_version: str = METRICS_MODEL_VERSION

    @property
    def is_reliable(self) -> bool:
        return self.observations >= MIN_RELIABLE_OBSERVATIONS

    def to_dict(self) -> dict:
        return {
            "observations": self.observations,
            "is_reliable": self.is_reliable,
            "years": self.years,
            "periods_per_year": self.periods_per_year,
            "risk_free_rate": self.risk_free_rate,
            "gross_of_costs": self.gross_of_costs,
            "total_return": self.total_return,
            "cagr": self.cagr,
            "annualised_volatility": self.annualised_volatility,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "max_drawdown": self.max_drawdown.to_dict(),
            "value_at_risk": self.value_at_risk,
            "trades": self.trades.to_dict(),
            "benchmark": self.benchmark.to_dict() if self.benchmark else None,
            "warnings": list(self.warnings),
            "model_version": self.model_version,
            "interpretation": {
                "periods_per_year": (
                    "Measured from the median gap between bars, not assumed. Every "
                    "annualised figure here scales by its square root."
                ),
                "gross_of_costs": (
                    "True means no cost schedule was supplied, so returns are before "
                    "brokerage, exchange charges and statutory levies."
                ),
            },
        }


def periods_per_year(timestamps: Sequence[datetime]) -> float:
    """Bars per year, from the median gap between them.

    Median rather than mean because a market closure or a data gap is a long
    interval that would drag a mean and change every annualised number in the
    report.
    """
    if len(timestamps) < 2:
        return 0.0
    gaps = [
        (timestamps[index] - timestamps[index - 1]).total_seconds()
        for index in range(1, len(timestamps))
    ]
    median = statistics.median(gaps)
    if median <= 0:
        return 0.0
    return SECONDS_PER_YEAR / median


def returns_from(equity: Sequence[Decimal]) -> list[float]:
    """Simple period returns of an equity curve."""
    values = [float(value) for value in equity]
    return [
        values[index] / values[index - 1] - 1.0
        for index in range(1, len(values))
        if values[index - 1] != 0
    ]


def max_drawdown(equity: Sequence[Decimal], timestamps: Sequence[datetime]) -> Drawdown:
    """The worst peak-to-trough fall, with the dates and the recovery."""
    if not equity:
        return Drawdown(0.0, None, None, None)

    values = [float(value) for value in equity]
    peak = values[0]
    peak_index = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0

    for index, value in enumerate(values):
        if value > peak:
            peak = value
            peak_index = index
        if peak > 0:
            fall = value / peak - 1.0
            if fall < worst:
                worst = fall
                worst_peak = peak_index
                worst_trough = index

    recovery = None
    if worst < 0:
        target = values[worst_peak]
        for index in range(worst_trough, len(values)):
            if values[index] >= target:
                recovery = index - worst_trough
                break

    return Drawdown(
        depth=worst,
        peak_timestamp=timestamps[worst_peak] if timestamps else None,
        trough_timestamp=timestamps[worst_trough] if timestamps else None,
        recovery_bars=recovery,
    )


def _annualised_volatility(returns: Sequence[float], factor: float) -> float | None:
    if len(returns) < 2 or factor <= 0:
        return None
    deviation = statistics.stdev(returns)
    return deviation * math.sqrt(factor)


def _sharpe(returns: Sequence[float], factor: float, risk_free: float) -> float | None:
    if len(returns) < 2 or factor <= 0:
        return None
    excess = [value - risk_free / factor for value in returns]
    deviation = statistics.stdev(excess)
    if deviation <= 0:
        return None
    return (statistics.fmean(excess) / deviation) * math.sqrt(factor)


def _sortino(returns: Sequence[float], factor: float, risk_free: float) -> float | None:
    """Downside deviation in the denominator, about the target rather than the
    mean — which is what makes it a Sortino rather than a one-sided Sharpe."""
    if len(returns) < 2 or factor <= 0:
        return None
    target = risk_free / factor
    excess = [value - target for value in returns]
    downside = [value for value in excess if value < 0]
    if not downside:
        return None
    deviation = math.sqrt(sum(value**2 for value in downside) / len(excess))
    if deviation <= 0:
        return None
    return (statistics.fmean(excess) / deviation) * math.sqrt(factor)


def _benchmark(
    returns: Sequence[float], benchmark: Sequence[float], factor: float, risk_free: float
) -> BenchmarkComparison:
    """Ordinary least squares of the run's excess returns on the benchmark's."""
    count = min(len(returns), len(benchmark))
    if count < 2:
        return BenchmarkComparison(None, None, None, None, None, count)

    strategy = np.asarray(returns[:count], dtype=float) - risk_free / factor
    market = np.asarray(benchmark[:count], dtype=float) - risk_free / factor
    variance = float(np.var(market, ddof=1))
    if variance <= 0:
        return BenchmarkComparison(None, None, None, None, None, count)

    beta = float(np.cov(strategy, market, ddof=1)[0, 1] / variance)
    alpha = float(np.mean(strategy) - beta * np.mean(market)) * factor
    active = strategy - market
    tracking = float(np.std(active, ddof=1)) * math.sqrt(factor)
    information = (float(np.mean(active)) * factor / tracking) if tracking > 0 else None
    deviations = float(np.std(strategy, ddof=1)) * float(np.std(market, ddof=1))
    correlation = (
        float(np.cov(strategy, market, ddof=1)[0, 1] / deviations) if deviations > 0 else None
    )

    return BenchmarkComparison(
        beta=beta,
        alpha_annualised=alpha,
        tracking_error=tracking,
        information_ratio=information,
        correlation=correlation,
        observations=count,
    )


def trade_statistics(
    realised: Sequence[float], traded_notional: Decimal, average_equity: Decimal, years: float
) -> TradeStatistics:
    """Per-trade outcomes, and turnover as traded notional over equity per year."""
    wins = [value for value in realised if value > 0]
    losses = [value for value in realised if value < 0]
    gross_loss = abs(sum(losses))

    turnover = None
    if average_equity > 0 and years > 0:
        turnover = float(traded_notional) / float(average_equity) / years

    return TradeStatistics(
        count=len(realised),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(realised)) if realised else None,
        # None rather than infinity where nothing lost: an infinite profit
        # factor is not a number worth printing beside finite ones.
        profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else None,
        average_trade=statistics.fmean(realised) if realised else None,
        best_trade=max(realised) if realised else None,
        worst_trade=min(realised) if realised else None,
        turnover=turnover,
    )


def evaluate(
    equity: Sequence[Decimal],
    timestamps: Sequence[datetime],
    realised_trades: Sequence[float] = (),
    traded_notional: Decimal = Decimal(0),
    risk_free_rate: float = 0.0,
    gross_of_costs: bool = True,
    benchmark_returns: Sequence[float] | None = None,
    confidence: float = 0.95,
) -> PerformanceMetrics:
    """Everything about a run's performance, with its conventions attached."""
    warnings: list[str] = []
    if gross_of_costs:
        warnings.append(MetricWarning.GROSS_OF_COSTS)

    returns = returns_from(equity)
    factor = periods_per_year(timestamps)
    span_years = (
        (timestamps[-1] - timestamps[0]).total_seconds() / SECONDS_PER_YEAR
        if len(timestamps) > 1
        else 0.0
    )

    if len(returns) < MIN_RELIABLE_OBSERVATIONS:
        warnings.append(MetricWarning.SHORT_SAMPLE)

    start = float(equity[0]) if equity else 0.0
    finish = float(equity[-1]) if equity else 0.0
    total_return = (finish / start - 1.0) if start else 0.0

    cagr = None
    if span_years >= MIN_YEARS_FOR_CAGR and start > 0 and finish > 0:
        cagr = (finish / start) ** (1.0 / span_years) - 1.0
    elif span_years > 0:
        warnings.append(MetricWarning.WINDOW_TOO_SHORT_FOR_CAGR)

    volatility = _annualised_volatility(returns, factor)
    if volatility is None and len(returns) >= 2:
        warnings.append(MetricWarning.NO_DISPERSION)

    sortino = _sortino(returns, factor, risk_free_rate)
    if sortino is None and len(returns) >= 2:
        warnings.append(MetricWarning.NO_DOWNSIDE_OBSERVATIONS)

    drawdown = max_drawdown(equity, timestamps)
    calmar = None
    if cagr is not None and drawdown.depth < 0:
        calmar = cagr / abs(drawdown.depth)

    tail = None
    if len(returns) >= 2:
        tail = historical_tail_risk(losses_from_pnl(returns), confidence).to_dict()

    comparison = None
    if benchmark_returns is not None:
        comparison = _benchmark(returns, benchmark_returns, factor, risk_free_rate)
    else:
        warnings.append(MetricWarning.NO_BENCHMARK)

    average_equity = (
        Decimal(str(statistics.fmean([float(value) for value in equity]))) if equity else Decimal(0)
    )

    return PerformanceMetrics(
        observations=len(returns),
        years=span_years,
        periods_per_year=factor,
        risk_free_rate=risk_free_rate,
        gross_of_costs=gross_of_costs,
        total_return=total_return,
        cagr=cagr,
        annualised_volatility=volatility,
        sharpe=_sharpe(returns, factor, risk_free_rate),
        sortino=sortino,
        calmar=calmar,
        max_drawdown=drawdown,
        value_at_risk=tail,
        trades=trade_statistics(realised_trades, traded_notional, average_equity, span_years),
        benchmark=comparison,
        warnings=tuple(sorted(set(warnings))),
    )
