"""Execution schedules, and what each strategy refuses to pretend it can do.

Every strategy returns a schedule whose slices sum to the parent quantity
**exactly** — in `Decimal`, with the remainder distributed by largest fractional
part rather than dumped on the last slice. A property test asserts it for
arbitrary weights and lot sizes, because a schedule that quietly loses two
shares to rounding is a schedule that will lose them again in production.

The other rule is the one from Phase 7 carried forward: a strategy that the
supplied market context cannot support raises `StrategyUnavailable` with a
reason rather than degrading into a different strategy under this one's name. A
VWAP schedule built on a flat volume profile is a TWAP, and returning it as a
VWAP would make the comparison between them meaningless.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from domains.execution.models import Side

#: Weights below this are treated as zero. A slice that would take a
#: billionth of the order is not a slice.
NEGLIGIBLE_WEIGHT = 1e-9


class StrategyUnavailable(Exception):
    """The supplied market context cannot support this strategy."""

    def __init__(self, strategy: str, reason: str) -> None:
        super().__init__(reason)
        self.strategy = strategy
        self.reason = reason


class ScheduleError(ValueError):
    pass


class ScheduleWarning(StrEnum):
    EMPTY_SLICES_DROPPED = "SCHEDULE_EMPTY_SLICES_DROPPED"
    LOT_ROUNDING_APPLIED = "SCHEDULE_LOT_ROUNDING_APPLIED"
    SINGLE_SLICE = "SCHEDULE_SINGLE_SLICE"
    HIGH_PARTICIPATION = "SCHEDULE_HIGH_PARTICIPATION_RATE"


@dataclass(frozen=True, slots=True)
class IntervalContext:
    """What the caller knows about one interval of the trading window.

    Every field but the times is optional, and each strategy says which ones it
    needs. The platform does not have an intraday volume profile of its own, so
    these come from the caller — and a strategy that needs one and does not get
    it refuses rather than assuming the day is flat.
    """

    start: datetime
    end: datetime
    expected_volume: float | None = None
    spread: float | None = None
    volatility: float | None = None

    @property
    def seconds(self) -> float:
        return max((self.end - self.start).total_seconds(), 0.0)

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "expected_volume": self.expected_volume,
            "spread": self.spread,
            "volatility": self.volatility,
        }


@dataclass(frozen=True, slots=True)
class MarketContext:
    """The trading window, sliced, plus the day-level quantities impact needs."""

    intervals: tuple[IntervalContext, ...]
    reference_price: Decimal
    volatility: float
    average_daily_volume: float
    lot_size: Decimal = Decimal(1)
    spread: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.intervals:
            raise ScheduleError("a market context needs at least one interval")
        if self.reference_price <= 0:
            raise ScheduleError("a non-positive reference price is not a market")
        if self.lot_size <= 0:
            raise ScheduleError("lot size must be positive")

    @property
    def start(self) -> datetime:
        return self.intervals[0].start

    @property
    def end(self) -> datetime:
        return self.intervals[-1].end

    @property
    def has_volume_profile(self) -> bool:
        return all(item.expected_volume is not None for item in self.intervals)

    @property
    def expected_volume(self) -> float | None:
        if not self.has_volume_profile:
            return None
        return sum(item.expected_volume or 0.0 for item in self.intervals)

    def to_dict(self, include_intervals: bool = False) -> dict:
        payload = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "intervals": len(self.intervals),
            "reference_price": format(self.reference_price, "f"),
            "volatility": self.volatility,
            "average_daily_volume": self.average_daily_volume,
            "lot_size": format(self.lot_size, "f"),
            "spread": format(self.spread, "f") if self.spread is not None else None,
            "has_volume_profile": self.has_volume_profile,
        }
        if include_intervals:
            payload["interval_detail"] = [item.to_dict() for item in self.intervals]
        return payload


@dataclass(frozen=True, slots=True)
class ScheduleSlice:
    index: int
    start: datetime
    end: datetime
    quantity: Decimal
    #: Share of the interval's expected volume this slice would take, when the
    #: caller supplied a volume expectation for it.
    participation: float | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "quantity": format(self.quantity, "f"),
            "participation": self.participation,
        }


@dataclass(frozen=True, slots=True)
class Schedule:
    """A plan, not a prediction, and not advice."""

    strategy: str
    side: Side
    parent_quantity: Decimal
    slices: tuple[ScheduleSlice, ...]
    parameters: dict
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        total = sum((item.quantity for item in self.slices), Decimal(0))
        if total != self.parent_quantity:
            raise ScheduleError(
                f"{self.strategy} produced slices summing to {total}, not the "
                f"parent quantity {self.parent_quantity}. A schedule that loses "
                "quantity to rounding will lose it again in production."
            )
        if any(item.quantity <= 0 for item in self.slices):
            raise ScheduleError("a slice with no quantity in it is not a slice")

    @property
    def start(self) -> datetime:
        return self.slices[0].start

    @property
    def end(self) -> datetime:
        return self.slices[-1].end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def peak_participation(self) -> float | None:
        rates = [item.participation for item in self.slices if item.participation is not None]
        return max(rates) if rates else None

    def to_dict(self, include_slices: bool = True) -> dict:
        payload = {
            "strategy": self.strategy,
            "side": str(self.side),
            "parent_quantity": format(self.parent_quantity, "f"),
            "slices": len(self.slices),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": self.duration.total_seconds(),
            "peak_participation": self.peak_participation,
            "parameters": self.parameters,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }
        if include_slices:
            payload["slice_detail"] = [item.to_dict() for item in self.slices]
        return payload


def allocate(
    total: Decimal, weights: Sequence[float], lot_size: Decimal = Decimal(1)
) -> list[Decimal]:
    """Split ``total`` across ``weights`` in whole lots, summing exactly.

    Cumulative floors give each slice its share rounded down; the lots left over
    go one at a time to the largest fractional parts. That keeps the slices as
    close to their weights as whole lots allow *and* makes the sum exact, which
    dumping the remainder on the last slice would not.
    """
    if total < 0:
        raise ScheduleError("cannot allocate a negative quantity")
    if not weights:
        raise ScheduleError("cannot allocate across no intervals")
    if any(weight < 0 for weight in weights):
        raise ScheduleError("a negative weight is not a share of an order")

    magnitude = sum(weights)
    if magnitude <= 0:
        raise ScheduleError("weights that sum to zero allocate nothing")

    lots = (total / lot_size).to_integral_value(rounding=ROUND_DOWN)
    if lots <= 0:
        raise ScheduleError(
            f"an order of {total} is smaller than one lot of {lot_size}, so it "
            "cannot be split into whole lots"
        )

    exact = [Decimal(str(weight / magnitude)) * lots for weight in weights]
    floors = [value.to_integral_value(rounding=ROUND_DOWN) for value in exact]
    remainder = int(lots - sum(floors))

    order = sorted(range(len(weights)), key=lambda i: exact[i] - floors[i], reverse=True)
    for position in range(remainder):
        floors[order[position % len(order)]] += 1

    allocation = [value * lot_size for value in floors]
    # Any sub-lot dust belongs to the largest slice rather than to nobody.
    dust = total - sum(allocation, Decimal(0))
    if dust != 0:
        biggest = max(range(len(allocation)), key=lambda i: allocation[i])
        allocation[biggest] += dust
    return allocation


class ExecutionStrategy(ABC):
    """A named, versioned way of splitting a parent order across a window."""

    name: str
    version: str

    @abstractmethod
    def generate_schedule(self, quantity: Decimal, side: Side, context: MarketContext) -> Schedule:
        """Split ``quantity`` across the context's intervals."""

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"

    def _build(
        self,
        quantity: Decimal,
        side: Side,
        context: MarketContext,
        weights: list[float],
        parameters: dict,
        assumptions: list[str],
        warnings: list[str] | None = None,
    ) -> Schedule:
        allocation = allocate(quantity, weights, context.lot_size)
        warnings = list(warnings or [])

        slices: list[ScheduleSlice] = []
        for index, (interval, amount) in enumerate(zip(context.intervals, allocation, strict=True)):
            if amount <= 0:
                continue
            participation = None
            if interval.expected_volume:
                participation = float(amount) / interval.expected_volume
            slices.append(
                ScheduleSlice(
                    index=index,
                    start=interval.start,
                    end=interval.end,
                    quantity=amount,
                    participation=participation,
                )
            )

        if len(slices) < len(context.intervals):
            warnings.append(ScheduleWarning.EMPTY_SLICES_DROPPED)
        if len(slices) == 1:
            warnings.append(ScheduleWarning.SINGLE_SLICE)
        if any((item.participation or 0.0) > 0.5 for item in slices):
            warnings.append(ScheduleWarning.HIGH_PARTICIPATION)
            assumptions.append(
                "At least one slice would take more than half of its interval's "
                "expected volume. Impact models fitted to ordinary participation "
                "say nothing reliable about that."
            )

        return Schedule(
            strategy=self.identifier,
            side=side,
            parent_quantity=quantity,
            slices=tuple(slices),
            parameters={**parameters, "lot_size": format(context.lot_size, "f")},
            assumptions=tuple(assumptions),
            warnings=tuple(dict.fromkeys(warnings)),
        )


class TWAPStrategy(ExecutionStrategy):
    """Equal quantity per interval, regardless of what the market is doing.

    Needs nothing from the context but the interval count, which is exactly why
    it is the baseline: any other strategy has to earn its extra inputs by
    beating this.
    """

    name = "TWAP"
    version = "1.0.0"

    def generate_schedule(self, quantity: Decimal, side: Side, context: MarketContext) -> Schedule:
        return self._build(
            quantity,
            side,
            context,
            [1.0] * len(context.intervals),
            {"intervals": len(context.intervals)},
            [
                "Quantity is spread equally across every interval, so the "
                "schedule tracks the clock and not the market.",
                "It assumes nothing about when volume arrives, which is why it "
                "needs no volume forecast and cannot be wrong about one.",
            ],
        )


class VWAPStrategy(ExecutionStrategy):
    """Quantity in proportion to expected volume.

    Requires a volume profile that actually varies. Given a flat one this *is*
    TWAP, and returning it under the VWAP name would make any comparison between
    the two meaningless — so it refuses instead.
    """

    name = "VWAP"
    version = "1.0.0"

    def generate_schedule(self, quantity: Decimal, side: Side, context: MarketContext) -> Schedule:
        if not context.has_volume_profile:
            raise StrategyUnavailable(
                self.identifier,
                "A VWAP schedule tracks an intraday volume profile, and none was "
                "supplied for every interval. This platform does not hold one of "
                "its own, and assuming the day is flat would produce a TWAP under "
                "the VWAP name.",
            )
        volumes = [item.expected_volume or 0.0 for item in context.intervals]
        if max(volumes) - min(volumes) < NEGLIGIBLE_WEIGHT:
            raise StrategyUnavailable(
                self.identifier,
                "The supplied volume profile is flat, so a volume-weighted "
                "schedule is identical to a time-weighted one. Use TWAP, or "
                "supply a profile that varies.",
            )
        return self._build(
            quantity,
            side,
            context,
            volumes,
            {
                "intervals": len(context.intervals),
                "expected_volume": context.expected_volume,
            },
            [
                "Quantity follows the supplied expected-volume profile. The "
                "profile is the caller's forecast, not a measurement by this "
                "platform, and the schedule is only as good as it is.",
                "A forecast that is wrong about when volume arrives produces a "
                "schedule that participates more heavily than intended exactly "
                "when it is least able to.",
            ],
        )


class POVStrategy(ExecutionStrategy):
    """Take a fixed share of expected volume in every interval.

    Refuses when the window cannot absorb the order at that rate. Silently
    truncating would return a schedule that does not fill the parent order while
    still summing to it — which is impossible — and silently raising the rate
    would answer a question nobody asked.
    """

    name = "POV"
    version = "1.0.0"

    def __init__(self, participation_rate: float = 0.10) -> None:
        if not 0.0 < participation_rate <= 1.0:
            raise ScheduleError(
                f"a participation rate of {participation_rate} is not a share of "
                "volume; it must be in (0, 1]"
            )
        self.participation_rate = participation_rate

    def generate_schedule(self, quantity: Decimal, side: Side, context: MarketContext) -> Schedule:
        if not context.has_volume_profile:
            raise StrategyUnavailable(
                self.identifier,
                "Participating in volume requires a volume forecast for every "
                "interval, and none was supplied.",
            )
        capacity = (context.expected_volume or 0.0) * self.participation_rate
        if capacity < float(quantity):
            raise StrategyUnavailable(
                self.identifier,
                f"Participating at {self.participation_rate:.1%} of the expected "
                f"{context.expected_volume:,.0f} units in this window fills at "
                f"most {capacity:,.0f}, short of the {quantity} ordered. Widen "
                "the window or raise the rate; this schedule will not quietly do "
                "either for you.",
            )
        volumes = [item.expected_volume or 0.0 for item in context.intervals]
        return self._build(
            quantity,
            side,
            context,
            volumes,
            {
                "participation_rate": self.participation_rate,
                "expected_volume": context.expected_volume,
                "capacity": capacity,
            },
            [
                f"Quantity is allocated in proportion to expected volume so the "
                f"order finishes inside the window while staying under "
                f"{self.participation_rate:.1%} of it.",
                "Real participation-of-volume tracks realised volume as the day "
                "unfolds and re-plans. This is the ex-ante schedule that forecast "
                "implies, not a simulation of that feedback.",
                "Because the whole order fits inside the window, allocating in "
                "proportion to expected volume is exactly what VWAP does, so the "
                "two schedules coincide here. They diverge in practice only "
                "through the re-planning this one does not simulate.",
            ],
        )


class LiquidityAdaptiveStrategy(ExecutionStrategy):
    """Trade more where liquidity is cheap and less where it is dear.

        weight = expected_volume / (spread ^ a * volatility ^ b)

    Requires a per-interval signal that actually varies. With flat spread and
    flat volatility this is VWAP, and it says so rather than pretending the
    extra inputs did something.
    """

    name = "LiquidityAdaptive"
    version = "1.0.0"

    def __init__(self, spread_exponent: float = 1.0, volatility_exponent: float = 1.0) -> None:
        if spread_exponent < 0 or volatility_exponent < 0:
            raise ScheduleError("exponents must be non-negative")
        self.spread_exponent = spread_exponent
        self.volatility_exponent = volatility_exponent

    def generate_schedule(self, quantity: Decimal, side: Side, context: MarketContext) -> Schedule:
        if not context.has_volume_profile:
            raise StrategyUnavailable(
                self.identifier,
                "A liquidity-adaptive schedule modulates a volume profile, and "
                "none was supplied for every interval.",
            )
        spreads = [item.spread for item in context.intervals]
        vols = [item.volatility for item in context.intervals]
        has_spread = all(value is not None and value > 0 for value in spreads)
        has_vol = all(value is not None and value > 0 for value in vols)
        if not has_spread and not has_vol:
            raise StrategyUnavailable(
                self.identifier,
                "Adapting to liquidity needs a per-interval spread or volatility, "
                "and neither was supplied. Without one this is VWAP, and running "
                "it under this name would credit inputs that did nothing.",
            )

        varies = (has_spread and max(spreads) - min(spreads) > NEGLIGIBLE_WEIGHT) or (
            has_vol and max(vols) - min(vols) > NEGLIGIBLE_WEIGHT
        )
        if not varies:
            raise StrategyUnavailable(
                self.identifier,
                "The supplied spread and volatility are flat across every "
                "interval, so nothing modulates the volume profile and this is "
                "VWAP. Use VWAP, or supply signals that vary.",
            )

        weights = []
        for interval in context.intervals:
            weight = interval.expected_volume or 0.0
            if has_spread and interval.spread:
                weight /= interval.spread**self.spread_exponent
            if has_vol and interval.volatility:
                weight /= interval.volatility**self.volatility_exponent
            weights.append(weight)

        return self._build(
            quantity,
            side,
            context,
            weights,
            {
                "spread_exponent": self.spread_exponent,
                "volatility_exponent": self.volatility_exponent,
                "uses_spread": has_spread,
                "uses_volatility": has_vol,
            },
            [
                "Expected volume divided by spread and volatility, each raised to "
                "a stated exponent. The exponents are parameters of this "
                "schedule, not quantities measured from any market.",
                "It trades more where the platform's inputs say liquidity is "
                "cheap. Whether those inputs are right about the future is not "
                "something this schedule can know.",
            ],
        )


STRATEGIES: dict[str, type[ExecutionStrategy]] = {
    TWAPStrategy.name: TWAPStrategy,
    VWAPStrategy.name: VWAPStrategy,
    POVStrategy.name: POVStrategy,
    LiquidityAdaptiveStrategy.name: LiquidityAdaptiveStrategy,
}


def build_strategy(name: str, **kwargs) -> ExecutionStrategy:
    try:
        factory = STRATEGIES[name]
    except KeyError:
        raise ScheduleError(
            f"unknown execution strategy {name!r}; available: {', '.join(sorted(STRATEGIES))}"
        ) from None
    if factory is POVStrategy:
        return factory(participation_rate=kwargs.get("participation_rate", 0.10))
    if factory is LiquidityAdaptiveStrategy:
        return factory(
            spread_exponent=kwargs.get("spread_exponent", 1.0),
            volatility_exponent=kwargs.get("volatility_exponent", 1.0),
        )
    return factory()


def uniform_intervals(
    start: datetime, end: datetime, count: int, volumes: Sequence[float] | None = None
) -> tuple[IntervalContext, ...]:
    """Split a window into equal intervals, optionally with a volume forecast."""
    if count < 1:
        raise ScheduleError("a window needs at least one interval")
    if end <= start:
        raise ScheduleError("a trading window must end after it starts")
    if volumes is not None and len(volumes) != count:
        raise ScheduleError(f"{len(volumes)} volume values for {count} intervals")

    span = (end - start) / count
    return tuple(
        IntervalContext(
            start=start + span * index,
            end=start + span * (index + 1),
            expected_volume=None if volumes is None else float(volumes[index]),
        )
        for index in range(count)
    )
