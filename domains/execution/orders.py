"""Estimated cost of an order that has not been placed.

The Phase 8 simulator walks a schedule against prices the market *printed*. A
proposed order has no such path: the window it would trade in has not happened.
So this module holds the reference price flat at the snapshot mid for the whole
horizon and reports only the price movement the order is modelled to cause.

That is the assumption, and it is the one that matters. A real path moves for
reasons that have nothing to do with the order — and none of that movement is in
any number here. Stating it is not a disclaimer; it is the difference between a
cost estimate and a forecast, and this module produces the first.

Everything else is deliberately the *same* convention as
:mod:`domains.execution.simulation`, slice by slice:

* permanent impact accumulates and moves the reference price for every later
  slice, in the direction the order is going;
* temporary impact and half the quoted spread are paid on the slice and do not
  persist;
* the cost is measured against the arrival reference — the snapshot mid — and
  is signed so that paying more on a buy and receiving less on a sell are both
  positive costs.

``tests/unit/test_order_cost.py`` asserts the two agree on a flat path, so the
convention has one definition and two entry points rather than two definitions.

Nothing here ranks the strategies or names one. They are reported side by side
under one stated impact model with their assumptions attached, which is a
comparison, not a recommendation. And the model does not contain the result a
reader might expect it to: the permanent term is evaluated on each slice and
accumulates, so splitting an order into ``n`` pieces *raises* the accumulated
permanent impact roughly as ``sqrt(n)`` while lowering the temporary term, and
which of the two wins depends entirely on coefficients this platform has not
calibrated. Nothing in this module says that working an order is cheaper than
taking it, and ``tests/unit/test_order_cost.py`` pins both directions so the
claim cannot appear by accident.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domains.execution.impact import ImpactError, ImpactEstimate, MarketImpactModel
from domains.execution.models import OrderType, Side
from domains.execution.simulation import COUNTERFACTUAL, PRICE_PRECISION
from domains.execution.strategies import (
    MarketContext,
    Schedule,
    ScheduleError,
    ScheduleSlice,
    StrategyUnavailable,
    build_strategy,
    uniform_intervals,
)

ORDER_COST_MODEL_VERSION = "order-cost-estimate@1.0.0"

#: The schedule that takes the whole order now. Not a member of ``STRATEGIES``:
#: it schedules nothing, and it exists here as the point of comparison every
#: other schedule is measured against. Versioned like the real strategies, so
#: every row in the output is named the same way.
IMMEDIATE = "IMMEDIATE"
IMMEDIATE_IDENTIFIER = f"{IMMEDIATE}@1.0.0"

FLAT_REFERENCE_CAVEAT = (
    "A forward estimate, not a simulation. The order has not been placed and "
    "the window it would trade in has not happened, so the reference price is "
    "held flat at the snapshot mid for the whole horizon and the only price "
    "movement in these numbers is the movement this order is modelled to cause. "
    "A real path moves for other reasons as well, and none of that is here."
)


class OrderCostWarning(StrEnum):
    COUNTERFACTUAL = COUNTERFACTUAL
    FLAT_REFERENCE = "ORDER_COST_REFERENCE_HELD_FLAT"
    NO_SPREAD = "ORDER_COST_NO_OBSERVED_SPREAD"
    NO_ADV = "ORDER_COST_NO_AVERAGE_DAILY_VOLUME"
    NO_VOLATILITY = "ORDER_COST_NO_VOLATILITY"
    IMPACT_NOT_CALIBRATED = "ORDER_COST_IMPACT_NOT_CALIBRATED"
    STRATEGY_UNAVAILABLE = "ORDER_COST_STRATEGY_UNAVAILABLE"
    PASSIVE_FILL_NOT_MODELLED = "ORDER_COST_PASSIVE_FILL_NOT_MODELLED"
    LARGER_THAN_A_DAY = "ORDER_COST_ORDER_EXCEEDS_DAILY_VOLUME"


class Marketability(StrEnum):
    """Whether the order, as priced, would cross the displayed market."""

    #: A market order, or a limit at or through the far touch.
    MARKETABLE = "MARKETABLE"
    #: A limit that rests inside or at the near touch. Whether it fills at all
    #: is not modelled here.
    PASSIVE = "PASSIVE"
    #: No two-sided quote to compare the limit against.
    UNKNOWN = "UNKNOWN"


class OrderCostError(ValueError):
    """The estimate cannot be set up from what was supplied."""


@dataclass(frozen=True, slots=True)
class OrderCostRequest:
    """One proposed order, and every input the estimate is computed from.

    None of the volume or volatility inputs come from the platform: it holds no
    intraday profile and no average daily volume of its own, and an estimate
    that needs one and does not get it reports itself unavailable rather than
    inventing a number the reader would have no way to challenge.
    """

    instrument_id: uuid.UUID
    side: Side
    quantity: Decimal
    multiplier: Decimal
    currency: str
    #: The observed mid at the snapshot. Never a model value.
    reference_price: Decimal
    as_of: datetime

    bid: Decimal | None = None
    ask: Decimal | None = None
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None

    #: The venue's minimum tradable increment, in the same units as
    #: ``quantity``. A schedule whose slices would not be whole lots is refused
    #: by the strategy rather than rounded into one that could not be sent.
    lot_size: Decimal = Decimal(1)
    horizon_seconds: float = 1800.0
    intervals: int = 6
    strategies: tuple[str, ...] = (IMMEDIATE, "TWAP")
    impact_model: str = "SquareRootImpactModel"
    volatility: float = 0.0
    average_daily_volume: float = 0.0
    participation_rate: float = 0.10
    expected_volumes: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise OrderCostError("an order quantity is a magnitude; the side carries the sign")
        if self.reference_price <= 0:
            raise OrderCostError("a non-positive reference price is not a market")
        if self.horizon_seconds <= 0:
            raise OrderCostError("a trading horizon must be positive")
        if self.intervals < 1:
            raise OrderCostError("a horizon needs at least one interval")

    @property
    def quoted_spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.ask < self.bid:
            return None
        return self.ask - self.bid

    def to_provenance(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "side": str(self.side),
            "quantity": format(self.quantity, "f"),
            "order_type": str(self.order_type),
            "limit_price": format(self.limit_price, "f") if self.limit_price else None,
            "as_of": self.as_of.isoformat(),
            "horizon_seconds": self.horizon_seconds,
            "intervals": self.intervals,
            "strategies": list(self.strategies),
            "impact_model": self.impact_model,
            "volatility": self.volatility,
            "average_daily_volume": self.average_daily_volume,
            "participation_rate": self.participation_rate,
            "expected_volumes": (
                list(self.expected_volumes) if self.expected_volumes is not None else None
            ),
            "lot_size": format(self.lot_size, "f"),
            "multiplier": format(self.multiplier, "f"),
        }


@dataclass(frozen=True, slots=True)
class SliceCost:
    """One slice of a schedule, priced against the held-flat reference."""

    index: int
    start: datetime
    quantity: Decimal
    reference_price: Decimal
    drifted_price: Decimal
    fill_price: Decimal
    spread_cost_per_unit: Decimal | None
    temporary_impact_per_unit: Decimal
    permanent_impact_per_unit: Decimal
    participation: float | None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start.isoformat(),
            "quantity": format(self.quantity, "f"),
            "reference_price": format(self.reference_price, "f"),
            "drifted_price": format(self.drifted_price, "f"),
            "fill_price": format(self.fill_price, "f"),
            "spread_cost_per_unit": (
                format(self.spread_cost_per_unit, "f")
                if self.spread_cost_per_unit is not None
                else None
            ),
            "temporary_impact_per_unit": format(self.temporary_impact_per_unit, "f"),
            "permanent_impact_per_unit": format(self.permanent_impact_per_unit, "f"),
            "participation": self.participation,
        }


@dataclass(frozen=True, slots=True)
class StrategyCost:
    """What one schedule is estimated to cost, and under what.

    The two components are reported separately because they are known to
    different standards: the spread half is measured off an observed two-sided
    quote, while the impact half is a model output at a coefficient that is
    almost certainly not this market's. Either can be absent, and the total is
    absent whenever one of them is — a slippage figure that quietly omitted
    impact would read as a complete answer.
    """

    strategy: str
    impact_model: str | None
    slices: tuple[SliceCost, ...]
    schedule: Schedule | None
    average_fill_price: Decimal
    #: Positive is a cost whichever way the order goes.
    estimated_slippage_per_unit: Decimal | None
    estimated_slippage_currency: float | None
    estimated_slippage_basis_points: float | None
    spread_component_currency: float | None
    impact_component_currency: float | None
    peak_participation: float | None
    assumptions: tuple[str, ...] = field(default=())
    warnings: tuple[str, ...] = field(default=())

    def to_dict(self, include_slices: bool = True) -> dict:
        payload = {
            "strategy": self.strategy,
            "impact_model": self.impact_model,
            "slices": len(self.slices),
            "average_fill_price": format(self.average_fill_price, "f"),
            "estimated_slippage_per_unit": (
                format(self.estimated_slippage_per_unit, "f")
                if self.estimated_slippage_per_unit is not None
                else None
            ),
            "estimated_slippage_currency": self.estimated_slippage_currency,
            "estimated_slippage_basis_points": self.estimated_slippage_basis_points,
            "spread_component_currency": self.spread_component_currency,
            "impact_component_currency": self.impact_component_currency,
            "peak_participation": self.peak_participation,
            "schedule": self.schedule.to_dict(include_slices=False) if self.schedule else None,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }
        if include_slices:
            payload["slice_detail"] = [item.to_dict() for item in self.slices]
        return payload


@dataclass(frozen=True, slots=True)
class OrderCostEstimate:
    """Every schedule that could be priced, and every one that could not.

    There is no ``recommended_strategy`` field and there will not be one: which
    schedule to use is a judgement about a trader's own urgency and risk
    appetite, and this module measures cost under one impact model rather than
    weighing that trade-off on anyone's behalf.
    """

    instrument_id: uuid.UUID
    side: Side
    quantity: Decimal
    currency: str
    as_of: datetime
    reference_price: Decimal
    quoted_spread: Decimal | None
    marketability: Marketability
    marketability_basis: str
    strategies: tuple[StrategyCost, ...]
    unavailable: tuple[tuple[str, str], ...] = field(default=())
    assumptions: tuple[str, ...] = field(default=())
    warnings: tuple[str, ...] = field(default=())
    model_version: str = ORDER_COST_MODEL_VERSION

    def to_dict(self, include_slices: bool = False) -> dict:
        return {
            "model_version": self.model_version,
            "instrument_id": str(self.instrument_id),
            "side": str(self.side),
            "quantity": format(self.quantity, "f"),
            "currency": self.currency,
            "as_of_timestamp": self.as_of.isoformat(),
            "reference_price": format(self.reference_price, "f"),
            "quoted_spread": (
                format(self.quoted_spread, "f") if self.quoted_spread is not None else None
            ),
            "marketability": str(self.marketability),
            "marketability_basis": self.marketability_basis,
            "strategies": [item.to_dict(include_slices=include_slices) for item in self.strategies],
            "unavailable": [
                {"strategy": name, "reason": reason} for name, reason in self.unavailable
            ],
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
            "caveat": FLAT_REFERENCE_CAVEAT,
            "interpretation": (
                "Estimated slippage against the snapshot mid, per schedule, "
                "under one stated impact model. The schedules are listed side "
                "by side and are not ranked: the choice between them is a "
                "judgement about urgency against price risk, and the price risk "
                "of waiting is not measured here."
            ),
        }


def estimate_order_cost(
    request: OrderCostRequest, impact_model: MarketImpactModel
) -> OrderCostEstimate:
    """Price every requested schedule against a reference held flat."""
    marketability, basis = _marketability(request)

    warnings: list[str] = [OrderCostWarning.COUNTERFACTUAL, OrderCostWarning.FLAT_REFERENCE]
    assumptions: list[str] = [FLAT_REFERENCE_CAVEAT]

    spread = request.quoted_spread
    if spread is None:
        warnings.append(OrderCostWarning.NO_SPREAD)
        assumptions.append(
            "No two-sided quote was observed for this contract, so no spread "
            "cost is attributed. That is an absence, not a spread of zero."
        )
    if marketability is Marketability.PASSIVE:
        warnings.append(OrderCostWarning.PASSIVE_FILL_NOT_MODELLED)
        assumptions.append(
            "The limit price rests behind the touch, so this order would not "
            "cross. Every figure below is conditional on it filling in full, "
            "and whether a resting order fills is not modelled here: that needs "
            "the queue at the level, which is a microstructure question."
        )
    else:
        assumptions.append(
            "Every slice is assumed to cross the displayed market and to pay "
            "half the quoted spread."
        )
    if len(request.strategies) > 1:
        assumptions.append(
            "Permanent impact is evaluated per slice and accumulates, so a "
            "schedule with more slices carries more of it while carrying less "
            "temporary impact. Which effect dominates is a property of the "
            "coefficients, which are not calibrated here, so these figures are "
            "not an argument for or against working the order."
        )

    context = _context(request)

    # Two inputs the platform does not hold, and neither has a usable default.
    # Without a day to be large relative to there is no ratio; without a
    # volatility the model has no scale and would return a zero that reads as
    # "this order moves nothing". Both are therefore reported as an absent
    # impact figure rather than as a number, and the total is withheld with it.
    model: MarketImpactModel | None = impact_model
    if request.average_daily_volume <= 0:
        model = None
        warnings.append(OrderCostWarning.NO_ADV)
        assumptions.append(
            "No average daily volume was supplied. The platform holds none, so "
            "market impact has no size to be relative to; it is reported as "
            "absent rather than as zero, and no total slippage is stated."
        )
    elif float(request.quantity) > request.average_daily_volume:
        warnings.append(OrderCostWarning.LARGER_THAN_A_DAY)
    if request.volatility <= 0:
        model = None
        warnings.append(OrderCostWarning.NO_VOLATILITY)
        assumptions.append(
            "No volatility was supplied, so the impact model has no scale. It "
            "would return exactly zero, which is arithmetic rather than a "
            "finding that this order moves nothing, so no impact figure and no "
            "total slippage is stated."
        )

    priced: list[StrategyCost] = []
    unavailable: list[tuple[str, str]] = []

    for name in request.strategies:
        try:
            schedule = _schedule(name, request, context)
        except StrategyUnavailable as exc:
            unavailable.append((name, exc.reason))
            continue
        except ScheduleError as exc:
            unavailable.append((name, str(exc)))
            continue

        try:
            priced.append(_price(schedule, request, context, model, spread))
        except ImpactError as exc:
            unavailable.append((name, str(exc)))

    if unavailable:
        warnings.append(OrderCostWarning.STRATEGY_UNAVAILABLE)
    if any(OrderCostWarning.IMPACT_NOT_CALIBRATED in item.warnings for item in priced):
        warnings.append(OrderCostWarning.IMPACT_NOT_CALIBRATED)

    return OrderCostEstimate(
        instrument_id=request.instrument_id,
        side=request.side,
        quantity=request.quantity,
        currency=request.currency,
        as_of=request.as_of,
        reference_price=request.reference_price,
        quoted_spread=spread,
        marketability=marketability,
        marketability_basis=basis,
        strategies=tuple(priced),
        unavailable=tuple(unavailable),
        assumptions=tuple(dict.fromkeys(assumptions)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


# --------------------------------------------------------------------- pieces
def _marketability(request: OrderCostRequest) -> tuple[Marketability, str]:
    if request.order_type is not OrderType.LIMIT or request.limit_price is None:
        return (
            Marketability.MARKETABLE,
            f"{request.order_type} order: it takes whatever the displayed market shows.",
        )
    far = request.ask if request.side is Side.BUY else request.bid
    if far is None:
        return (
            Marketability.UNKNOWN,
            "No two-sided quote was observed, so whether this limit would cross "
            "the market cannot be decided from the data.",
        )
    crosses = request.limit_price >= far if request.side is Side.BUY else request.limit_price <= far
    if crosses:
        return (
            Marketability.MARKETABLE,
            f"The limit of {request.limit_price} is at or through the "
            f"{'ask' if request.side is Side.BUY else 'bid'} of {far}.",
        )
    return (
        Marketability.PASSIVE,
        f"The limit of {request.limit_price} rests behind the "
        f"{'ask' if request.side is Side.BUY else 'bid'} of {far}.",
    )


def _context(request: OrderCostRequest) -> MarketContext:
    end = request.as_of + timedelta(seconds=request.horizon_seconds)
    return MarketContext(
        intervals=uniform_intervals(
            request.as_of, end, request.intervals, request.expected_volumes
        ),
        reference_price=request.reference_price,
        volatility=request.volatility,
        average_daily_volume=request.average_daily_volume,
        lot_size=request.lot_size,
    )


def _schedule(name: str, request: OrderCostRequest, context: MarketContext) -> Schedule:
    """The requested schedule, or the one-slice immediate take."""
    if name == IMMEDIATE:
        return Schedule(
            strategy=IMMEDIATE_IDENTIFIER,
            side=request.side,
            parent_quantity=request.quantity,
            slices=(
                ScheduleSlice(
                    index=0,
                    start=context.start,
                    end=context.start,
                    quantity=request.quantity,
                    participation=(
                        float(request.quantity) / request.average_daily_volume
                        if request.average_daily_volume > 0
                        else None
                    ),
                ),
            ),
            parameters={"intervals": 1},
            assumptions=(
                "The whole order is taken at once, against the displayed market "
                "at the snapshot. It is the point of comparison for every "
                "schedule that works the order instead, not a schedule itself.",
            ),
        )
    strategy = build_strategy(name, participation_rate=request.participation_rate)
    return strategy.generate_schedule(request.quantity, request.side, context)


def _price(
    schedule: Schedule,
    request: OrderCostRequest,
    context: MarketContext,
    impact_model: MarketImpactModel | None,
    spread: Decimal | None,
) -> StrategyCost:
    """Walk the slices, accumulating permanent impact exactly as the simulator does.

    ``impact_model`` is ``None`` when the model has nothing to work from — an
    order with no average daily volume behind it has no size relative to a day.
    The spread half is still measurable in that case and is reported on its own,
    with the total withheld rather than quietly reported spread-only.
    """
    sign = Decimal(request.side.sign)
    half_spread = None if spread is None else spread / 2

    cumulative_permanent = Decimal(0)
    slices: list[SliceCost] = []
    warnings: list[str] = []
    spread_total = Decimal(0)
    impact_total = Decimal(0)

    for item in schedule.slices:
        estimate: ImpactEstimate | None = None
        if impact_model is not None:
            estimate = impact_model.estimate(
                quantity=float(item.quantity),
                average_daily_volume=context.average_daily_volume,
                volatility=context.volatility,
                reference_price=float(request.reference_price),
                participation=item.participation,
            )
            if not estimate.is_calibrated:
                warnings.append(OrderCostWarning.IMPACT_NOT_CALIBRATED)

        drifted = request.reference_price + cumulative_permanent
        temporary = Decimal(0)
        permanent = Decimal(0)
        if estimate is not None:
            temporary = _quantise(Decimal(str(estimate.temporary)) * drifted)
            permanent = _quantise(Decimal(str(estimate.permanent)) * drifted)
        paid_spread = Decimal(0) if half_spread is None else half_spread

        fill_price = drifted + sign * (temporary + paid_spread)
        cumulative_permanent += sign * permanent

        # Signed so that both halves are costs: the spread is paid on the slice,
        # the impact term is everything the fill moved away from the arrival
        # reference that the spread does not account for.
        spread_total += paid_spread * item.quantity
        impact_total += (
            sign * (fill_price - request.reference_price) - paid_spread
        ) * item.quantity

        slices.append(
            SliceCost(
                index=item.index,
                start=item.start,
                quantity=item.quantity,
                reference_price=request.reference_price,
                drifted_price=drifted,
                fill_price=fill_price,
                spread_cost_per_unit=half_spread,
                temporary_impact_per_unit=temporary,
                permanent_impact_per_unit=permanent,
                participation=item.participation,
            )
        )

    filled = sum((item.quantity for item in slices), Decimal(0))
    notional = sum((item.fill_price * item.quantity for item in slices), Decimal(0))
    average = notional / filled
    scale = filled * request.multiplier

    complete = impact_model is not None and half_spread is not None
    per_unit = sign * (average - request.reference_price) if complete else None

    return StrategyCost(
        strategy=schedule.strategy,
        impact_model=impact_model.identifier if impact_model is not None else None,
        slices=tuple(slices),
        schedule=schedule,
        average_fill_price=average,
        estimated_slippage_per_unit=per_unit,
        estimated_slippage_currency=(float(per_unit * scale) if per_unit is not None else None),
        estimated_slippage_basis_points=(
            float(per_unit / request.reference_price) * 10_000.0 if per_unit is not None else None
        ),
        spread_component_currency=(
            None if half_spread is None else float(spread_total * request.multiplier)
        ),
        impact_component_currency=(
            float(impact_total * request.multiplier) if impact_model is not None else None
        ),
        peak_participation=schedule.peak_participation,
        assumptions=schedule.assumptions,
        warnings=tuple(dict.fromkeys((*schedule.warnings, *warnings))),
    )


def _quantise(value: Decimal) -> Decimal:
    return value.quantize(PRICE_PRECISION)
