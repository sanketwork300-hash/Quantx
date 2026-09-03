"""Execution benchmarks, each carrying the window, source and method it used.

A VWAP without its window is not a benchmark, it is a number. Every benchmark
here reports the interval it was measured over, where the observations came
from, how they were combined, and how well they actually covered the interval —
and a benchmark that the available data cannot support is returned as
*unavailable with a reason* rather than computed from three ticks.

That last rule is the whole point of this module. The platform's market data is
whatever the user has ingested, which for most windows is sparse. Silently
averaging four observations into an "interval VWAP" would produce a
confident-looking benchmark that no market ever traded at, and every cost
measured against it would inherit the error.
"""

from __future__ import annotations

import uuid
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

#: A quote older than this is not "the prevailing mid": it is the last thing the
#: platform happened to see. A stated convention and a parameter of the run.
DEFAULT_STALENESS_TOLERANCE_SECONDS = 300.0

#: An interval statistic needs at least this many observations before it
#: describes an interval rather than a handful of moments inside one.
MIN_INTERVAL_OBSERVATIONS = 4

#: And the observations must span at least this much of the window, or what was
#: measured is a corner of the interval rather than the interval.
MIN_SPAN_RATIO = 0.60


class BenchmarkKind(StrEnum):
    ARRIVAL = "ARRIVAL"
    DECISION = "DECISION"
    PREVAILING_MID = "PREVAILING_MID"
    INTERVAL_VWAP = "INTERVAL_VWAP"
    INTERVAL_TWAP = "INTERVAL_TWAP"
    CLOSE = "CLOSE"


class BenchmarkMethod(StrEnum):
    QUOTE_MID_AT_TIMESTAMP = "QUOTE_MID_AT_TIMESTAMP"
    #: The first fill's own price, used only when nothing else can stand in for
    #: arrival, and always flagged.
    FIRST_FILL_PROXY = "FIRST_FILL_PROXY"
    #: Piecewise-constant in time: each observation holds until the next.
    TIME_WEIGHTED_STEP = "TIME_WEIGHTED_STEP"
    VOLUME_WEIGHTED = "VOLUME_WEIGHTED"
    LAST_OBSERVATION_IN_WINDOW = "LAST_OBSERVATION_IN_WINDOW"
    SUPPLIED_BY_CALLER = "SUPPLIED_BY_CALLER"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class BenchmarkFlag(StrEnum):
    ARRIVAL_PROXY_USED = "ARRIVAL_PROXY_USED"
    STALE_REFERENCE_QUOTE = "TCA_STALE_REFERENCE_QUOTE"
    DATA_COVERAGE_LOW = "TCA_DATA_COVERAGE_LOW"
    NO_MARKET_DATA = "TCA_NO_MARKET_DATA_IN_WINDOW"
    NO_VOLUME_DATA = "TCA_NO_INTERVAL_VOLUME_DATA"
    NO_DECISION_TIMESTAMP = "TCA_NO_DECISION_TIMESTAMP"
    WINDOW_NOT_BRACKETED = "TCA_WINDOW_NOT_BRACKETED"


@dataclass(frozen=True, slots=True)
class MarketObservation:
    """One thing the platform actually saw, and where it saw it."""

    timestamp: datetime
    price: Decimal
    source: str
    #: Interval volume. ``None`` means the source carries no volume that can be
    #: attributed to an interval, which is different from a volume of zero.
    volume: Decimal | None = None
    #: Quoted ask minus bid at this moment. ``None`` when the source is a single
    #: price with no two-sided market behind it, in which case no spread cost
    #: can be attributed and the decomposition says so.
    spread: Decimal | None = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "price": format(self.price, "f"),
            "volume": format(self.volume, "f") if self.volume is not None else None,
            "spread": format(self.spread, "f") if self.spread is not None else None,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class DataCoverage:
    """How well the observations cover the interval that was asked about."""

    observations: int
    window_seconds: float
    #: Fraction of the window between the first and last observation.
    span_ratio: float
    largest_gap_seconds: float
    brackets_start: bool
    brackets_end: bool
    has_volume: bool

    @property
    def is_sufficient(self) -> bool:
        return self.observations >= MIN_INTERVAL_OBSERVATIONS and self.span_ratio >= MIN_SPAN_RATIO

    def to_dict(self) -> dict:
        return {
            "observations": self.observations,
            "window_seconds": self.window_seconds,
            "span_ratio": self.span_ratio,
            "largest_gap_seconds": self.largest_gap_seconds,
            "brackets_start": self.brackets_start,
            "brackets_end": self.brackets_end,
            "has_interval_volume": self.has_volume,
            "is_sufficient": self.is_sufficient,
            "minimum_observations": MIN_INTERVAL_OBSERVATIONS,
            "minimum_span_ratio": MIN_SPAN_RATIO,
            "policy": (
                "An interval benchmark is computed only when the observations "
                "both number enough and span enough of the window. Below either "
                "threshold the benchmark is reported as unavailable with a "
                "reason, because a mean of four ticks is not an interval price."
            ),
        }


@dataclass(frozen=True, slots=True)
class MarketWindow:
    """Everything the platform holds about one instrument over one interval."""

    instrument_id: uuid.UUID
    start: datetime
    end: datetime
    observations: tuple[MarketObservation, ...]
    source: str
    staleness_tolerance_seconds: float = DEFAULT_STALENESS_TOLERANCE_SECONDS

    @property
    def window_seconds(self) -> float:
        return max((self.end - self.start).total_seconds(), 0.0)

    @property
    def inside(self) -> tuple[MarketObservation, ...]:
        return tuple(item for item in self.observations if self.start <= item.timestamp <= self.end)

    @property
    def coverage(self) -> DataCoverage:
        inside = self.inside
        window = self.window_seconds
        if not inside:
            return DataCoverage(0, window, 0.0, window, False, False, False)

        stamps = [item.timestamp for item in inside]
        span = (max(stamps) - min(stamps)).total_seconds()
        gaps = [
            (later - earlier).total_seconds()
            for earlier, later in zip(stamps, stamps[1:], strict=False)
        ]
        edge_gaps = [
            (min(stamps) - self.start).total_seconds(),
            (self.end - max(stamps)).total_seconds(),
        ]
        return DataCoverage(
            observations=len(inside),
            window_seconds=window,
            span_ratio=(span / window) if window > 0 else 1.0,
            largest_gap_seconds=max([*gaps, *edge_gaps], default=window),
            brackets_start=any(item.timestamp <= self.start for item in self.observations),
            brackets_end=any(item.timestamp >= self.end for item in self.observations),
            has_volume=any(item.volume is not None for item in inside),
        )

    def at(self, moment: datetime) -> tuple[MarketObservation | None, float | None]:
        """The most recent observation at or before ``moment``, and its age."""
        ordered = sorted(self.observations, key=lambda item: item.timestamp)
        stamps = [item.timestamp for item in ordered]
        index = bisect_right(stamps, moment) - 1
        if index < 0:
            return None, None
        observation = ordered[index]
        return observation, (moment - observation.timestamp).total_seconds()

    def to_dict(self, include_observations: bool = False) -> dict:
        payload = {
            "instrument_id": str(self.instrument_id),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "source": self.source,
            "staleness_tolerance_seconds": self.staleness_tolerance_seconds,
            "coverage": self.coverage.to_dict(),
        }
        if include_observations:
            payload["observations"] = [item.to_dict() for item in self.inside]
        return payload


@dataclass(frozen=True, slots=True)
class Benchmark:
    """A reference price, or a stated reason there is not one."""

    kind: BenchmarkKind
    price: Decimal | None
    method: BenchmarkMethod
    window_start: datetime | None
    window_end: datetime | None
    source: str | None
    observations: int
    #: Why there is no price, when there is none. Always populated in that case.
    unavailable_reason: str | None = None
    flags: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.price is not None

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "price": format(self.price, "f") if self.price is not None else None,
            "available": self.available,
            "method": str(self.method),
            "window": {
                "start": self.window_start.isoformat() if self.window_start else None,
                "end": self.window_end.isoformat() if self.window_end else None,
            },
            "source": self.source,
            "observations": self.observations,
            "unavailable_reason": self.unavailable_reason,
            "flags": list(self.flags),
        }


def _unavailable(
    kind: BenchmarkKind,
    reason: str,
    window: MarketWindow | None = None,
    flags: Sequence[str] = (),
    method: BenchmarkMethod = BenchmarkMethod.NOT_AVAILABLE,
) -> Benchmark:
    return Benchmark(
        kind=kind,
        price=None,
        method=method,
        window_start=window.start if window else None,
        window_end=window.end if window else None,
        source=window.source if window else None,
        observations=len(window.inside) if window else 0,
        unavailable_reason=reason,
        flags=tuple(flags),
    )


def arrival_benchmark(
    window: MarketWindow, submit_timestamp: datetime | None, first_fill_price: Decimal
) -> Benchmark:
    """The prevailing mid when the order was submitted.

    With no submit timestamp there is nothing to look up, so the first fill's
    own price stands in — and the result carries `ARRIVAL_PROXY_USED`, because a
    shortfall measured against the first fill is systematically smaller than one
    measured against the price before trading started, and silently substituting
    one for the other is how transaction cost analysis becomes fiction.
    """
    if submit_timestamp is None:
        return Benchmark(
            kind=BenchmarkKind.ARRIVAL,
            price=first_fill_price,
            method=BenchmarkMethod.FIRST_FILL_PROXY,
            window_start=window.start,
            window_end=window.start,
            source="execution log",
            observations=0,
            flags=(BenchmarkFlag.ARRIVAL_PROXY_USED,),
        )

    observation, age = window.at(submit_timestamp)
    if observation is None:
        return Benchmark(
            kind=BenchmarkKind.ARRIVAL,
            price=first_fill_price,
            method=BenchmarkMethod.FIRST_FILL_PROXY,
            window_start=submit_timestamp,
            window_end=submit_timestamp,
            source="execution log",
            observations=0,
            flags=(BenchmarkFlag.ARRIVAL_PROXY_USED, BenchmarkFlag.NO_MARKET_DATA),
        )

    flags: list[str] = []
    if age is not None and age > window.staleness_tolerance_seconds:
        flags.append(BenchmarkFlag.STALE_REFERENCE_QUOTE)
    return Benchmark(
        kind=BenchmarkKind.ARRIVAL,
        price=observation.price,
        method=BenchmarkMethod.QUOTE_MID_AT_TIMESTAMP,
        window_start=observation.timestamp,
        window_end=submit_timestamp,
        source=observation.source,
        observations=1,
        flags=tuple(flags),
    )


def decision_benchmark(
    window: MarketWindow,
    decision_timestamp: datetime | None,
    supplied_price: Decimal | None = None,
) -> Benchmark:
    """The price when the decision was made, which only the caller can know."""
    if supplied_price is not None:
        return Benchmark(
            kind=BenchmarkKind.DECISION,
            price=supplied_price,
            method=BenchmarkMethod.SUPPLIED_BY_CALLER,
            window_start=decision_timestamp,
            window_end=decision_timestamp,
            source="caller",
            observations=0,
        )
    if decision_timestamp is None:
        return _unavailable(
            BenchmarkKind.DECISION,
            "No decision timestamp was supplied. The delay between deciding and "
            "submitting is a real cost, but nothing in the trade log records it, "
            "so it is not estimated here.",
            window,
            (BenchmarkFlag.NO_DECISION_TIMESTAMP,),
        )

    observation, age = window.at(decision_timestamp)
    if observation is None:
        return _unavailable(
            BenchmarkKind.DECISION,
            "No market observation at or before the decision timestamp.",
            window,
            (BenchmarkFlag.NO_MARKET_DATA,),
        )
    flags = (
        (BenchmarkFlag.STALE_REFERENCE_QUOTE,)
        if age is not None and age > window.staleness_tolerance_seconds
        else ()
    )
    return Benchmark(
        kind=BenchmarkKind.DECISION,
        price=observation.price,
        method=BenchmarkMethod.QUOTE_MID_AT_TIMESTAMP,
        window_start=observation.timestamp,
        window_end=decision_timestamp,
        source=observation.source,
        observations=1,
        flags=flags,
    )


def prevailing_mid_benchmark(window: MarketWindow, fills: Sequence[tuple[datetime, Decimal]]):
    """The quantity-weighted prevailing mid across the fills.

    This is the benchmark that answers "what was on screen while I was
    trading?", so it is weighted by the quantity of each fill rather than by
    time: a fill of 500 at a wide moment costs more than a fill of 5.
    """
    priced: list[tuple[Decimal, Decimal]] = []
    stale = False
    for moment, quantity in fills:
        observation, age = window.at(moment)
        if observation is None:
            continue
        if age is not None and age > window.staleness_tolerance_seconds:
            stale = True
        priced.append((observation.price, quantity))

    if not priced:
        return _unavailable(
            BenchmarkKind.PREVAILING_MID,
            "No market observation at or before any fill in this order.",
            window,
            (BenchmarkFlag.NO_MARKET_DATA,),
        )

    total = sum((quantity for _price, quantity in priced), Decimal(0))
    if total == 0:
        return _unavailable(
            BenchmarkKind.PREVAILING_MID, "The fills carry no quantity to weight by.", window
        )
    weighted = sum((price * quantity for price, quantity in priced), Decimal(0)) / total
    return Benchmark(
        kind=BenchmarkKind.PREVAILING_MID,
        price=weighted,
        method=BenchmarkMethod.QUOTE_MID_AT_TIMESTAMP,
        window_start=window.start,
        window_end=window.end,
        source=window.source,
        observations=len(priced),
        flags=(BenchmarkFlag.STALE_REFERENCE_QUOTE,) if stale else (),
    )


def interval_twap(window: MarketWindow) -> Benchmark:
    """Time-weighted average of the observed price over the window.

    Piecewise-constant: each observation holds until the next one arrives, which
    is what the platform actually knows. Interpolating between them would invent
    prices the market never showed.
    """
    coverage = window.coverage
    inside = window.inside
    if not inside:
        return _unavailable(
            BenchmarkKind.INTERVAL_TWAP,
            "No market observations inside the execution window.",
            window,
            (BenchmarkFlag.NO_MARKET_DATA,),
        )
    if not coverage.is_sufficient:
        return _unavailable(
            BenchmarkKind.INTERVAL_TWAP,
            (
                f"Only {coverage.observations} observation(s) covering "
                f"{coverage.span_ratio:.0%} of the window. A time-weighted "
                "average of that is a corner of the interval, not the interval."
            ),
            window,
            (BenchmarkFlag.DATA_COVERAGE_LOW,),
        )

    ordered = sorted(inside, key=lambda item: item.timestamp)
    total_weight = Decimal(0)
    total = Decimal(0)
    for current, following in zip(ordered, [*ordered[1:], None], strict=False):
        until = following.timestamp if following else window.end
        seconds = Decimal(str(max((until - current.timestamp).total_seconds(), 0.0)))
        total += current.price * seconds
        total_weight += seconds

    if total_weight == 0:
        return _unavailable(
            BenchmarkKind.INTERVAL_TWAP,
            "Every observation shares one instant, so there is no interval to weight over.",
            window,
            (BenchmarkFlag.DATA_COVERAGE_LOW,),
        )

    flags = [] if coverage.brackets_start else [BenchmarkFlag.WINDOW_NOT_BRACKETED]
    return Benchmark(
        kind=BenchmarkKind.INTERVAL_TWAP,
        price=total / total_weight,
        method=BenchmarkMethod.TIME_WEIGHTED_STEP,
        window_start=window.start,
        window_end=window.end,
        source=window.source,
        observations=coverage.observations,
        flags=tuple(flags),
    )


def interval_vwap(window: MarketWindow) -> Benchmark:
    """Market volume-weighted average price over the window.

    Requires **interval** volume — how much traded between one observation and
    the next. A cumulative session volume carried on a snapshot is not that, and
    treating it as though it were would weight the whole day onto one moment. So
    when the source carries no interval volume, this benchmark is unavailable
    and says why, rather than quietly degrading into a time-weighted average
    under a volume-weighted name.
    """
    coverage = window.coverage
    if not window.inside:
        return _unavailable(
            BenchmarkKind.INTERVAL_VWAP,
            "No market observations inside the execution window.",
            window,
            (BenchmarkFlag.NO_MARKET_DATA,),
        )
    if not coverage.has_volume:
        return _unavailable(
            BenchmarkKind.INTERVAL_VWAP,
            (
                "The market data available for this window carries no interval "
                "volume. A volume-weighted price cannot be computed from prices "
                "alone, and a time-weighted one under a volume-weighted name "
                "would be a different benchmark wearing this one's label."
            ),
            window,
            (BenchmarkFlag.NO_VOLUME_DATA,),
        )
    if not coverage.is_sufficient:
        return _unavailable(
            BenchmarkKind.INTERVAL_VWAP,
            (
                f"Only {coverage.observations} observation(s) covering "
                f"{coverage.span_ratio:.0%} of the window."
            ),
            window,
            (BenchmarkFlag.DATA_COVERAGE_LOW,),
        )

    priced = [item for item in window.inside if item.volume is not None and item.volume > 0]
    total_volume = sum((item.volume for item in priced), Decimal(0))
    if total_volume == 0:
        return _unavailable(
            BenchmarkKind.INTERVAL_VWAP,
            "Every observation in the window reports zero traded volume.",
            window,
            (BenchmarkFlag.NO_VOLUME_DATA,),
        )
    weighted = sum((item.price * item.volume for item in priced), Decimal(0)) / total_volume
    return Benchmark(
        kind=BenchmarkKind.INTERVAL_VWAP,
        price=weighted,
        method=BenchmarkMethod.VOLUME_WEIGHTED,
        window_start=window.start,
        window_end=window.end,
        source=window.source,
        observations=len(priced),
    )


def close_benchmark(window: MarketWindow) -> Benchmark:
    """The last observation the platform holds at or after the window's end."""
    at_or_after = [item for item in window.observations if item.timestamp >= window.end]
    if not at_or_after:
        return _unavailable(
            BenchmarkKind.CLOSE,
            "No market observation at or after the end of the execution window, "
            "so there is no later price to compare against.",
            window,
            (BenchmarkFlag.NO_MARKET_DATA,),
        )
    last = max(at_or_after, key=lambda item: item.timestamp)
    return Benchmark(
        kind=BenchmarkKind.CLOSE,
        price=last.price,
        method=BenchmarkMethod.LAST_OBSERVATION_IN_WINDOW,
        window_start=window.end,
        window_end=last.timestamp,
        source=last.source,
        observations=1,
    )
