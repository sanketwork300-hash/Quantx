"""Microstructure domain model.

The canonical observations — :class:`~domains.market_data.models.OrderBookSnapshot`
and :class:`~domains.market_data.models.BookEvent` — live in the market-data
domain with the rest of the market-data schemas. What lives here is everything
about a *dataset*: what was imported, what was refused and why, and what the
data is capable of supporting.

One rule governs the whole module, and it is the same one that governs every
import in the platform: **nothing is dropped without a reason**. The counts
conserve — ``input == kept + rejected`` — and every rejected row carries its
1-based source row number and a closed-vocabulary reason.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domains.market_data.models import BookEvent, OrderBookSnapshot

IMPORT_MODEL_VERSION = "microstructure-import@1.0.0"


class DatasetKind(StrEnum):
    """What a dataset holds. Both parts are optional, but not both at once."""

    SNAPSHOTS_ONLY = "SNAPSHOTS_ONLY"
    EVENTS_ONLY = "EVENTS_ONLY"
    SNAPSHOTS_AND_EVENTS = "SNAPSHOTS_AND_EVENTS"


class SnapshotRejection(StrEnum):
    """Why a depth-snapshot row could not become a snapshot."""

    UNPARSEABLE_ROW = "UNPARSEABLE_ROW"
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    NO_LEVELS = "NO_LEVELS"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
    LEVELS_OUT_OF_ORDER = "LEVELS_OUT_OF_ORDER"
    PRICE_WITHOUT_QUANTITY = "PRICE_WITHOUT_QUANTITY"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"


class EventRejection(StrEnum):
    """Why an event row could not become an event."""

    UNPARSEABLE_ROW = "UNPARSEABLE_ROW"
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    MISSING_EVENT_TYPE = "MISSING_EVENT_TYPE"
    UNRECOGNISED_EVENT_TYPE = "UNRECOGNISED_EVENT_TYPE"
    UNRECOGNISED_SIDE = "UNRECOGNISED_SIDE"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A row that did not make it, and everything needed to find it again."""

    row_number: int
    reason: SnapshotRejection | EventRejection
    message: str
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "reason": str(self.reason),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class SnapshotBatch:
    """Parsed depth snapshots plus the rows that were refused."""

    snapshots: tuple[OrderBookSnapshot, ...]
    rejected: tuple[RejectedRow, ...]
    rows_in: int
    #: The level columns the header parser recognised, so the user can see what
    #: was read as what before committing anything.
    detected_levels: int = 0
    detected_columns: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        total = len(self.snapshots) + len(self.rejected)
        if total != self.rows_in:
            raise ValueError(
                f"{self.rows_in} rows in but {len(self.snapshots)} kept and "
                f"{len(self.rejected)} rejected; a row went missing without a reason"
            )

    def counts(self) -> dict:
        return {
            "input": self.rows_in,
            "kept": len(self.snapshots),
            "rejected": len(self.rejected),
        }


@dataclass(frozen=True, slots=True)
class EventBatch:
    """Parsed book events plus the rows that were refused."""

    events: tuple[BookEvent, ...]
    rejected: tuple[RejectedRow, ...]
    rows_in: int
    detected_columns: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        total = len(self.events) + len(self.rejected)
        if total != self.rows_in:
            raise ValueError(
                f"{self.rows_in} rows in but {len(self.events)} kept and "
                f"{len(self.rejected)} rejected; a row went missing without a reason"
            )

    def counts(self) -> dict:
        return {
            "input": self.rows_in,
            "kept": len(self.events),
            "rejected": len(self.rejected),
        }


@dataclass(frozen=True, slots=True)
class SequencingReport:
    """What the sequence numbers say about whether the tape is complete.

    A hole in the sequence is the difference between "this is what happened at
    the level" and "this is what we were shown". Queue arithmetic that walks a
    tape with a hole in it is arithmetic about a different book, so the gate
    reads this rather than assuming a feed is complete because it parsed.
    """

    present: bool
    monotone: bool
    duplicates: int
    #: ``max - min + 1 - distinct``: how many numbers in the observed range were
    #: never seen. Zero on a complete tape, and only meaningful for a venue
    #: whose sequence numbers are contiguous, which is recorded as a caveat
    #: rather than assumed.
    missing_in_range: int
    first: int | None
    last: int | None

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "monotone": self.monotone,
            "duplicates": self.duplicates,
            "missing_in_range": self.missing_in_range,
            "first": self.first,
            "last": self.last,
            "caveat": (
                "Gaps are counted against a contiguous numbering. A venue that "
                "numbers per-partition or skips by design will show gaps that are "
                "not losses, which is why this is reported rather than acted on "
                "silently."
            ),
        }


def sequencing_report(numbers: list[int | None]) -> SequencingReport:
    present = [number for number in numbers if number is not None]
    if not present:
        return SequencingReport(
            present=False,
            monotone=False,
            duplicates=0,
            missing_in_range=0,
            first=None,
            last=None,
        )
    distinct = set(present)
    monotone = all(
        later >= earlier for earlier, later in zip(present, present[1:], strict=False)
    )
    span = max(present) - min(present) + 1
    return SequencingReport(
        present=True,
        monotone=monotone,
        duplicates=len(present) - len(distinct),
        missing_in_range=max(0, span - len(distinct)),
        first=min(present),
        last=max(present),
    )


@dataclass(frozen=True, slots=True)
class DatasetProfile:
    """The shape of a dataset: what the availability gate reads.

    Deliberately separate from the gate itself. The profile is a description of
    the bytes; the gate is a judgement about what those bytes can support. A
    later capability can be added by extending the gate without re-reading
    every dataset, and a disputed refusal can be argued with against a profile
    that was recorded rather than recomputed.
    """

    instrument_id: uuid.UUID
    kind: DatasetKind
    snapshots: int
    events: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    span_seconds: float
    two_sided_snapshots: int
    #: Minimum of the bid and ask level counts, per snapshot, summarised.
    min_levels: int
    median_levels: float
    max_levels: int
    crossed_snapshots: int
    locked_snapshots: int
    event_type_counts: dict[str, int]
    labelled_side_events: int
    priced_events: int
    #: Share of consecutive events that arrived at the same timestamp. A tape
    #: recorded at one-second resolution has almost all of them tied, and a
    #: self-excitation model fitted to it is measuring the clock.
    tied_event_fraction: float
    snapshot_sequencing: SequencingReport
    event_sequencing: SequencingReport

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "kind": str(self.kind),
            "snapshots": self.snapshots,
            "events": self.events,
            "first_timestamp": (
                self.first_timestamp.isoformat() if self.first_timestamp else None
            ),
            "last_timestamp": (
                self.last_timestamp.isoformat() if self.last_timestamp else None
            ),
            "span_seconds": self.span_seconds,
            "two_sided_snapshots": self.two_sided_snapshots,
            "min_levels": self.min_levels,
            "median_levels": self.median_levels,
            "max_levels": self.max_levels,
            "crossed_snapshots": self.crossed_snapshots,
            "locked_snapshots": self.locked_snapshots,
            "event_type_counts": dict(self.event_type_counts),
            "labelled_side_events": self.labelled_side_events,
            "priced_events": self.priced_events,
            "tied_event_fraction": self.tied_event_fraction,
            "snapshot_sequencing": self.snapshot_sequencing.to_dict(),
            "event_sequencing": self.event_sequencing.to_dict(),
        }


def _levels(snapshot: OrderBookSnapshot) -> int:
    return min(len(snapshot.bids), len(snapshot.asks))


def profile_dataset(
    instrument_id: uuid.UUID,
    snapshots: tuple[OrderBookSnapshot, ...],
    events: tuple[BookEvent, ...],
) -> DatasetProfile:
    """Describe a dataset without judging it."""
    if snapshots and events:
        kind = DatasetKind.SNAPSHOTS_AND_EVENTS
    elif events:
        kind = DatasetKind.EVENTS_ONLY
    else:
        kind = DatasetKind.SNAPSHOTS_ONLY

    stamps: list[datetime] = [snapshot.exchange_timestamp for snapshot in snapshots]
    stamps.extend(event.exchange_timestamp for event in events)
    first = min(stamps) if stamps else None
    last = max(stamps) if stamps else None
    span = (last - first).total_seconds() if first and last else 0.0

    level_counts = sorted(_levels(snapshot) for snapshot in snapshots)
    if level_counts:
        middle = len(level_counts) // 2
        median = (
            float(level_counts[middle])
            if len(level_counts) % 2
            else (level_counts[middle - 1] + level_counts[middle]) / 2.0
        )
    else:
        median = 0.0

    type_counts: dict[str, int] = {}
    for event in events:
        key = str(event.event_type)
        type_counts[key] = type_counts.get(key, 0) + 1

    ordered_events = sorted(events, key=lambda item: item.exchange_timestamp)
    if len(ordered_events) > 1:
        ties = sum(
            1
            for earlier, later in zip(ordered_events, ordered_events[1:], strict=False)
            if later.exchange_timestamp == earlier.exchange_timestamp
        )
        tied_fraction = ties / (len(ordered_events) - 1)
    else:
        tied_fraction = 0.0

    return DatasetProfile(
        instrument_id=instrument_id,
        kind=kind,
        snapshots=len(snapshots),
        events=len(events),
        first_timestamp=first,
        last_timestamp=last,
        span_seconds=span,
        two_sided_snapshots=sum(
            1 for snapshot in snapshots if snapshot.bids and snapshot.asks
        ),
        min_levels=min(level_counts) if level_counts else 0,
        median_levels=median,
        max_levels=max(level_counts) if level_counts else 0,
        crossed_snapshots=sum(
            1
            for snapshot in snapshots
            if snapshot.best_bid
            and snapshot.best_ask
            and snapshot.best_bid.price > snapshot.best_ask.price
        ),
        locked_snapshots=sum(
            1
            for snapshot in snapshots
            if snapshot.best_bid
            and snapshot.best_ask
            and snapshot.best_bid.price == snapshot.best_ask.price
        ),
        event_type_counts=type_counts,
        labelled_side_events=sum(1 for event in events if event.side is not None),
        priced_events=sum(1 for event in events if event.price is not None),
        tied_event_fraction=tied_fraction,
        snapshot_sequencing=sequencing_report(
            [snapshot.sequence_number for snapshot in snapshots]
        ),
        event_sequencing=sequencing_report([event.sequence_number for event in events]),
    )


def as_decimal(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))
