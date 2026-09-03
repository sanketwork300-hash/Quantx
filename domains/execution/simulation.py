"""The counterfactual simulator, and the thing it can never account for.

A simulated schedule is walked against prices the market actually printed, and
the impact model is asked what each slice would have cost to push through. What
neither can capture is that **executing the simulated schedule would itself have
changed the prices it was simulated against**: the observed path already
contains whatever the real order did to the market, and it does not contain what
this hypothetical one would have done.

That is not a footnote. Every result here is labelled `COUNTERFACTUAL_ESTIMATE`,
carries the caveat in its own payload, and a test asserts the label is present on
every path through this module.

The simulated fills are then measured by exactly the Phase 7 machinery that
measures real ones — same benchmarks, same shortfall, same decomposition — so a
counterfactual and the execution it is compared against are never scored by two
different rulers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domains.execution.benchmarks import (
    BenchmarkKind,
    MarketWindow,
    arrival_benchmark,
    close_benchmark,
    interval_twap,
    prevailing_mid_benchmark,
)
from domains.execution.impact import ImpactEstimate, MarketImpactModel
from domains.execution.models import (
    Execution,
    ExecutionSource,
    GroupingMethod,
    OrderType,
    ParentOrder,
    Side,
)
from domains.execution.strategies import MarketContext, Schedule
from domains.execution.tca import ExecutionAnalysis, analyse

SIMULATION_MODEL_VERSION = "execution-simulation@1.0.0"

#: The label that must appear on every simulated result.
COUNTERFACTUAL = "COUNTERFACTUAL_ESTIMATE"

#: Derived prices are kept to this precision. Enough for any instrument, and
#: fine enough that a small impact never rounds to nothing.
PRICE_PRECISION = Decimal("0.00000001")

COUNTERFACTUAL_CAVEAT = (
    "A counterfactual estimate. This schedule was never executed: it is priced "
    "against a path the market printed while something else was happening, and "
    "executing it would itself have moved that path in ways no simulation here "
    "can capture. Treat the numbers as a comparison between schedules under one "
    "stated impact model, not as what would have happened."
)


class SimulationWarning(StrEnum):
    COUNTERFACTUAL = COUNTERFACTUAL
    INCOMPLETE = "SIMULATION_INCOMPLETE_SCHEDULE"
    NO_PRICE_FOR_SLICE = "SIMULATION_NO_PRICE_FOR_SLICE"
    IMPACT_NOT_CALIBRATED = "SIMULATION_IMPACT_NOT_CALIBRATED"
    NO_SPREAD_DATA = "SIMULATION_NO_SPREAD_DATA"
    STALE_PRICES = "SIMULATION_STALE_PRICES"


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One slice, priced. Never stored as an `Execution` in the trade log."""

    index: int
    timestamp: datetime
    quantity: Decimal
    #: The observed mid at this moment, before anything hypothetical.
    observed_price: Decimal
    #: The mid after the permanent impact accumulated by earlier slices.
    drifted_price: Decimal
    fill_price: Decimal
    spread_cost_per_unit: Decimal
    temporary_impact_per_unit: Decimal
    permanent_impact_per_unit: Decimal
    participation: float | None
    price_age_seconds: float | None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "quantity": format(self.quantity, "f"),
            "observed_price": format(self.observed_price, "f"),
            "drifted_price": format(self.drifted_price, "f"),
            "fill_price": format(self.fill_price, "f"),
            "spread_cost_per_unit": format(self.spread_cost_per_unit, "f"),
            "temporary_impact_per_unit": format(self.temporary_impact_per_unit, "f"),
            "permanent_impact_per_unit": format(self.permanent_impact_per_unit, "f"),
            "participation": self.participation,
            "price_age_seconds": self.price_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class UnfilledSlice:
    index: int
    timestamp: datetime
    quantity: Decimal
    reason: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "quantity": format(self.quantity, "f"),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """What a schedule would have paid, under stated models, on a past path."""

    strategy: str
    impact_model: str
    side: Side
    ordered_quantity: Decimal
    filled_quantity: Decimal
    fills: tuple[SimulatedFill, ...]
    unfilled: tuple[UnfilledSlice, ...]
    schedule: Schedule
    context: MarketContext
    #: The simulated fills run through the Phase 7 analysis, so a counterfactual
    #: is benchmarked by the same code that benchmarks a real execution.
    analysis: ExecutionAnalysis | None
    latency_seconds: float
    max_price_age_seconds: float
    warnings: tuple[str, ...] = ()

    @property
    def is_counterfactual(self) -> bool:
        """Always true. The property exists so the claim is testable."""
        return COUNTERFACTUAL in self.warnings

    @property
    def completion_rate(self) -> float:
        if self.ordered_quantity == 0:
            return 0.0
        return float(self.filled_quantity / self.ordered_quantity)

    @property
    def average_price(self) -> Decimal | None:
        if not self.fills or self.filled_quantity == 0:
            return None
        return (
            sum((item.fill_price * item.quantity for item in self.fills), Decimal(0))
            / self.filled_quantity
        )

    @property
    def time_to_completion(self) -> timedelta | None:
        if not self.fills:
            return None
        return self.fills[-1].timestamp - self.schedule.start

    @property
    def modelled_impact_cost(self) -> Decimal:
        """What the impact model added, separated from the observed path."""
        return sum(
            (
                (item.temporary_impact_per_unit + item.permanent_impact_per_unit) * item.quantity
                for item in self.fills
            ),
            Decimal(0),
        )

    @property
    def modelled_spread_cost(self) -> Decimal:
        return sum((item.spread_cost_per_unit * item.quantity for item in self.fills), Decimal(0))

    def to_dict(self, include_fills: bool = True) -> dict:
        average = self.average_price
        shortfall = None
        if self.analysis is not None and self.analysis.primary_shortfall is not None:
            shortfall = self.analysis.primary_shortfall.to_dict()

        payload = {
            "counterfactual": True,
            "caveat": COUNTERFACTUAL_CAVEAT,
            "strategy": self.strategy,
            "impact_model": self.impact_model,
            "side": str(self.side),
            "ordered_quantity": format(self.ordered_quantity, "f"),
            "filled_quantity": format(self.filled_quantity, "f"),
            "completion_rate": self.completion_rate,
            "average_price": format(average, "f") if average is not None else None,
            "time_to_completion_seconds": (
                self.time_to_completion.total_seconds()
                if self.time_to_completion is not None
                else None
            ),
            "modelled_impact_cost": format(self.modelled_impact_cost, "f"),
            "modelled_spread_cost": format(self.modelled_spread_cost, "f"),
            "latency_seconds": self.latency_seconds,
            "max_price_age_seconds": self.max_price_age_seconds,
            "shortfall": shortfall,
            "benchmarks": (
                [item.to_dict() for item in self.analysis.benchmarks] if self.analysis else []
            ),
            "schedule": self.schedule.to_dict(include_slices=False),
            "context": self.context.to_dict(),
            "unfilled": [item.to_dict() for item in self.unfilled],
            "warnings": list(self.warnings),
        }
        if include_fills:
            payload["fills"] = [item.to_dict() for item in self.fills]
        return payload


def simulate(
    schedule: Schedule,
    window: MarketWindow,
    context: MarketContext,
    impact_model: MarketImpactModel,
    instrument_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    latency_seconds: float = 0.0,
    multiplier: Decimal = Decimal(1),
    currency: str = "INR",
    max_price_age_seconds: float | None = None,
) -> SimulationResult:
    """Walk a schedule against observed prices under a stated impact model.

    Permanent impact accumulates: each slice moves the reference price for every
    later one, in the direction the order is going. Temporary impact and half the
    quoted spread are paid on the slice and do not persist.

    A slice whose nearest observation is older than ``max_price_age_seconds`` is
    left **unfilled**, and the completion rate says so. That is the opposite of
    what a portfolio valuation does with a stale quote, deliberately: a stale
    mark is still the best observation of a position that exists, whereas
    filling a hypothetical slice against a price from hours ago asserts
    liquidity nobody saw. The tolerance is a declared parameter, so a caller who
    wants the generous reading can ask for it.
    """
    if latency_seconds < 0:
        raise ValueError("latency cannot be negative")
    tolerance = (
        window.staleness_tolerance_seconds
        if max_price_age_seconds is None
        else max_price_age_seconds
    )

    instrument_id = instrument_id or window.instrument_id
    user_id = user_id or uuid.UUID(int=0)
    sign = Decimal(schedule.side.sign)

    warnings: list[str] = [SimulationWarning.COUNTERFACTUAL]
    fills: list[SimulatedFill] = []
    unfilled: list[UnfilledSlice] = []
    cumulative_permanent = Decimal(0)
    stale = False
    missing_spread = False

    for slice_ in schedule.slices:
        moment = slice_.start + timedelta(seconds=latency_seconds)
        observation, age = window.at(moment)
        if observation is None:
            unfilled.append(
                UnfilledSlice(
                    index=slice_.index,
                    timestamp=moment,
                    quantity=slice_.quantity,
                    reason=(
                        "The market window holds no observation at or before this "
                        "slice, so there is no price to fill it at."
                    ),
                )
            )
            continue
        if age is not None and age > tolerance:
            unfilled.append(
                UnfilledSlice(
                    index=slice_.index,
                    timestamp=moment,
                    quantity=slice_.quantity,
                    reason=(
                        f"The nearest observation is {age:,.0f}s old, beyond the "
                        f"{tolerance:,.0f}s tolerance. Filling a hypothetical "
                        "slice against a price that stale would assert liquidity "
                        "nobody observed."
                    ),
                )
            )
            stale = True
            continue

        estimate: ImpactEstimate = impact_model.estimate(
            quantity=float(slice_.quantity),
            average_daily_volume=context.average_daily_volume,
            volatility=context.volatility,
            reference_price=float(observation.price),
            participation=slice_.participation,
        )
        if not estimate.is_calibrated:
            warnings.append(SimulationWarning.IMPACT_NOT_CALIBRATED)

        drifted = observation.price + cumulative_permanent
        temporary = _quantise(Decimal(str(estimate.temporary)) * drifted)
        permanent = _quantise(Decimal(str(estimate.permanent)) * drifted)

        spread = observation.spread
        if spread is None:
            missing_spread = True
            half_spread = Decimal(0)
        else:
            half_spread = spread / 2

        fill_price = drifted + sign * (temporary + half_spread)
        cumulative_permanent += sign * permanent

        fills.append(
            SimulatedFill(
                index=slice_.index,
                timestamp=moment,
                quantity=slice_.quantity,
                observed_price=observation.price,
                drifted_price=drifted,
                fill_price=fill_price,
                spread_cost_per_unit=half_spread,
                temporary_impact_per_unit=temporary,
                permanent_impact_per_unit=permanent,
                participation=slice_.participation,
                price_age_seconds=age,
            )
        )

    if unfilled:
        warnings.append(SimulationWarning.INCOMPLETE)
        warnings.append(SimulationWarning.NO_PRICE_FOR_SLICE)
    if stale:
        warnings.append(SimulationWarning.STALE_PRICES)
    if missing_spread:
        warnings.append(SimulationWarning.NO_SPREAD_DATA)

    filled = sum((item.quantity for item in fills), Decimal(0))
    analysis = (
        _analyse(fills, schedule, window, instrument_id, user_id, multiplier, currency)
        if fills
        else None
    )

    return SimulationResult(
        strategy=schedule.strategy,
        impact_model=impact_model.identifier,
        side=schedule.side,
        ordered_quantity=schedule.parent_quantity,
        filled_quantity=filled,
        fills=tuple(fills),
        unfilled=tuple(unfilled),
        schedule=schedule,
        context=context,
        analysis=analysis,
        latency_seconds=latency_seconds,
        max_price_age_seconds=tolerance,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _analyse(
    fills: list[SimulatedFill],
    schedule: Schedule,
    window: MarketWindow,
    instrument_id: uuid.UUID,
    user_id: uuid.UUID,
    multiplier: Decimal,
    currency: str,
) -> ExecutionAnalysis:
    """Score the simulated fills with the machinery that scores real ones."""
    executions = tuple(
        Execution(
            id=uuid.uuid4(),
            user_id=user_id,
            instrument_id=instrument_id,
            side=schedule.side,
            quantity=item.quantity,
            execution_price=item.fill_price,
            exchange_timestamp=item.timestamp,
            submit_timestamp=schedule.start,
            order_quantity=schedule.parent_quantity,
            parent_order_key=f"simulated:{schedule.strategy}",
            order_type=OrderType.UNKNOWN,
            source=ExecutionSource.MANUAL,
            metadata={"simulated": True},
        )
        for item in fills
    )
    parent = ParentOrder(
        key=f"simulated:{schedule.strategy}",
        instrument_id=instrument_id,
        side=schedule.side,
        executions=executions,
        grouping_method=GroupingMethod.EXPLICIT,
        multiplier=multiplier,
        currency=currency,
    )
    benchmarks = [
        arrival_benchmark(window, schedule.start, executions[0].execution_price),
        prevailing_mid_benchmark(
            window, [(item.exchange_timestamp, item.quantity) for item in executions]
        ),
        interval_twap(window),
        close_benchmark(window),
    ]
    return analyse(parent, window, benchmarks, primary=BenchmarkKind.ARRIVAL)


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    """Several schedules on one path, side by side. Not a ranking, not advice."""

    results: tuple[SimulationResult, ...]
    unavailable: tuple[tuple[str, str], ...]
    window_start: datetime
    window_end: datetime

    def to_dict(self, include_fills: bool = False) -> dict:
        return {
            "counterfactual": True,
            "caveat": COUNTERFACTUAL_CAVEAT,
            "comparison_caveat": (
                "These are estimates of what different schedules would have paid "
                "on one past path under one impact model. They are not a ranking, "
                "no strategy is recommended, and the differences between them are "
                "smaller than the uncertainty in an uncalibrated impact "
                "coefficient unless you supplied one."
            ),
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "strategies": [item.to_dict(include_fills=include_fills) for item in self.results],
            "unavailable": [
                {"strategy": name, "reason": reason} for name, reason in self.unavailable
            ],
        }


def compare(
    schedules: dict[str, Schedule],
    unavailable: dict[str, str],
    window: MarketWindow,
    context: MarketContext,
    impact_model: MarketImpactModel,
    **kwargs,
) -> StrategyComparison:
    """Simulate several schedules against the same path and the same model."""
    results = tuple(
        simulate(schedule, window, context, impact_model, **kwargs)
        for schedule in schedules.values()
    )
    return StrategyComparison(
        results=results,
        unavailable=tuple(sorted(unavailable.items())),
        window_start=window.start,
        window_end=window.end,
    )


def _quantise(value: Decimal) -> Decimal:
    """Trim a float-derived Decimal to a sane price precision.

    Eight places, not the observed price's own exponent: quantising to the
    reference would round a small impact on a whole-number price straight to
    zero, which is the one error this function must not make.
    """
    return value.quantize(PRICE_PRECISION)
