"""Where a row goes, and how a query finds it again.

The layout is Hive-style — ``exchange=NSE/year=2026/month=09/day=09`` — for one
reason that matters: DuckDB reads it natively, so a query for one exchange over
one week reads the files for that exchange and that week and no others. A layout
that needed a lookup table to prune would make the "queryable" half of this
phase a wrapper around a full scan.

The partition columns are also *columns*, recoverable from the path alone. A
file found on its own still says which exchange, which day and which instrument
it belongs to, which is the same property the parquet footer metadata gives.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from domains.warehouse.enums import DatasetKind, DatasetLayer

ROOT = "warehouse"

#: Path segments are built from server-side values only, but the check costs
#: nothing and removes a class of bug if that ever stops being true.
_SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class PartitionError(ValueError):
    """A partition key could not be built or parsed."""


@dataclass(frozen=True, slots=True)
class PartitionKey:
    """One day of one instrument's data on one exchange."""

    layer: DatasetLayer
    kind: DatasetKind
    exchange: str
    day: date
    instrument_id: uuid.UUID

    def __post_init__(self) -> None:
        if not _SAFE.match(self.exchange):
            raise PartitionError(f"exchange {self.exchange!r} is not a usable path segment")

    @property
    def directory(self) -> str:
        return (
            f"{ROOT}/{self.layer}/{self.kind}"
            f"/exchange={self.exchange}"
            f"/year={self.day.year:04d}"
            f"/month={self.day.month:02d}"
            f"/day={self.day.day:02d}"
            f"/instrument_id={self.instrument_id}"
        )

    @property
    def object_key(self) -> str:
        return f"{self.directory}/part.parquet"

    def to_dict(self) -> dict:
        return {
            "layer": str(self.layer),
            "kind": str(self.kind),
            "exchange": self.exchange,
            "day": self.day.isoformat(),
            "instrument_id": str(self.instrument_id),
            "object_key": self.object_key,
        }


def dataset_prefix(layer: DatasetLayer, kind: DatasetKind) -> str:
    """Everything of one kind in one layer, across every exchange and day."""
    return f"{ROOT}/{layer}/{kind}"


def glob_for(
    layer: DatasetLayer,
    kind: DatasetKind,
    exchange: str | None = None,
    instrument_id: uuid.UUID | None = None,
) -> str:
    """A glob a Parquet reader can expand, pruned as far as the filters allow.

    Day filtering is deliberately *not* in the glob. Pruning by year and month
    in the path would work, but pruning by a date range does not express as one
    glob, and a reader that understands Hive partitioning prunes on the
    partition columns anyway — from the values, not from a pattern.
    """
    parts = [dataset_prefix(layer, kind)]
    parts.append(f"exchange={exchange}" if exchange else "exchange=*")
    parts.extend(["year=*", "month=*", "day=*"])
    parts.append(f"instrument_id={instrument_id}" if instrument_id else "instrument_id=*")
    parts.append("part.parquet")
    return "/".join(parts)


def parse_key(key: str) -> PartitionKey:
    """Recover a partition key from an object key.

    The inverse of :attr:`PartitionKey.object_key`, and a property test: a file
    found on its own has to be identifiable from its path.
    """
    parts = key.split("/")
    if len(parts) != 9 or parts[0] != ROOT or parts[-1] != "part.parquet":
        raise PartitionError(f"not a warehouse partition key: {key!r}")

    _root, layer, kind, exchange, year, month, day, instrument = parts[:8]
    fields = {}
    for segment, name in (
        (exchange, "exchange"),
        (year, "year"),
        (month, "month"),
        (day, "day"),
        (instrument, "instrument_id"),
    ):
        prefix = f"{name}="
        if not segment.startswith(prefix):
            raise PartitionError(f"segment {segment!r} does not name {name}")
        fields[name] = segment[len(prefix) :]

    try:
        return PartitionKey(
            layer=DatasetLayer(layer),
            kind=DatasetKind(kind),
            exchange=fields["exchange"],
            day=date(int(fields["year"]), int(fields["month"]), int(fields["day"])),
            instrument_id=uuid.UUID(fields["instrument_id"]),
        )
    except (ValueError, KeyError) as exc:
        raise PartitionError(f"malformed warehouse partition key {key!r}: {exc}") from exc


def day_of(moment: datetime) -> date:
    """The UTC calendar day a timestamp belongs to.

    Partitioning is on UTC rather than on the venue's local day, so that one
    dataset spanning several exchanges partitions consistently and a reader does
    not have to know each venue's session boundary to find a file. The venue's
    own day is recoverable from the timestamps inside; the partition is only an
    index.
    """
    if moment.tzinfo is None:
        raise PartitionError("a naive timestamp does not name a moment and cannot be partitioned")
    return moment.astimezone(UTC).date()
