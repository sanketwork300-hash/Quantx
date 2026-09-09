"""Strategies, and the benchmarks a backtest engine has to be checked against.

Build spec §44 asks for buy-and-hold, a moving average, momentum and mean
reversion — not because they are good, but because they are *known*. A
buy-and-hold backtest whose return does not match the instrument's own return
over the same window has an accounting bug, and no amount of staring at a
Sharpe ratio would have found it.

Every strategy produces a **target weight**, not an instruction. See
``models.TargetPosition`` for why that distinction is kept.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from domains.research.features import FeatureSpec, ema, momentum, sma, zscore
from domains.research.models import Signal, TargetPosition


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """What a strategy sees at one bar.

    Deliberately narrow. It gets the features for *this* bar and the weight it
    currently holds — not the series, not the future, and not the whole book.
    A strategy that cannot reach the future cannot leak it.
    """

    instrument_id: uuid.UUID
    timestamp: datetime
    features: dict[str, float | None]
    #: The weight the simulated book currently holds in this instrument.
    current_weight: Decimal
    #: Bars elapsed since the run began, for strategies that need a warm-up.
    bar_index: int


class Strategy(ABC):
    """A rule that turns features into a target weight."""

    name: ClassVar[str] = "abstract"
    version: ClassVar[str] = "1.0.0"

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"

    @abstractmethod
    def features(self) -> tuple[FeatureSpec, ...]:
        """The features this strategy needs.

        Declared rather than requested ad hoc so the engine computes exactly
        what will be used, and so a stored experiment records the feature set
        the result actually depended on.
        """

    @abstractmethod
    def evaluate(self, context: StrategyContext) -> Signal | None:
        """The target position at this bar, or ``None`` to leave the book alone.

        ``None`` and a zero-weight signal are different: the first says "no view
        expressed", the second says "hold nothing". A strategy in warm-up
        returns the first.
        """

    def parameters(self) -> dict:
        """Everything needed to rebuild this strategy. Goes into the record."""
        return {}


@dataclass(frozen=True, slots=True)
class BuyAndHold(Strategy):
    """Hold a fixed weight from the first bar. The accounting benchmark.

    Its return has to match the instrument's own return over the window, less
    costs. When it does not, the bug is in the engine and not in the idea.
    """

    weight: Decimal = Decimal(1)
    name: ClassVar[str] = "buy_and_hold"
    version: ClassVar[str] = "1.0.0"

    def features(self) -> tuple[FeatureSpec, ...]:
        return ()

    def evaluate(self, context: StrategyContext) -> Signal | None:
        # Once, on the first bar, and then no view at all. A constant *weight*
        # would rebalance as the price moved, and the point of this strategy is
        # to be the accounting benchmark: its return has to be the instrument's
        # return, not the instrument's return plus a rebalancing artefact.
        if context.bar_index > 0:
            return None
        return Signal(
            instrument_id=context.instrument_id,
            timestamp=context.timestamp,
            position=TargetPosition.LONG if self.weight > 0 else TargetPosition.FLAT,
            target_weight=self.weight,
            reason="buy once on the first bar and hold to the end",
        )

    def parameters(self) -> dict:
        return {"weight": format(self.weight, "f")}


@dataclass(frozen=True, slots=True)
class MovingAverageCrossover(Strategy):
    """Long while the fast average is above the slow one, flat otherwise."""

    fast: int = 20
    slow: int = 50
    weight: Decimal = Decimal(1)
    use_ema: bool = False
    name: ClassVar[str] = "moving_average_crossover"
    version: ClassVar[str] = "1.0.0"

    def __post_init__(self) -> None:
        if self.fast >= self.slow:
            raise ValueError(
                f"the fast window ({self.fast}) must be shorter than the slow one "
                f"({self.slow}); otherwise the crossover has no meaning"
            )

    def _names(self) -> tuple[str, str]:
        prefix = "ema" if self.use_ema else "sma"
        return f"{prefix}_{self.fast}", f"{prefix}_{self.slow}"

    def features(self) -> tuple[FeatureSpec, ...]:
        builder = ema if self.use_ema else sma
        return (builder(self.fast), builder(self.slow))

    def evaluate(self, context: StrategyContext) -> Signal | None:
        fast_name, slow_name = self._names()
        fast = context.features.get(fast_name)
        slow = context.features.get(slow_name)
        if fast is None or slow is None:
            return None

        long = fast > slow
        return Signal(
            instrument_id=context.instrument_id,
            timestamp=context.timestamp,
            position=TargetPosition.LONG if long else TargetPosition.FLAT,
            target_weight=self.weight if long else Decimal(0),
            reason=(
                f"{fast_name} {fast:.4f} {'above' if long else 'below'} {slow_name} {slow:.4f}"
            ),
        )

    def parameters(self) -> dict:
        return {
            "fast": self.fast,
            "slow": self.slow,
            "weight": format(self.weight, "f"),
            "use_ema": self.use_ema,
        }


@dataclass(frozen=True, slots=True)
class Momentum(Strategy):
    """Long after a positive lookback return, short after a negative one."""

    lookback: int = 60
    weight: Decimal = Decimal(1)
    allow_short: bool = False
    name: ClassVar[str] = "momentum"
    version: ClassVar[str] = "1.0.0"

    def features(self) -> tuple[FeatureSpec, ...]:
        return (momentum(self.lookback),)

    def evaluate(self, context: StrategyContext) -> Signal | None:
        value = context.features.get(f"momentum_{self.lookback}")
        if value is None:
            return None

        if value > 0:
            position, weight = TargetPosition.LONG, self.weight
        elif self.allow_short:
            position, weight = TargetPosition.SHORT, -self.weight
        else:
            position, weight = TargetPosition.FLAT, Decimal(0)

        return Signal(
            instrument_id=context.instrument_id,
            timestamp=context.timestamp,
            position=position,
            target_weight=weight,
            reason=f"{self.lookback}-bar return {value:+.4f}",
        )

    def parameters(self) -> dict:
        return {
            "lookback": self.lookback,
            "weight": format(self.weight, "f"),
            "allow_short": self.allow_short,
        }


@dataclass(frozen=True, slots=True)
class MeanReversion(Strategy):
    """Lean against a stretched trailing z-score."""

    window: int = 20
    entry: float = 1.5
    exit: float = 0.5
    weight: Decimal = Decimal(1)
    allow_short: bool = True
    name: ClassVar[str] = "mean_reversion"
    version: ClassVar[str] = "1.0.0"

    def __post_init__(self) -> None:
        if self.exit >= self.entry:
            raise ValueError(
                f"the exit threshold ({self.exit}) must be inside the entry one "
                f"({self.entry}); otherwise a position closes the bar it opens"
            )

    def features(self) -> tuple[FeatureSpec, ...]:
        return (zscore(self.window),)

    def evaluate(self, context: StrategyContext) -> Signal | None:
        value = context.features.get(f"zscore_{self.window}")
        if value is None:
            return None

        holding = context.current_weight
        # Hysteresis: enter outside ``entry``, leave only inside ``exit``. A
        # single threshold would trade on every crossing of it, and the turnover
        # would be an artefact of the rule rather than of the signal.
        if value <= -self.entry:
            position, weight = TargetPosition.LONG, self.weight
        elif value >= self.entry and self.allow_short:
            position, weight = TargetPosition.SHORT, -self.weight
        elif abs(value) <= self.exit:
            position, weight = TargetPosition.FLAT, Decimal(0)
        else:
            position = (
                TargetPosition.LONG
                if holding > 0
                else TargetPosition.SHORT
                if holding < 0
                else TargetPosition.FLAT
            )
            weight = holding

        return Signal(
            instrument_id=context.instrument_id,
            timestamp=context.timestamp,
            position=position,
            target_weight=weight,
            reason=f"trailing z-score {value:+.3f}",
        )

    def parameters(self) -> dict:
        return {
            "window": self.window,
            "entry": self.entry,
            "exit": self.exit,
            "weight": format(self.weight, "f"),
            "allow_short": self.allow_short,
        }


#: Strategies the platform ships, by name. A stored experiment records the name
#: and the parameters, so the run can be rebuilt without the code that made it.
REGISTRY: dict[str, type[Strategy]] = {
    BuyAndHold.name: BuyAndHold,
    MovingAverageCrossover.name: MovingAverageCrossover,
    Momentum.name: Momentum,
    MeanReversion.name: MeanReversion,
}


def build(name: str, parameters: dict | None = None) -> Strategy:
    """Rebuild a strategy from its recorded name and parameters."""
    strategy_type = REGISTRY.get(name)
    if strategy_type is None:
        raise ValueError(f"unknown strategy {name!r}; available: {', '.join(sorted(REGISTRY))}")
    payload = dict(parameters or {})
    for key in ("weight",):
        if key in payload:
            payload[key] = Decimal(str(payload[key]))
    return strategy_type(**payload)
