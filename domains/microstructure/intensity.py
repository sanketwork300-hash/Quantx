"""Fitting arrival intensities to a dataset's event tape, behind the gate.

The domain's job here is narrow and mostly about honesty at the edges: select
the events the caller asked about, hand them to
:mod:`quant.microstructure.intensity` as seconds since the window start, and
carry the held-out verdict back out intact. The one thing it must not do is
present the richer model when the comparison did not adopt it — so the result
carries an ``adopted_model`` that is the Poisson baseline unless the Hawkes fit
won on data it had not seen, and the Hawkes parameters travel alongside as a
*rejected candidate* rather than as an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from domains.market_data.enums import BookEventType, BookSide
from domains.market_data.models import BookEvent
from quant.microstructure.intensity import (
    DIEBOLD_MARIANO_CRITICAL_VALUE,
    HeldOutComparison,
    IntensityRefusal,
    IntensityUnavailable,
    compare_held_out,
)

INTENSITY_MODEL_VERSION = "microstructure-intensity@1.0.0"


@dataclass(frozen=True, slots=True)
class IntensityParams:
    """Which events to model, and how to split them.

    ``event_types`` empty means every event on the tape, which is a different
    process from the trade tape or the cancellation tape and is labelled as
    such. Mixing them and calling the result "order flow" would fit one model
    to a superposition of several.
    """

    event_types: tuple[BookEventType, ...] = ()
    side: BookSide | None = None
    price: Decimal | None = None
    train_fraction: float = 0.7
    critical_value: float = DIEBOLD_MARIANO_CRITICAL_VALUE

    @property
    def scope(self) -> str:
        types = "+".join(sorted(str(item) for item in self.event_types)) or "ALL"
        side = str(self.side) if self.side else "BOTH"
        price = format(self.price, "f") if self.price is not None else "ANY"
        return f"{types}/{side}/{price}"

    def to_dict(self) -> dict:
        return {
            "event_types": [str(item) for item in self.event_types] or ["ALL"],
            "side": str(self.side) if self.side else None,
            "price": format(self.price, "f") if self.price is not None else None,
            "train_fraction": self.train_fraction,
            "critical_value": self.critical_value,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class IntensityResult:
    params: IntensityParams
    window_start: datetime
    window_end: datetime
    split_timestamp: datetime
    events_selected: int
    comparison: HeldOutComparison

    @property
    def adopted_model(self) -> str:
        return "HAWKES_EXPONENTIAL" if self.comparison.hawkes_is_adopted else "POISSON"

    @property
    def adopted_rate(self) -> float:
        """The long-run rate implied by whichever model was adopted."""
        if self.comparison.hawkes_is_adopted:
            rate = self.comparison.hawkes_train.parameters.stationary_rate  # type: ignore[union-attr]
            if rate is not None:
                return rate
        return self.comparison.poisson_train.parameters.rate  # type: ignore[union-attr]

    def to_dict(self) -> dict:
        payload = self.comparison.to_dict()
        payload.update(
            {
                "model_version": INTENSITY_MODEL_VERSION,
                "parameters": self.params.to_dict(),
                "window": {
                    "start": self.window_start.isoformat(),
                    "end": self.window_end.isoformat(),
                    "split": self.split_timestamp.isoformat(),
                },
                "events_selected": self.events_selected,
                "adopted_model": self.adopted_model,
                "adopted_rate_per_second": self.adopted_rate,
                "interpretation": (
                    "An arrival rate for the selected events over the window "
                    "observed. It is a measurement of that window, not a "
                    "forecast, and the self-exciting model is reported only "
                    "when it beat a constant rate on events it was not fitted "
                    "on."
                ),
            }
        )
        return payload


def select_events(events: tuple[BookEvent, ...], params: IntensityParams) -> list[BookEvent]:
    """Filter to the scope being modelled, in time order."""
    selected = [
        event
        for event in events
        if (not params.event_types or event.event_type in params.event_types)
        and (params.side is None or event.side is params.side)
        and (params.price is None or event.price == params.price)
    ]
    selected.sort(key=lambda event: event.exchange_timestamp)
    return selected


def fit_intensity(
    events: tuple[BookEvent, ...],
    params: IntensityParams,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> IntensityResult:
    """Run the Poisson-versus-Hawkes comparison over the selected events.

    The window defaults to the span of the *whole tape* rather than of the
    selected events, so that a scope with a quiet start is measured over the
    time it was actually quiet for. Taking the first and last selected event as
    the window would delete every second in which nothing of that kind arrived,
    which is exactly the evidence a rate is made of.

    Raises :class:`~quant.microstructure.intensity.IntensityUnavailable` when
    the selection is too thin, which the caller turns into a refusal rather
    than a number.
    """
    ordered = sorted(events, key=lambda event: event.exchange_timestamp)
    if not ordered:
        raise IntensityUnavailable(
            IntensityRefusal.TOO_FEW_TRAINING_EVENTS,
            "The dataset has no events at all, so there is no arrival process to fit.",
        )

    start = window_start or ordered[0].exchange_timestamp
    end = window_end or ordered[-1].exchange_timestamp
    selected = select_events(events, params)

    # Seconds since the window start. A point-process likelihood is invariant
    # to the origin, but a shared origin keeps the split reportable as a wall
    # clock time rather than as an offset nobody can check.
    offsets = [(event.exchange_timestamp - start).total_seconds() for event in selected]
    span = (end - start).total_seconds()

    comparison = compare_held_out(
        offsets,
        0.0,
        span,
        train_fraction=params.train_fraction,
        critical_value=params.critical_value,
    )
    split = start + (end - start) * params.train_fraction
    return IntensityResult(
        params=params,
        window_start=start,
        window_end=end,
        split_timestamp=split,
        events_selected=len(selected),
        comparison=comparison,
    )
