"""Queue outlook for a hypothetical resting order, behind the gate.

The domain work is entirely in assembling the inputs honestly: which snapshot's
book the order is joining, how much is displayed at its level, and how much
size left that level over the observation window. Everything after that is
:func:`quant.microstructure.queue.estimate_queue_outlook`, which returns a
bracket rather than a number.

Two decisions worth stating. **The queue ahead defaults to the level's entire
displayed size**, because an order joining now is behind all of it — assuming
anything smaller would be assuming priority the venue never granted. And
**events are matched to the level by exact price**, not by proximity: a
cancellation one tick away drained a different queue, and rounding it into this
one would invent departures.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from domains.market_data.enums import BookEventType, BookSide
from domains.market_data.models import BookEvent, OrderBookSnapshot
from quant.microstructure.queue import (
    QueueOutlook,
    QueueRefusal,
    QueueUnavailable,
    estimate_queue_outlook,
)

QUEUE_MODEL_VERSION = "microstructure-queue@1.0.0"


@dataclass(frozen=True, slots=True)
class QueueParams:
    side: BookSide
    horizon_seconds: float
    #: The level to rest at. ``None`` means the best price on that side in the
    #: chosen snapshot.
    price: Decimal | None = None
    #: What is ahead. ``None`` means the level's whole displayed size.
    quantity_ahead: Decimal | None = None
    #: Which book to join. ``None`` means the last snapshot in the dataset.
    as_of: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "side": str(self.side),
            "price": format(self.price, "f") if self.price is not None else None,
            "quantity_ahead": (
                format(self.quantity_ahead, "f") if self.quantity_ahead is not None else None
            ),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "horizon_seconds": self.horizon_seconds,
        }


@dataclass(frozen=True, slots=True)
class QueueResult:
    params: QueueParams
    snapshot_timestamp: datetime
    price: Decimal
    outlook: QueueOutlook

    def to_dict(self) -> dict:
        payload = self.outlook.to_dict()
        payload.update(
            {
                "parameters": self.params.to_dict(),
                "snapshot_timestamp": self.snapshot_timestamp.isoformat(),
                "price": format(self.price, "f"),
                "interpretation": (
                    "A bracketed estimate of where a hypothetical order would "
                    "sit and how the size ahead of it behaved, under the stated "
                    "assumptions. It is not a claim about where any exchange "
                    "has placed an order: public data does not carry queue "
                    "priority, so the two ends of the bracket are the two "
                    "answers the data admits."
                ),
            }
        )
        return payload


def _snapshot_at(
    snapshots: tuple[OrderBookSnapshot, ...], as_of: datetime | None
) -> OrderBookSnapshot:
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.exchange_timestamp)
    if as_of is None:
        return ordered[-1]
    stamps = [snapshot.exchange_timestamp for snapshot in ordered]
    index = bisect.bisect_right(stamps, as_of) - 1
    if index < 0:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            f"The dataset's first snapshot is at {stamps[0].isoformat()}, after "
            f"{as_of.isoformat()}. There is no book at that instant to join, and "
            "using a later one would place the order in a book that did not "
            "exist yet.",
        )
    return ordered[index]


def estimate(
    snapshots: tuple[OrderBookSnapshot, ...],
    events: tuple[BookEvent, ...],
    params: QueueParams,
) -> QueueResult:
    """Assemble the level, the departures at it, and hand them to the model."""
    if not snapshots:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            "There is no depth snapshot to read the resting size from.",
        )
    snapshot = _snapshot_at(snapshots, params.as_of)
    levels = snapshot.bids if params.side is BookSide.BID else snapshot.asks
    if not levels:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            f"The snapshot at {snapshot.exchange_timestamp.isoformat()} has no "
            f"{params.side} levels, so there is no queue to join.",
        )

    price = params.price if params.price is not None else levels[0].price
    matching = [level for level in levels if level.price == price]
    if not matching:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            f"No {params.side} level is displayed at {price} in the snapshot at "
            f"{snapshot.exchange_timestamp.isoformat()}. An order cannot be "
            "queued behind size the book was not showing.",
        )
    level_quantity = matching[0].quantity
    quantity_ahead = params.quantity_ahead if params.quantity_ahead is not None else level_quantity

    ordered = sorted(events, key=lambda event: event.exchange_timestamp)
    if not ordered:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            "The dataset has no event tape, so no departure from the level was ever observed.",
        )
    window = (ordered[-1].exchange_timestamp - ordered[0].exchange_timestamp).total_seconds()

    trades = [
        event
        for event in ordered
        if event.event_type is BookEventType.TRADE
        and event.price == price
        and (event.side is None or event.side is params.side)
    ]
    cancels = [
        event
        for event in ordered
        if event.event_type is BookEventType.CANCEL
        and event.price == price
        and event.side is params.side
    ]

    outlook = estimate_queue_outlook(
        quantity_ahead=float(quantity_ahead),
        level_quantity=float(level_quantity),
        trades_observed=len(trades),
        traded_quantity=float(sum((event.quantity or Decimal(0)) for event in trades)),
        cancels_observed=len(cancels),
        cancelled_quantity=float(sum((event.quantity or Decimal(0)) for event in cancels)),
        observation_window_seconds=window,
        horizon_seconds=params.horizon_seconds,
    )
    return QueueResult(
        params=params,
        snapshot_timestamp=snapshot.exchange_timestamp,
        price=price,
        outlook=outlook,
    )
