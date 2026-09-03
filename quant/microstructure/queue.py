"""Where a resting order sits in a queue, and what that implies for a fill.

This is the most gated calculation in the platform, and it is worth being
precise about why. An exchange knows the order of the queue at a price level.
An observer of a public feed does not: they see the level's total size and the
events that change it, and from those they can bound where an order would sit
and how fast the size ahead of it goes away. They cannot see priority, they
cannot see which specific orders cancelled, and on most feeds they cannot see
hidden size at all. So every number here is probabilistic and says so, and the
one assumption that changes the answer most is not chosen — it is reported as
both ends of a bracket.

**The fork.** A cancellation at our price level either removes size ahead of us
or behind us, and the feed does not say which:

* ``CANCELS_AHEAD`` — every cancellation removes size in front. The queue
  drains fastest, so this end of the bracket is the optimistic one.
* ``CANCELS_BEHIND`` — only trades move us forward. The slowest drain, and the
  pessimistic end.

The truth is between them and depends on where in the queue cancellations
actually concentrate, which is a property of the venue and the participants
rather than something derivable. So the estimate is a region bracketed by the
two, in the same spirit as the Phase 6 margin-shortfall region: the two rungs
that locate the answer are reported alongside it, and nothing pretends to know
where inside them it falls.

**Method.** Departures at the level are treated as a Poisson counting process
at the observed event rate, each removing the observed mean size. That is an
approximation with two named costs: it ignores the variance of event sizes, and
it ignores clustering in the arrivals — which is exactly what the Hawkes model
in :mod:`quant.microstructure.intensity` is for, and why a queue estimate
computed alongside an adopted Hawkes fit records that its own arrival model is
the simpler one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from scipy.stats import poisson

__all__ = [
    "CancellationPriority",
    "QueueEstimate",
    "QueueOutlook",
    "QueueRefusal",
    "QueueUnavailable",
    "estimate_queue_outlook",
]

MODEL_VERSION = "queue@1.0.0"


class CancellationPriority(StrEnum):
    """Where cancellations at our price level are assumed to come from."""

    CANCELS_AHEAD = "CANCELS_AHEAD"
    CANCELS_BEHIND = "CANCELS_BEHIND"


class QueueRefusal(StrEnum):
    NO_DEPARTURES_OBSERVED = "NO_DEPARTURES_OBSERVED"
    NON_POSITIVE_HORIZON = "NON_POSITIVE_HORIZON"
    NEGATIVE_QUEUE = "NEGATIVE_QUEUE"
    NO_EVENT_SIZES = "NO_EVENT_SIZES"


class QueueUnavailable(Exception):
    def __init__(self, reason: QueueRefusal, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class QueueEstimate:
    """One end of the bracket: the outlook under one cancellation assumption."""

    priority: CancellationPriority
    #: Quantity that must leave the level before our order trades.
    quantity_ahead: float
    #: Volume per second leaving the level under this assumption.
    departure_rate: float
    #: Events per second leaving the level under this assumption.
    event_rate: float
    mean_event_size: float
    #: How many departure events at the mean size the queue ahead amounts to.
    events_required: int
    expected_wait_seconds: float
    fill_probability: float
    horizon_seconds: float

    def to_dict(self) -> dict:
        return {
            "priority_assumption": str(self.priority),
            "quantity_ahead": self.quantity_ahead,
            "departure_rate_per_second": self.departure_rate,
            "event_rate_per_second": self.event_rate,
            "mean_event_size": self.mean_event_size,
            "events_required": self.events_required,
            "estimated_wait_seconds": self.expected_wait_seconds,
            "estimated_fill_probability": self.fill_probability,
            "horizon_seconds": self.horizon_seconds,
        }


@dataclass(frozen=True, slots=True)
class QueueOutlook:
    """The bracket, and everything needed to argue with it."""

    quantity_ahead: float
    level_quantity: float
    horizon_seconds: float
    observation_window_seconds: float
    trades_observed: int
    cancels_observed: int
    optimistic: QueueEstimate
    pessimistic: QueueEstimate
    assumptions: tuple[str, ...]
    confidence: float

    @property
    def fill_probability_range(self) -> tuple[float, float]:
        """``(pessimistic, optimistic)``. There is no single number here."""
        return self.pessimistic.fill_probability, self.optimistic.fill_probability

    @property
    def wait_seconds_range(self) -> tuple[float, float]:
        """``(optimistic, pessimistic)``, in seconds. Shortest wait first."""
        return self.optimistic.expected_wait_seconds, self.pessimistic.expected_wait_seconds

    @property
    def queue_position_fraction(self) -> float | None:
        """Share of the level's displayed size that sits ahead of the order.

        Displayed size only. A venue with hidden or iceberg liquidity has more
        ahead than this, and no public feed says how much.
        """
        if self.level_quantity <= 0.0:
            return None
        return self.quantity_ahead / self.level_quantity

    def to_dict(self) -> dict:
        low, high = self.fill_probability_range
        fast, slow = self.wait_seconds_range
        return {
            "estimated_queue_position": self.quantity_ahead,
            "queue_position_fraction_of_displayed_size": self.queue_position_fraction,
            "level_quantity": self.level_quantity,
            "horizon_seconds": self.horizon_seconds,
            "observation_window_seconds": self.observation_window_seconds,
            "trades_observed": self.trades_observed,
            "cancels_observed": self.cancels_observed,
            "estimated_fill_probability_range": [low, high],
            "estimated_wait_seconds_range": [fast, slow],
            "optimistic": self.optimistic.to_dict(),
            "pessimistic": self.pessimistic.to_dict(),
            "assumptions": list(self.assumptions),
            "confidence": self.confidence,
            "model_version": MODEL_VERSION,
        }


def _estimate(
    priority: CancellationPriority,
    quantity_ahead: float,
    event_rate: float,
    mean_event_size: float,
    horizon_seconds: float,
) -> QueueEstimate:
    departure_rate = event_rate * mean_event_size
    if quantity_ahead <= 0.0:
        events_required = 0
        wait = 0.0
        probability = 1.0
    else:
        events_required = max(1, math.ceil(quantity_ahead / mean_event_size))
        wait = quantity_ahead / departure_rate if departure_rate > 0.0 else math.inf
        # P(at least `events_required` departures inside the horizon) under a
        # Poisson count. `sf(k-1)` is P(N >= k).
        probability = (
            float(poisson.sf(events_required - 1, event_rate * horizon_seconds))
            if event_rate > 0.0
            else 0.0
        )
    return QueueEstimate(
        priority=priority,
        quantity_ahead=quantity_ahead,
        departure_rate=departure_rate,
        event_rate=event_rate,
        mean_event_size=mean_event_size,
        events_required=events_required,
        expected_wait_seconds=wait,
        fill_probability=probability,
        horizon_seconds=horizon_seconds,
    )


def estimate_queue_outlook(
    quantity_ahead: float,
    level_quantity: float,
    trades_observed: int,
    traded_quantity: float,
    cancels_observed: int,
    cancelled_quantity: float,
    observation_window_seconds: float,
    horizon_seconds: float,
) -> QueueOutlook:
    """Bracket the outlook for an order resting behind ``quantity_ahead``.

    ``trades_observed`` / ``cancels_observed`` and their quantities are counted
    at *this price level* over ``observation_window_seconds``. Rates are those
    counts over that window: this is a measurement of what happened at the
    level, not a forecast, and it is only a guide to the next
    ``horizon_seconds`` to the extent the level keeps behaving as it did.

    Refuses when nothing was observed to leave the level. A fill probability
    computed from a rate of zero is exactly zero, which reads as "this will not
    fill" when the truth is "this feed did not show us anything happening
    here", and those are different statements.
    """
    if horizon_seconds <= 0.0:
        raise QueueUnavailable(
            QueueRefusal.NON_POSITIVE_HORIZON,
            f"a horizon of {horizon_seconds} seconds contains no time to fill in",
        )
    if quantity_ahead < 0.0:
        raise QueueUnavailable(
            QueueRefusal.NEGATIVE_QUEUE,
            f"a negative queue ahead is not a queue position: {quantity_ahead}",
        )
    if observation_window_seconds <= 0.0:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            "the observation window is empty, so no departure rate was measured",
        )
    if trades_observed + cancels_observed == 0:
        raise QueueUnavailable(
            QueueRefusal.NO_DEPARTURES_OBSERVED,
            "no trade or cancellation was observed at this price level in the "
            "window. A fill probability computed from that would be zero, which "
            "would read as a statement about the order rather than about the data.",
        )
    if traded_quantity + cancelled_quantity <= 0.0:
        raise QueueUnavailable(
            QueueRefusal.NO_EVENT_SIZES,
            "the observed departures carry no quantity, so there is no mean "
            "event size to consume the queue with",
        )

    trade_rate = trades_observed / observation_window_seconds
    cancel_rate = cancels_observed / observation_window_seconds

    if trades_observed > 0:
        mean_trade_size = traded_quantity / trades_observed
    else:
        # Nothing traded here. The pessimistic end has no departures at all,
        # which the estimate below turns into an infinite wait and a zero
        # probability — the correct reading of "only cancellations were seen".
        mean_trade_size = traded_quantity if traded_quantity > 0 else 1.0

    both_events = trades_observed + cancels_observed
    mean_both_size = (traded_quantity + cancelled_quantity) / both_events

    optimistic = _estimate(
        CancellationPriority.CANCELS_AHEAD,
        quantity_ahead,
        trade_rate + cancel_rate,
        mean_both_size,
        horizon_seconds,
    )
    pessimistic = _estimate(
        CancellationPriority.CANCELS_BEHIND,
        quantity_ahead,
        trade_rate,
        mean_trade_size,
        horizon_seconds,
    )

    assumptions = (
        "Queue priority is strict first-in-first-out at the price level.",
        "Only displayed size is counted; hidden and iceberg quantity is invisible "
        "to this feed and would sit ahead of the order without appearing here.",
        "Departures are modelled as a Poisson counting process at the observed "
        "event rate, each removing the observed mean size. Event-size variance "
        "and arrival clustering are not modelled.",
        "The rates measured over the observation window are assumed to hold over "
        "the horizon. They are a measurement of the past, not a forecast.",
        "The order is assumed to join at the back of the queue and never to be "
        "modified, which would lose its priority.",
        "This is not a claim about where an exchange has actually placed an "
        "order. Public data does not carry queue priority.",
    )

    # Confidence is the product of three things that can each ruin the estimate,
    # so one bad dimension drives it to zero rather than being averaged away —
    # the same rule as the Phase 0 quality score.
    #   * how much of the level's size the estimate could see
    #   * how many departure events the rates were measured from
    #   * how wide the bracket is, since a wide bracket is a weak answer
    evidence = min(1.0, both_events / 30.0)
    low, high = pessimistic.fill_probability, optimistic.fill_probability
    agreement = 1.0 - min(1.0, high - low)
    coverage = 1.0 if level_quantity <= 0.0 else min(1.0, level_quantity / max(quantity_ahead, 1e-9))
    confidence = float(evidence * agreement * min(coverage, 1.0))

    return QueueOutlook(
        quantity_ahead=quantity_ahead,
        level_quantity=level_quantity,
        horizon_seconds=horizon_seconds,
        observation_window_seconds=observation_window_seconds,
        trades_observed=trades_observed,
        cancels_observed=cancels_observed,
        optimistic=optimistic,
        pessimistic=pessimistic,
        assumptions=assumptions,
        confidence=confidence,
    )
