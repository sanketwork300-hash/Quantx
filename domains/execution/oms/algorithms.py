"""Working a parent order as child orders.

Build spec §26 asks that the existing execution algorithms be connected to live
and paper trading. They are: this module reuses
:mod:`domains.execution.strategies` — the same TWAP, VWAP, POV and
liquidity-adaptive schedulers the Phase 7 and Phase 8 analysis surfaces run —
rather than growing a second implementation that would agree with the first
until it did not.

What is added here is only the part the schedulers never needed: turning a
schedule into orders that are actually placed, one slice at a time, as each
slice's window arrives.

## What this is not

It is not "optimal execution", and the platform does not use that phrase. A
schedule is a *plan for splitting a quantity the caller already decided to
trade*. It does not say whether trading is a good idea, it does not choose the
size, and it is not a claim that this split beats another one — that claim needs
a counterfactual, which is what the Phase 8 simulator is for and what it calls
itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from domains.execution.models import Side
from domains.execution.oms.models import (
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)
from domains.execution.strategies import (
    MarketContext,
    Schedule,
    ScheduleError,
    StrategyUnavailable,
    build_strategy,
    uniform_intervals,
)


class AlgorithmError(ValueError):
    """The parent order cannot be turned into a workable schedule."""


@dataclass(frozen=True, slots=True)
class ParentOrderPlan:
    """A parent order, its schedule, and what the schedule assumed.

    The assumptions and warnings from the scheduler travel with the plan rather
    than being consumed at generation time, because they are the difference
    between "this will take 8% of volume" and "this will take 8% of a volume
    figure nobody supplied".
    """

    parent_client_order_id: str
    instrument_id: uuid.UUID
    side: OrderSide
    total_quantity: Decimal
    schedule: Schedule
    order_type: OrderType
    limit_price: Decimal | None
    strategy_tag: str | None
    #: True when the schedule was built with no volume expectation, so
    #: participation is unknown for every slice rather than low.
    volume_profile_supplied: bool

    def to_dict(self) -> dict:
        return {
            "parent_client_order_id": self.parent_client_order_id,
            "instrument_id": str(self.instrument_id),
            "side": str(self.side),
            "total_quantity": format(self.total_quantity, "f"),
            "order_type": str(self.order_type),
            "limit_price": (
                format(self.limit_price, "f") if self.limit_price is not None else None
            ),
            "volume_profile_supplied": self.volume_profile_supplied,
            "schedule": self.schedule.to_dict(),
            "child_orders": [
                {
                    "index": item.index,
                    "start": item.start.isoformat(),
                    "end": item.end.isoformat(),
                    "quantity": format(item.quantity, "f"),
                    "participation": item.participation,
                    "client_order_id": self.child_client_order_id(item.index),
                }
                for item in self.schedule.slices
            ],
        }

    def child_client_order_id(self, index: int) -> str:
        """Derived from the parent's, so a retry is idempotent per slice.

        A child order id that was random would place a second slice on every
        retry of the same window. Deriving it means the OMS's existing
        idempotency check does the work, with nothing new to get wrong.
        """
        return f"{self.parent_client_order_id}-{index:03d}"

    def due_slices(self, as_of: datetime) -> tuple:
        """Slices whose window has started and not yet ended.

        A slice whose window has passed is **not** returned. Placing it late
        would put the whole of a missed interval into the market at once, which
        is the opposite of what the schedule was for, and the caller should see
        the gap rather than have it quietly filled.
        """
        return tuple(item for item in self.schedule.slices if item.start <= as_of < item.end)

    def missed_slices(self, as_of: datetime, placed: set[int]) -> tuple:
        return tuple(
            item for item in self.schedule.slices if item.end <= as_of and item.index not in placed
        )

    def child_request(self, index: int) -> OrderRequest:
        slice_ = next(item for item in self.schedule.slices if item.index == index)
        return OrderRequest(
            instrument_id=self.instrument_id,
            side=self.side,
            quantity=slice_.quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            # Immediate-or-cancel by default: a child order that rested past its
            # own interval would still be working when the next slice arrived,
            # and the schedule would be delivering more than it planned.
            time_in_force=TimeInForce.IMMEDIATE_OR_CANCEL,
            client_order_id=self.child_client_order_id(index),
            strategy_tag=self.strategy_tag,
            metadata={
                "parent_client_order_id": self.parent_client_order_id,
                "slice_index": index,
                "slice_start": slice_.start.isoformat(),
                "slice_end": slice_.end.isoformat(),
                "schedule_strategy": self.schedule.strategy,
            },
        )


@dataclass(frozen=True, slots=True)
class WorkingWindow:
    """The window a parent order is to be worked over, as the caller states it.

    ``expected_volumes`` is optional and is not filled in from anywhere. A
    volume forecast the platform invented would make POV and VWAP produce
    confident-looking schedules built on a number nobody supplied.
    """

    start: datetime
    end: datetime
    slices: int
    reference_price: Decimal
    volatility: float = 0.0
    average_daily_volume: float = 0.0
    lot_size: Decimal = Decimal(1)
    spread: Decimal | None = None
    expected_volumes: tuple[float, ...] | None = None


def plan_parent_order(
    *,
    parent_client_order_id: str,
    instrument_id: uuid.UUID,
    side: OrderSide,
    quantity: Decimal,
    strategy: str,
    window: WorkingWindow,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
    strategy_tag: str | None = None,
    parameters: dict | None = None,
) -> ParentOrderPlan:
    """Split a parent order across a window using a named strategy.

    Raises rather than degrading: a VWAP asked for with no volume profile, or a
    POV with no average daily volume, is refused by the underlying scheduler.
    Falling back to TWAP would answer a different question than the one asked
    and label the answer with the name of the question.
    """
    if quantity <= 0:
        raise AlgorithmError("a parent order needs a positive quantity")
    if window.slices < 1:
        raise AlgorithmError("a working window needs at least one slice")
    if window.expected_volumes is not None and len(window.expected_volumes) != window.slices:
        raise AlgorithmError(
            f"{len(window.expected_volumes)} volume values were supplied for "
            f"{window.slices} slices; a profile that does not line up with the "
            "window would silently weight the wrong intervals"
        )

    try:
        intervals = uniform_intervals(
            window.start, window.end, window.slices, window.expected_volumes
        )
        context = MarketContext(
            intervals=intervals,
            reference_price=window.reference_price,
            volatility=window.volatility,
            average_daily_volume=window.average_daily_volume,
            lot_size=window.lot_size,
            spread=window.spread,
        )
        schedule = build_strategy(strategy, **(parameters or {})).generate_schedule(
            quantity, _side(side), context
        )
    except (ScheduleError, StrategyUnavailable) as exc:
        # StrategyUnavailable is not a ScheduleError, and catching only the
        # latter would let a VWAP asked for without a volume profile escape as
        # an unhandled error instead of the refusal it is.
        raise AlgorithmError(str(exc)) from exc

    return ParentOrderPlan(
        parent_client_order_id=parent_client_order_id,
        instrument_id=instrument_id,
        side=side,
        total_quantity=quantity,
        schedule=schedule,
        order_type=order_type,
        limit_price=limit_price,
        strategy_tag=strategy_tag,
        volume_profile_supplied=window.expected_volumes is not None,
    )


def _side(side: OrderSide) -> Side:
    """The execution domain's own side enum, which the schedulers take."""
    return Side.BUY if side is OrderSide.BUY else Side.SELL


__all__ = [
    "AlgorithmError",
    "ParentOrderPlan",
    "WorkingWindow",
    "plan_parent_order",
]
