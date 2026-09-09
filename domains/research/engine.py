"""The backtest engine.

An explicit bar loop, and the loop's shape *is* the anti-look-ahead argument:

```
for each bar t:
    features  = frame at t          # computed from bars 0..t alone
    signal    = strategy(features)  # a decision taken with bar t's information
    fill      = bar t+1             # at its open or its close, never bar t's
    book      = book + fill
    equity    = mark at bar t+1's close
```

A decision that used bar ``t``'s close cannot be filled at bar ``t``'s close, so
that is not an option the engine offers. Making it configurable would turn the
most common backtest error into a setting, and somebody would set it.

The other structural choice: **the last bar is never traded on**. A signal at the
final bar has no next bar to fill against, so it is recorded and not executed.
Filling it at the same bar would produce a free trade at a known price, which is
the same error wearing a different hat.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from domains.research.costs import (
    NO_COST_MODEL,
    NO_SLIPPAGE_MODEL,
    CostSchedule,
    SlippageModel,
)
from domains.research.features import BarSeries, FeatureFrame, compute
from domains.research.models import Book, ExecutionTiming, Fill, Signal
from domains.research.strategies import Strategy, StrategyContext

ENGINE_VERSION = "backtest-engine@1.0.0"


class BacktestWarning:
    #: No cost schedule was supplied, so every return is gross.
    COSTS_NOT_MODELLED = "BACKTEST_COSTS_NOT_MODELLED"
    #: No slippage was modelled, so fills are at prices no order achieves.
    SLIPPAGE_NOT_MODELLED = "BACKTEST_SLIPPAGE_NOT_MODELLED"
    #: A target weight was cut by a limit. Reported because a strategy whose
    #: weights are being clamped is not the strategy that was described.
    WEIGHT_LIMITED = "BACKTEST_WEIGHT_LIMITED"
    #: The book's cash went negative: the run used leverage.
    LEVERAGE_USED = "BACKTEST_LEVERAGE_USED"
    #: A signal on the final bar could not be executed.
    FINAL_SIGNAL_NOT_EXECUTED = "BACKTEST_FINAL_SIGNAL_NOT_EXECUTED"
    #: Rows the warehouse flagged were included in the run.
    FLAGGED_BARS_INCLUDED = "BACKTEST_FLAGGED_BARS_INCLUDED"


class BacktestError(ValueError):
    """The run could not be set up as specified."""


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything about a run that is not the data or the strategy."""

    initial_cash: Decimal = Decimal(1_000_000)
    timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN
    costs: CostSchedule = NO_COST_MODEL
    slippage: SlippageModel = NO_SLIPPAGE_MODEL
    #: Largest absolute weight in any one instrument. A strategy asking for more
    #: is clamped and the clamp is reported.
    max_position_weight: Decimal = Decimal(1)
    #: Largest total absolute exposure as a fraction of equity.
    max_gross_exposure: Decimal = Decimal(1)
    #: Trades below this fraction of equity are skipped. Without it a strategy
    #: rebalances a hundredth of a percent every bar and pays for it.
    rebalance_threshold: Decimal = Decimal("0.001")
    #: Whether whole units are required. Equities and futures trade in whole
    #: contracts; a backtest filling 13.482 shares is measuring something that
    #: could not have happened.
    whole_units: bool = True
    #: Include bars the warehouse validator flagged. Default False, and the
    #: choice is recorded either way — a backtest whose best day was a bad tick
    #: should be able to say so.
    include_flagged_bars: bool = False

    def to_dict(self) -> dict:
        return {
            "initial_cash": format(self.initial_cash, "f"),
            "timing": str(self.timing),
            "costs": self.costs.to_dict(),
            "slippage": self.slippage.to_dict(),
            "max_position_weight": format(self.max_position_weight, "f"),
            "max_gross_exposure": format(self.max_gross_exposure, "f"),
            "rebalance_threshold": format(self.rebalance_threshold, "f"),
            "whole_units": self.whole_units,
            "include_flagged_bars": self.include_flagged_bars,
            "engine_version": ENGINE_VERSION,
        }


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One mark of the book."""

    timestamp: datetime
    equity: Decimal
    cash: Decimal
    market_value: Decimal
    gross_exposure: Decimal
    #: Costs paid on this bar, so the equity curve's drag is attributable rather
    #: than only visible as a shortfall against a gross curve.
    costs: Decimal = Decimal(0)
    slippage: Decimal = Decimal(0)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "equity": format(self.equity, "f"),
            "cash": format(self.cash, "f"),
            "market_value": format(self.market_value, "f"),
            "gross_exposure": format(self.gross_exposure, "f"),
            "costs": format(self.costs, "f"),
            "slippage": format(self.slippage, "f"),
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """A run: its curve, its trades, and everything needed to argue with it."""

    strategy: str
    instrument_id: uuid.UUID
    start: datetime
    end: datetime
    config: BacktestConfig
    equity_curve: tuple[EquityPoint, ...]
    fills: tuple[Fill, ...]
    signals: tuple[Signal, ...]
    warnings: tuple[str, ...] = ()
    bars_in: int = 0
    bars_used: int = 0
    bars_flagged: int = 0
    #: Per-instrument P&L at the final mark, so attribution reconciles against
    #: the same book the equity curve was drawn from rather than a second one
    #: rebuilt from the fills.
    realised_by_instrument: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    unrealised_by_instrument: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    #: Every trade's realised profit, in order, for the per-trade statistics.
    realised_trades: tuple[Decimal, ...] = ()

    @property
    def initial_equity(self) -> Decimal:
        return self.config.initial_cash

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1].equity if self.equity_curve else self.initial_equity

    @property
    def total_costs(self) -> Decimal:
        return sum((fill.cost.total for fill in self.fills), Decimal(0))

    @property
    def traded_notional(self) -> Decimal:
        return sum((abs(fill.notional) for fill in self.fills), Decimal(0))

    @property
    def total_slippage(self) -> Decimal:
        return sum((fill.slippage_cost for fill in self.fills), Decimal(0))

    @property
    def gross_of_costs(self) -> bool:
        """True when no cost schedule was supplied.

        Carried on the result rather than left to be worked out from the config,
        because every return figure this run produces means something different
        depending on it.
        """
        return not self.config.costs.models_costs

    def to_dict(self, include_curve: bool = True, include_fills: bool = True) -> dict:
        payload = {
            "strategy": self.strategy,
            "instrument_id": str(self.instrument_id),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "config": self.config.to_dict(),
            "initial_equity": format(self.initial_equity, "f"),
            "final_equity": format(self.final_equity, "f"),
            "total_costs": format(self.total_costs, "f"),
            "total_slippage": format(self.total_slippage, "f"),
            "gross_of_costs": self.gross_of_costs,
            "counts": {
                "bars_in": self.bars_in,
                "bars_used": self.bars_used,
                "bars_flagged": self.bars_flagged,
                "signals": len(self.signals),
                "fills": len(self.fills),
            },
            "warnings": list(self.warnings),
        }
        if include_curve:
            payload["equity_curve"] = [point.to_dict() for point in self.equity_curve]
        if include_fills:
            payload["fills"] = [fill.to_dict() for fill in self.fills]
        return payload


def run(
    series: BarSeries,
    strategy: Strategy,
    config: BacktestConfig | None = None,
    flagged: Sequence[bool] | None = None,
) -> BacktestResult:
    """Run one strategy over one instrument's bars."""
    settings = config or BacktestConfig()
    if len(series) < 2:
        raise BacktestError(
            "a backtest needs at least two bars: one to decide on and one to fill against"
        )

    flags = list(flagged or [False] * len(series))
    bars_flagged = sum(1 for value in flags if value)

    frame: FeatureFrame = compute(series, strategy.features())
    book = Book(cash=settings.initial_cash)
    curve: list[EquityPoint] = []
    fills: list[Fill] = []
    signals: list[Signal] = []
    realised_trades: list[Decimal] = []
    warnings: set[str] = set()

    if not settings.costs.models_costs:
        warnings.add(BacktestWarning.COSTS_NOT_MODELLED)
    if not settings.slippage.models_slippage:
        warnings.add(BacktestWarning.SLIPPAGE_NOT_MODELLED)
    if bars_flagged and settings.include_flagged_bars:
        warnings.add(BacktestWarning.FLAGGED_BARS_INCLUDED)

    bars_used = 0

    for index in range(len(series)):
        price_now = series.close[index]
        prices = {series.instrument_id: price_now}
        equity = book.equity(prices)

        if flags[index] and not settings.include_flagged_bars:
            # The bar is still marked — dropping it from the curve would make
            # the equity series discontinuous — but no decision is taken on it.
            curve.append(_mark(series.timestamps[index], book, prices))
            continue

        bars_used += 1
        position = book.position(series.instrument_id)
        current_weight = (position.market_value(price_now) / equity) if equity != 0 else Decimal(0)

        signal = strategy.evaluate(
            StrategyContext(
                instrument_id=series.instrument_id,
                timestamp=series.timestamps[index],
                features=frame.at(index),
                current_weight=current_weight,
                bar_index=index,
            )
        )

        bar_cost = Decimal(0)
        bar_slippage = Decimal(0)

        if signal is not None:
            signals.append(signal)

            if index + 1 >= len(series):
                # No bar left to fill against. Recorded, not executed: filling it
                # here would be a free trade at a price the decision already saw.
                warnings.add(BacktestWarning.FINAL_SIGNAL_NOT_EXECUTED)
            else:
                fill, limited = _execute(series, index, signal, book, equity, settings)
                if limited:
                    warnings.add(BacktestWarning.WEIGHT_LIMITED)
                if fill is not None:
                    realised = book.apply(fill)
                    if realised != 0:
                        realised_trades.append(realised)
                    fills.append(fill)
                    bar_cost = fill.cost.total
                    bar_slippage = fill.slippage_cost

        if book.cash < 0:
            warnings.add(BacktestWarning.LEVERAGE_USED)

        curve.append(_mark(series.timestamps[index], book, prices, bar_cost, bar_slippage))

    final_price = series.close[-1]
    return BacktestResult(
        strategy=strategy.identifier,
        instrument_id=series.instrument_id,
        start=series.timestamps[0],
        end=series.timestamps[-1],
        config=settings,
        equity_curve=tuple(curve),
        fills=tuple(fills),
        signals=tuple(signals),
        warnings=tuple(sorted(warnings)),
        bars_in=len(series),
        bars_used=bars_used,
        bars_flagged=bars_flagged,
        realised_by_instrument={
            instrument_id: position.realised_pnl
            for instrument_id, position in book.positions.items()
        },
        unrealised_by_instrument={
            instrument_id: position.unrealised_pnl(final_price)
            for instrument_id, position in book.positions.items()
        },
        realised_trades=tuple(realised_trades),
    )


def _mark(
    timestamp: datetime,
    book: Book,
    prices: dict[uuid.UUID, Decimal],
    costs: Decimal = Decimal(0),
    slippage: Decimal = Decimal(0),
) -> EquityPoint:
    return EquityPoint(
        timestamp=timestamp,
        equity=book.equity(prices),
        cash=book.cash,
        market_value=book.market_value(prices),
        gross_exposure=book.gross_exposure(prices),
        costs=costs,
        slippage=slippage,
    )


def _execute(
    series: BarSeries,
    index: int,
    signal: Signal,
    book: Book,
    equity: Decimal,
    config: BacktestConfig,
) -> tuple[Fill | None, bool]:
    """Turn a target weight into a fill against the *next* bar.

    Returns ``(fill, was_limited)``. ``None`` for the fill means the trade was
    below the rebalance threshold or rounded to nothing — which is a real
    outcome, not an error: a strategy asking for a hundredth of a share on a
    million-rupee book has asked for nothing.
    """
    target = signal.target_weight
    limited = False

    if abs(target) > config.max_position_weight:
        target = config.max_position_weight * (Decimal(1) if target > 0 else Decimal(-1))
        limited = True
    if abs(target) > config.max_gross_exposure:
        target = config.max_gross_exposure * (Decimal(1) if target > 0 else Decimal(-1))
        limited = True

    reference = (
        series.open[index + 1]
        if config.timing is ExecutionTiming.NEXT_OPEN
        else series.close[index + 1]
    )
    if reference <= 0 or equity <= 0:
        return None, limited

    desired_quantity = (target * equity) / reference
    if config.whole_units:
        desired_quantity = desired_quantity.quantize(Decimal(1), rounding=ROUND_DOWN)

    position = book.position(series.instrument_id)
    delta = desired_quantity - position.quantity
    if delta == 0:
        return None, limited

    if abs(delta * reference) < config.rebalance_threshold * equity:
        return None, limited

    is_buy = delta > 0
    price = config.slippage.apply(reference, is_buy)
    cost = config.costs.charge(price, delta, is_buy)

    return (
        Fill(
            instrument_id=series.instrument_id,
            timestamp=series.timestamps[index + 1],
            quantity=delta,
            price=price,
            reference_price=reference,
            cost=cost,
            reason=signal.reason,
        ),
        limited,
    )
