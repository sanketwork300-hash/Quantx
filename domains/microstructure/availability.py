"""The data-availability gate.

Phase 10 exists behind this module. Every other phase computes what it can and
degrades with a warning; microstructure does not get that latitude, because the
failure mode is different. A volatility surface fitted to thin data is visibly
uncertain — wide confidence, few observations, a warning the user reads. An
order-book imbalance computed from a one-level feed, or a queue position
inferred from a tape with holes in it, looks exactly like the real thing. There
is nothing in the number that says it came from data that could not support it.

So the answer to "what can this dataset support?" is computed once, stored with
the dataset, and consulted before anything else runs. A capability is
``GRANTED`` or ``REFUSED``; a refusal carries a closed-vocabulary reason, a
sentence saying what was missing, and the evidence it was decided on. There is
no third state and no override: an endpoint whose capability is refused returns
the refusal, not a number with a caveat attached.

**The thresholds are stated, not tuned.** Each is a minimum below which the
measurement is not the measurement it claims to be — two levels for a slope
because a line through one point is not a fit, fifty events for a three-
parameter arrival model, a cancellation before a cancellation intensity. None
of them was chosen by seeing which produced better output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domains.microstructure.models import DatasetProfile

GATE_VERSION = "microstructure-availability@1.0.0"

#: Two levels per side, because :func:`quant.microstructure.book.book_slope`
#: fits through the origin and a single point makes the "fit" an identity.
MIN_DEPTH_LEVELS = 2

#: A three-parameter arrival model needs enough arrivals for its parameters to
#: be separately identifiable, plus a held-out window the comparison can see.
#: These mirror the constants in :mod:`quant.microstructure.intensity`, which is
#: where they are argued for.
MIN_INTENSITY_EVENTS = 70
MIN_CANCEL_EVENTS = 20

#: Below this, the window is too short for a rate to mean anything: an intensity
#: measured over ten seconds is a statement about those ten seconds.
MIN_WINDOW_SECONDS = 60.0

#: Above this share of consecutive events sharing a timestamp, the tape's clock
#: resolution — not the market — is what an inter-arrival model would be
#: measuring. One in five is already generous.
MAX_TIED_EVENT_FRACTION = 0.20


class MicrostructureCapability(StrEnum):
    """What a dataset can be asked for."""

    #: Spread, mid, microprice, top-of-book imbalance.
    TOP_OF_BOOK = "TOP_OF_BOOK"
    #: Multi-level depth, weighted imbalance, book slope, concentration.
    DEPTH_ANALYTICS = "DEPTH_ANALYTICS"
    #: Arrival rates for events of a given type.
    EVENT_INTENSITY = "EVENT_INTENSITY"
    #: Cancellation rates specifically, which the queue model needs separately.
    CANCELLATION_INTENSITY = "CANCELLATION_INTENSITY"
    #: The Poisson-versus-Hawkes held-out comparison.
    SELF_EXCITATION = "SELF_EXCITATION"
    #: The bracketed queue outlook.
    QUEUE_POSITION = "QUEUE_POSITION"


class AvailabilityRefusal(StrEnum):
    """Why a capability is not available on this dataset."""

    NO_SNAPSHOTS = "NO_SNAPSHOTS"
    NO_TWO_SIDED_SNAPSHOTS = "NO_TWO_SIDED_SNAPSHOTS"
    SINGLE_LEVEL_ONLY = "SINGLE_LEVEL_ONLY"
    NO_EVENTS = "NO_EVENTS"
    TOO_FEW_EVENTS = "TOO_FEW_EVENTS"
    NO_CANCEL_EVENTS = "NO_CANCEL_EVENTS"
    TOO_FEW_CANCEL_EVENTS = "TOO_FEW_CANCEL_EVENTS"
    NO_TRADE_EVENTS = "NO_TRADE_EVENTS"
    NO_EVENT_PRICES = "NO_EVENT_PRICES"
    NO_EVENT_SIDES = "NO_EVENT_SIDES"
    WINDOW_TOO_SHORT = "WINDOW_TOO_SHORT"
    TIMESTAMP_RESOLUTION_TOO_COARSE = "TIMESTAMP_RESOLUTION_TOO_COARSE"
    NO_SEQUENCE_NUMBERS = "NO_SEQUENCE_NUMBERS"
    SEQUENCE_NOT_MONOTONE = "SEQUENCE_NOT_MONOTONE"
    SEQUENCE_HAS_GAPS = "SEQUENCE_HAS_GAPS"


@dataclass(frozen=True, slots=True)
class CapabilityAssessment:
    capability: MicrostructureCapability
    is_available: bool
    reason: AvailabilityRefusal | None
    message: str
    #: The numbers the decision was made on, so a refusal can be argued with.
    evidence: dict

    def to_dict(self) -> dict:
        return {
            "capability": str(self.capability),
            "available": self.is_available,
            "reason": str(self.reason) if self.reason else None,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class AvailabilityReport:
    profile: DatasetProfile
    assessments: tuple[CapabilityAssessment, ...]
    gate_version: str = GATE_VERSION

    def __getitem__(self, capability: MicrostructureCapability) -> CapabilityAssessment:
        for assessment in self.assessments:
            if assessment.capability is capability:
                return assessment
        raise KeyError(capability)

    def allows(self, capability: MicrostructureCapability) -> bool:
        return self[capability].is_available

    def require(self, capability: MicrostructureCapability) -> None:
        """Raise unless the capability is granted. The one way through the gate."""
        assessment = self[capability]
        if not assessment.is_available:
            raise CapabilityRefused(assessment)

    @property
    def available(self) -> tuple[MicrostructureCapability, ...]:
        return tuple(a.capability for a in self.assessments if a.is_available)

    @property
    def refused(self) -> tuple[MicrostructureCapability, ...]:
        return tuple(a.capability for a in self.assessments if not a.is_available)

    def to_dict(self) -> dict:
        return {
            "gate_version": self.gate_version,
            "profile": self.profile.to_dict(),
            "capabilities": [a.to_dict() for a in self.assessments],
            "available": [str(c) for c in self.available],
            "refused": [str(c) for c in self.refused],
            "thresholds": {
                "min_depth_levels": MIN_DEPTH_LEVELS,
                "min_intensity_events": MIN_INTENSITY_EVENTS,
                "min_cancel_events": MIN_CANCEL_EVENTS,
                "min_window_seconds": MIN_WINDOW_SECONDS,
                "max_tied_event_fraction": MAX_TIED_EVENT_FRACTION,
            },
        }


class CapabilityRefused(Exception):
    """A capability the dataset does not support was asked for anyway."""

    def __init__(self, assessment: CapabilityAssessment) -> None:
        super().__init__(assessment.message)
        self.assessment = assessment

    @property
    def capability(self) -> MicrostructureCapability:
        return self.assessment.capability

    @property
    def reason(self) -> AvailabilityRefusal | None:
        return self.assessment.reason


def _granted(
    capability: MicrostructureCapability, message: str, **evidence
) -> CapabilityAssessment:
    return CapabilityAssessment(capability, True, None, message, evidence)


def _refused(
    capability: MicrostructureCapability,
    reason: AvailabilityRefusal,
    message: str,
    **evidence,
) -> CapabilityAssessment:
    return CapabilityAssessment(capability, False, reason, message, evidence)


def _top_of_book(profile: DatasetProfile) -> CapabilityAssessment:
    capability = MicrostructureCapability.TOP_OF_BOOK
    evidence = {
        "snapshots": profile.snapshots,
        "two_sided_snapshots": profile.two_sided_snapshots,
    }
    if profile.snapshots == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_SNAPSHOTS,
            "This dataset holds no depth snapshots, and a spread, a mid or a "
            "microprice is a property of a book at an instant. An event tape "
            "alone does not carry one without a book reconstruction, which this "
            "platform does not perform.",
            **evidence,
        )
    if profile.two_sided_snapshots == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_TWO_SIDED_SNAPSHOTS,
            f"None of the {profile.snapshots} snapshots has a price on both "
            "sides. A one-sided book has no mid and no spread, and using the "
            "side that is present would report half a market as a whole one.",
            **evidence,
        )
    return _granted(
        capability,
        f"{profile.two_sided_snapshots} of {profile.snapshots} snapshots are "
        "two-sided, so spread, mid, microprice and top-of-book imbalance are "
        "measurable on those.",
        **evidence,
    )


def _depth_analytics(profile: DatasetProfile) -> CapabilityAssessment:
    capability = MicrostructureCapability.DEPTH_ANALYTICS
    evidence = {
        "snapshots": profile.snapshots,
        "min_levels": profile.min_levels,
        "median_levels": profile.median_levels,
        "max_levels": profile.max_levels,
        "required_levels": MIN_DEPTH_LEVELS,
    }
    if profile.snapshots == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_SNAPSHOTS,
            "Depth analytics need depth snapshots, and this dataset has none.",
            **evidence,
        )
    if profile.max_levels < MIN_DEPTH_LEVELS:
        return _refused(
            capability,
            AvailabilityRefusal.SINGLE_LEVEL_ONLY,
            f"No snapshot carries {MIN_DEPTH_LEVELS} levels on both sides, so "
            "this is a top-of-book feed. A book slope needs at least two points "
            "to be a fit rather than an identity, and depth concentration "
            "across one level is one by construction and says nothing.",
            **evidence,
        )
    return _granted(
        capability,
        f"Snapshots carry up to {profile.max_levels} levels per side (median "
        f"{profile.median_levels:g}), so multi-level depth, weighted imbalance, "
        "book slope and depth concentration are measurable.",
        **evidence,
    )


def _event_intensity(profile: DatasetProfile) -> CapabilityAssessment:
    capability = MicrostructureCapability.EVENT_INTENSITY
    evidence = {
        "events": profile.events,
        "span_seconds": profile.span_seconds,
        "required_events": MIN_INTENSITY_EVENTS,
        "required_window_seconds": MIN_WINDOW_SECONDS,
    }
    if profile.events == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_EVENTS,
            "This dataset holds no event tape. Arrival rates are counts of "
            "events over time, and periodic snapshots do not carry the events "
            "between them — the changes they imply are not the messages that "
            "caused them.",
            **evidence,
        )
    if profile.span_seconds < MIN_WINDOW_SECONDS:
        return _refused(
            capability,
            AvailabilityRefusal.WINDOW_TOO_SHORT,
            f"The tape spans {profile.span_seconds:.1f} seconds. A rate measured "
            f"over less than {MIN_WINDOW_SECONDS:.0f} seconds is a statement "
            "about that moment rather than about the arrival process.",
            **evidence,
        )
    if profile.events < MIN_INTENSITY_EVENTS:
        return _refused(
            capability,
            AvailabilityRefusal.TOO_FEW_EVENTS,
            f"{profile.events} events is below the {MIN_INTENSITY_EVENTS} needed "
            "to fit an arrival model on a training window and still leave a "
            "held-out window to score it on.",
            **evidence,
        )
    return _granted(
        capability,
        f"{profile.events} events over {profile.span_seconds:.0f} seconds, which "
        "is enough for an arrival rate and for the held-out comparison that "
        "decides whether a richer model earns its parameters.",
        **evidence,
    )


def _cancellation_intensity(profile: DatasetProfile) -> CapabilityAssessment:
    capability = MicrostructureCapability.CANCELLATION_INTENSITY
    cancels = profile.event_type_counts.get("CANCEL", 0)
    evidence = {
        "cancel_events": cancels,
        "event_type_counts": dict(profile.event_type_counts),
        "required_cancel_events": MIN_CANCEL_EVENTS,
    }
    if profile.events == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_EVENTS,
            "This dataset holds no event tape, so cancellations cannot be "
            "counted.",
            **evidence,
        )
    if cancels == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_CANCEL_EVENTS,
            "The tape contains no events labelled as cancellations. Deriving "
            "them from size decreases between snapshots would conflate a "
            "cancellation with a trade, and those move a queue very differently.",
            **evidence,
        )
    if cancels < MIN_CANCEL_EVENTS:
        return _refused(
            capability,
            AvailabilityRefusal.TOO_FEW_CANCEL_EVENTS,
            f"{cancels} cancellations is below the {MIN_CANCEL_EVENTS} at which "
            "a cancellation rate is a measurement rather than a handful of "
            "coincidences.",
            **evidence,
        )
    return _granted(
        capability,
        f"{cancels} labelled cancellations, so a cancellation rate can be "
        "measured separately from the trade rate.",
        **evidence,
    )


def _self_excitation(profile: DatasetProfile) -> CapabilityAssessment:
    capability = MicrostructureCapability.SELF_EXCITATION
    evidence = {
        "events": profile.events,
        "span_seconds": profile.span_seconds,
        "tied_event_fraction": profile.tied_event_fraction,
        "required_events": MIN_INTENSITY_EVENTS,
        "max_tied_event_fraction": MAX_TIED_EVENT_FRACTION,
    }
    intensity = _event_intensity(profile)
    if not intensity.is_available:
        return _refused(
            capability,
            intensity.reason or AvailabilityRefusal.NO_EVENTS,
            "A self-excitation model is an arrival model with two more "
            f"parameters, so it inherits the same refusal: {intensity.message}",
            **evidence,
        )
    if profile.tied_event_fraction > MAX_TIED_EVENT_FRACTION:
        return _refused(
            capability,
            AvailabilityRefusal.TIMESTAMP_RESOLUTION_TOO_COARSE,
            f"{profile.tied_event_fraction:.0%} of consecutive events share a "
            "timestamp, so the tape's clock is coarser than the clustering a "
            "self-exciting model measures. Fitting one here would estimate the "
            "recording resolution and report it as market behaviour: a decay "
            "faster than the tick is unidentifiable, and the excitation would "
            "be absorbed into whatever the optimiser could see.",
            **evidence,
        )
    return _granted(
        capability,
        f"{profile.events} events with distinct enough timestamps "
        f"({profile.tied_event_fraction:.1%} tied) for a self-exciting model to "
        "be fitted and, separately, for it to be judged against a constant rate "
        "on data it was not fitted on.",
        **evidence,
    )


def _queue_position(profile: DatasetProfile) -> CapabilityAssessment:
    capability = MicrostructureCapability.QUEUE_POSITION
    sequencing = profile.event_sequencing
    trades = profile.event_type_counts.get("TRADE", 0)
    cancels = profile.event_type_counts.get("CANCEL", 0)
    evidence = {
        "snapshots": profile.snapshots,
        "events": profile.events,
        "trade_events": trades,
        "cancel_events": cancels,
        "priced_events": profile.priced_events,
        "labelled_side_events": profile.labelled_side_events,
        "sequencing": sequencing.to_dict(),
    }
    if profile.events == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_EVENTS,
            "Queue position is a statement about the order of messages at a "
            "price level. Without an event tape there is no order to reason "
            "about, and a snapshot series cannot supply one.",
            **evidence,
        )
    if profile.snapshots == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_SNAPSHOTS,
            "The queue ahead of an order is the size resting at its level when "
            "it joins, which is read from a depth snapshot. Without snapshots "
            "there is nothing to place the order behind.",
            **evidence,
        )
    if profile.priced_events == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_EVENT_PRICES,
            "The events carry no price, so they cannot be attributed to a price "
            "level. A departure rate averaged over the whole book is not the "
            "departure rate at the level an order is resting on.",
            **evidence,
        )
    if profile.labelled_side_events == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_EVENT_SIDES,
            "The events carry no side, so a cancellation on the bid cannot be "
            "told from one on the ask at a crossed or recently moved level.",
            **evidence,
        )
    if trades == 0 and cancels == 0:
        return _refused(
            capability,
            AvailabilityRefusal.NO_TRADE_EVENTS,
            "The tape contains neither trades nor cancellations, so nothing in "
            "it ever removes size from a level and no queue could ever drain.",
            **evidence,
        )
    if not sequencing.present:
        return _refused(
            capability,
            AvailabilityRefusal.NO_SEQUENCE_NUMBERS,
            "The events carry no sequence numbers, so there is no way to tell a "
            "complete tape from one with messages missing. A queue estimate "
            "built on a tape with a hole in it describes a different book, and "
            "nothing in the number would say so.",
            **evidence,
        )
    if not sequencing.monotone:
        return _refused(
            capability,
            AvailabilityRefusal.SEQUENCE_NOT_MONOTONE,
            "The sequence numbers do not increase with the timestamps, so the "
            "tape's own ordering disagrees with its clock. Queue arithmetic "
            "depends on the order being right, and here it is not known which "
            "of the two to believe.",
            **evidence,
        )
    if sequencing.missing_in_range > 0:
        return _refused(
            capability,
            AvailabilityRefusal.SEQUENCE_HAS_GAPS,
            f"{sequencing.missing_in_range} sequence numbers in the observed "
            "range were never seen. Each gap may have carried a cancellation or "
            "a trade at the level in question, so the size that left it is a "
            "lower bound and the wait derived from it would be an overestimate "
            "presented as a measurement.",
            **evidence,
        )
    return _granted(
        capability,
        f"{profile.events} priced, sided events with a complete monotone "
        f"sequence, and {profile.snapshots} snapshots to read the resting size "
        "from. The outlook is still a bracket, because public data never "
        "carries queue priority itself.",
        **evidence,
    )


def assess(profile: DatasetProfile) -> AvailabilityReport:
    """Decide, once, what this dataset can support."""
    return AvailabilityReport(
        profile=profile,
        assessments=(
            _top_of_book(profile),
            _depth_analytics(profile),
            _event_intensity(profile),
            _cancellation_intensity(profile),
            _self_excitation(profile),
            _queue_position(profile),
        ),
    )
