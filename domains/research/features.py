"""Features, computed so that they cannot see the future.

Look-ahead bias is the defect that makes a backtest look brilliant and be
worthless, and it is almost never introduced deliberately. It arrives through a
rolling mean that includes the current bar's close when the decision is taken at
the open, through a z-score standardised over the whole sample, through a fill
priced at the same bar the signal was computed on.

So the guarantee here is structural rather than careful: **the value of any
feature at index i is a function of bars 0..i alone.** That is testable as a
property — computing a feature over a truncated series must give the same value
at its last index as computing it over the whole series — and
``tests/unit/test_features.py`` asserts exactly that for every feature shipped.

Prices stay exact ``Decimal`` in the series, because they are observations.
Features are ``float``, because they are estimates. That is the same boundary the
rest of the platform draws, and it is why a feature is never written back into a
price column.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

FEATURE_MODEL_VERSION = "research-features@1.0.0"

#: Trading days per year, for annualising a realised volatility measured on
#: daily bars. Stated rather than assumed: an annualised number computed on a
#: different bar frequency with this constant would be wrong by its square root.
TRADING_DAYS_PER_YEAR = 252


class FeatureError(ValueError):
    """A feature could not be computed as specified."""


@dataclass(frozen=True, slots=True)
class BarSeries:
    """One instrument's bars, in ascending time order.

    Order is an invariant rather than a convention: every feature here indexes
    backwards from the current bar, and a series out of order would silently
    make "the previous close" something else.
    """

    instrument_id: uuid.UUID
    timestamps: tuple[datetime, ...]
    open: tuple[Decimal, ...]
    high: tuple[Decimal, ...]
    low: tuple[Decimal, ...]
    close: tuple[Decimal, ...]
    volume: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        lengths = {
            len(self.timestamps),
            len(self.open),
            len(self.high),
            len(self.low),
            len(self.close),
            len(self.volume),
        }
        if len(lengths) != 1:
            raise FeatureError("bar columns have different lengths")
        if any(
            self.timestamps[index] >= self.timestamps[index + 1]
            for index in range(len(self.timestamps) - 1)
        ):
            raise FeatureError(
                "bars must be strictly ascending in time; every feature here indexes "
                "backwards, and an unordered series makes 'the previous close' "
                "something else"
            )
        if any(moment.tzinfo is None for moment in self.timestamps):
            raise FeatureError("bar timestamps must be timezone-aware")

    def __len__(self) -> int:
        return len(self.timestamps)

    def head(self, count: int) -> BarSeries:
        """The first ``count`` bars. Used by the look-ahead property test."""
        return BarSeries(
            instrument_id=self.instrument_id,
            timestamps=self.timestamps[:count],
            open=self.open[:count],
            high=self.high[:count],
            low=self.low[:count],
            close=self.close[:count],
            volume=self.volume[:count],
        )

    @property
    def closes(self) -> list[float]:
        return [float(value) for value in self.close]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One feature: what it is called, how it is computed, what it needs."""

    name: str
    compute: Callable[[BarSeries, int], float | None]
    #: Bars of history required before the feature has a value. Below it the
    #: feature is ``None`` rather than computed from a shorter window, because a
    #: 20-day mean of 3 days is not a 20-day mean.
    warmup: int
    description: str = ""

    def at(self, series: BarSeries, index: int) -> float | None:
        if index < self.warmup:
            return None
        value = self.compute(series, index)
        if value is None or not math.isfinite(value):
            return None
        return value


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    """Feature values aligned to a series' bars.

    ``values[name][i]`` is the feature at bar ``i``, or ``None`` where the
    feature had insufficient history. ``None`` rather than a forward- or
    back-filled number: a filled feature is an invented observation, and a
    strategy that traded on one traded on nothing.
    """

    instrument_id: uuid.UUID
    timestamps: tuple[datetime, ...]
    values: dict[str, list[float | None]] = field(default_factory=dict)
    model_version: str = FEATURE_MODEL_VERSION

    def at(self, index: int) -> dict[str, float | None]:
        return {name: series[index] for name, series in self.values.items()}

    def ready(self, index: int) -> bool:
        """Whether every feature has a value at this bar."""
        return all(series[index] is not None for series in self.values.values())

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "model_version": self.model_version,
            "features": sorted(self.values),
            "bars": len(self.timestamps),
        }


def compute(series: BarSeries, specs: Sequence[FeatureSpec]) -> FeatureFrame:
    """Compute every spec across every bar.

    Deliberately a simple double loop rather than a vectorised rolling window.
    A vectorised implementation is faster and is also where look-ahead hides —
    an off-by-one in a shift is invisible and changes everything. Each value here
    is computed from an explicit backward slice, which is checkable by eye and is
    what the property test pins.
    """
    return FeatureFrame(
        instrument_id=series.instrument_id,
        timestamps=series.timestamps,
        values={
            spec.name: [spec.at(series, index) for index in range(len(series))] for spec in specs
        },
    )


# ------------------------------------------------------------------ features
def _simple_return(series: BarSeries, index: int) -> float | None:
    previous = float(series.close[index - 1])
    if previous == 0:
        return None
    return float(series.close[index]) / previous - 1.0


def _log_return(series: BarSeries, index: int) -> float | None:
    previous = float(series.close[index - 1])
    current = float(series.close[index])
    if previous <= 0 or current <= 0:
        return None
    return math.log(current / previous)


def _sma(window: int) -> Callable[[BarSeries, int], float | None]:
    def compute_sma(series: BarSeries, index: int) -> float | None:
        # Inclusive of the current bar, which is the convention every moving
        # average is quoted on. A strategy deciding *at* this bar must therefore
        # act on the *next* one, which is the backtest engine's rule, not this
        # function's job to enforce.
        window_values = series.closes[index - window + 1 : index + 1]
        return sum(window_values) / window

    return compute_sma


def _ema(window: int) -> Callable[[BarSeries, int], float | None]:
    alpha = 2.0 / (window + 1.0)

    def compute_ema(series: BarSeries, index: int) -> float | None:
        closes = series.closes[: index + 1]
        value = sum(closes[:window]) / window
        for close in closes[window:]:
            value = alpha * close + (1.0 - alpha) * value
        return value

    return compute_ema


def _realised_volatility(window: int, annualise: bool) -> Callable[[BarSeries, int], float | None]:
    def compute_volatility(series: BarSeries, index: int) -> float | None:
        returns = [
            math.log(float(series.close[step]) / float(series.close[step - 1]))
            for step in range(index - window + 1, index + 1)
            if float(series.close[step - 1]) > 0 and float(series.close[step]) > 0
        ]
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        # Sample variance: the mean is estimated from the same window, so the
        # denominator is n-1. Using n understates dispersion, and on a short
        # window it understates it enough to matter.
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        deviation = math.sqrt(variance)
        return deviation * math.sqrt(TRADING_DAYS_PER_YEAR) if annualise else deviation

    return compute_volatility


def _momentum(window: int) -> Callable[[BarSeries, int], float | None]:
    def compute_momentum(series: BarSeries, index: int) -> float | None:
        past = float(series.close[index - window])
        if past == 0:
            return None
        return float(series.close[index]) / past - 1.0

    return compute_momentum


def _atr(window: int) -> Callable[[BarSeries, int], float | None]:
    def compute_atr(series: BarSeries, index: int) -> float | None:
        ranges = []
        for step in range(index - window + 1, index + 1):
            high = float(series.high[step])
            low = float(series.low[step])
            previous_close = float(series.close[step - 1])
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        return sum(ranges) / len(ranges) if ranges else None

    return compute_atr


def _zscore(window: int) -> Callable[[BarSeries, int], float | None]:
    def compute_zscore(series: BarSeries, index: int) -> float | None:
        window_values = series.closes[index - window + 1 : index + 1]
        mean = sum(window_values) / len(window_values)
        variance = sum((value - mean) ** 2 for value in window_values) / (len(window_values) - 1)
        deviation = math.sqrt(variance)
        if deviation <= 0:
            # A window with no dispersion gives no z-score, rather than a large
            # one manufactured from rounding noise.
            return None
        return (series.closes[index] - mean) / deviation

    return compute_zscore


def simple_return() -> FeatureSpec:
    return FeatureSpec("return", _simple_return, warmup=1, description="Close-to-close return.")


def log_return() -> FeatureSpec:
    return FeatureSpec("log_return", _log_return, warmup=1, description="Log close-to-close.")


def sma(window: int) -> FeatureSpec:
    return FeatureSpec(
        f"sma_{window}", _sma(window), warmup=window - 1, description=f"{window}-bar mean close."
    )


def ema(window: int) -> FeatureSpec:
    return FeatureSpec(
        f"ema_{window}", _ema(window), warmup=window - 1, description=f"{window}-bar EMA."
    )


def realised_volatility(window: int, annualise: bool = True) -> FeatureSpec:
    suffix = "ann" if annualise else "raw"
    return FeatureSpec(
        f"volatility_{window}_{suffix}",
        _realised_volatility(window, annualise),
        warmup=window,
        description=f"{window}-bar realised volatility of log returns.",
    )


def momentum(window: int) -> FeatureSpec:
    return FeatureSpec(
        f"momentum_{window}",
        _momentum(window),
        warmup=window,
        description=f"Return over the last {window} bars.",
    )


def average_true_range(window: int) -> FeatureSpec:
    return FeatureSpec(
        f"atr_{window}",
        _atr(window),
        warmup=window,
        description=f"{window}-bar average true range.",
    )


def zscore(window: int) -> FeatureSpec:
    return FeatureSpec(
        f"zscore_{window}",
        _zscore(window),
        warmup=window - 1,
        description=f"Close standardised over a trailing {window}-bar window.",
    )


#: Every feature the platform ships, by name, so a stored experiment can be
#: rebuilt from its recorded feature list rather than from code that has moved.
BUILDERS: dict[str, Callable[..., FeatureSpec]] = {
    "return": simple_return,
    "log_return": log_return,
    "sma": sma,
    "ema": ema,
    "volatility": realised_volatility,
    "momentum": momentum,
    "atr": average_true_range,
    "zscore": zscore,
}


def build(name: str, **parameters) -> FeatureSpec:
    builder = BUILDERS.get(name)
    if builder is None:
        raise FeatureError(f"unknown feature {name!r}; available: {', '.join(sorted(BUILDERS))}")
    return builder(**parameters)
