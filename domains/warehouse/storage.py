"""Writing and reading warehouse partitions.

One file per instrument per day per kind, Hive-partitioned so a reader prunes on
the path. The parquet footer repeats the partition values plus the source and
the code commit, so a file found on its own is still identifiable — the same
property the microstructure store gives its files.

Writes are **whole-partition replacements**, not appends. A day's data for one
instrument is either there or it is not, which makes a re-ingestion idempotent
and removes the question of what a half-appended partition means.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from domains.warehouse.enums import DatasetKind, DatasetLayer
from domains.warehouse.partitioning import PartitionKey, day_of
from domains.warehouse.schemas import STORAGE_VERSION, quantize, schema_for
from domains.warehouse.validation import ValidatedRow
from infrastructure.storage.base import ObjectStore


def _metadata(
    key: PartitionKey, source: str, code_commit: str, rows: int, written_at: datetime | None
) -> dict:
    """Footer metadata. ``written_at`` is injectable so a fixture regenerates
    byte-identically rather than diffing on a wall clock."""
    return {
        b"qip.storage_version": STORAGE_VERSION.encode(),
        b"qip.layer": str(key.layer).encode(),
        b"qip.kind": str(key.kind).encode(),
        b"qip.exchange": key.exchange.encode(),
        b"qip.day": key.day.isoformat().encode(),
        b"qip.instrument_id": str(key.instrument_id).encode(),
        b"qip.source": source.encode(),
        b"qip.code_commit": code_commit.encode(),
        b"qip.rows": str(rows).encode(),
        b"qip.written_at": (written_at or datetime.now(UTC)).isoformat().encode(),
    }


def _column(rows: Sequence[ValidatedRow], name: str, arrow_type) -> pa.Array:
    values = [row.values.get(name) for row in rows]
    if arrow_type.equals(pa.decimal128(38, 12)):
        return pa.array(
            [quantize(value) if isinstance(value, Decimal) else None for value in values],
            arrow_type,
        )
    if pa.types.is_timestamp(arrow_type):
        return pa.array(values, arrow_type)
    if pa.types.is_string(arrow_type):
        return pa.array([None if value is None else str(value) for value in values], arrow_type)
    return pa.array(values, arrow_type)


def rows_to_table(rows: Sequence[ValidatedRow], kind: DatasetKind) -> pa.Table:
    """Build the Arrow table for one partition.

    ``flags`` come from the validator rather than from the row's own payload:
    they are the platform's judgement about the row, not the source's claim
    about it, and keeping the two apart is the same separation of observation
    from estimate that runs through everything else here.
    """
    schema = schema_for(kind)
    columns: dict[str, pa.Array] = {}
    for field in schema:
        if field.name == "instrument_id":
            columns[field.name] = pa.array([str(row.instrument_id) for row in rows], pa.string())
        elif field.name == "exchange_timestamp":
            columns[field.name] = pa.array([row.exchange_timestamp for row in rows], field.type)
        elif field.name == "flags":
            columns[field.name] = pa.array([list(row.flags) for row in rows], field.type)
        else:
            columns[field.name] = _column(rows, field.name, field.type)
    return pa.table(columns, schema=schema)


def table_to_parquet(
    table: pa.Table,
    key: PartitionKey,
    source: str,
    code_commit: str,
    written_at: datetime | None = None,
) -> bytes:
    table = table.replace_schema_metadata(
        _metadata(key, source, code_commit, table.num_rows, written_at)
    )
    sink = io.BytesIO()
    # zstd: these files are read far more than written and their columns are
    # highly repetitive, so the ratio is worth the write cost.
    pq.write_table(table, sink, compression="zstd", version="2.6")
    return sink.getvalue()


def partition_rows(
    rows: Sequence[ValidatedRow],
    layer: DatasetLayer,
    kind: DatasetKind,
    exchange: str,
) -> dict[PartitionKey, list[ValidatedRow]]:
    """Split validated rows into the partitions they belong to."""
    buckets: dict[PartitionKey, list[ValidatedRow]] = {}
    for row in rows:
        key = PartitionKey(
            layer=layer,
            kind=kind,
            exchange=exchange,
            day=day_of(row.exchange_timestamp),
            instrument_id=row.instrument_id,
        )
        buckets.setdefault(key, []).append(row)
    return buckets


class WarehouseStore:
    """Object-store round-trips for warehouse partitions."""

    def __init__(self, store: ObjectStore, code_commit: str) -> None:
        self._store = store
        self._code_commit = code_commit

    async def write_partition(
        self,
        key: PartitionKey,
        rows: Sequence[ValidatedRow],
        source: str,
        written_at: datetime | None = None,
    ) -> tuple[str, int, int]:
        """Replace one partition. Returns ``(object_key, rows, bytes)``."""
        table = rows_to_table(rows, key.kind)
        payload = table_to_parquet(table, key, source, self._code_commit, written_at)
        await self._store.put(
            key.object_key, payload, content_type="application/vnd.apache.parquet"
        )
        return key.object_key, table.num_rows, len(payload)

    async def read_partition(self, key: PartitionKey, columns: list[str] | None = None) -> pa.Table:
        data = await self._store.get(key.object_key)
        return pq.read_table(io.BytesIO(data), columns=columns)

    async def partition_metadata(self, key: PartitionKey) -> dict:
        """The footer metadata alone, so a file can be identified without
        reading its rows."""
        data = await self._store.get(key.object_key)
        raw = pq.read_schema(io.BytesIO(data)).metadata or {}
        return {
            name.decode().removeprefix("qip."): value.decode()
            for name, value in raw.items()
            if name.startswith(b"qip.")
        }

    async def exists(self, key: PartitionKey) -> bool:
        return await self._store.exists(key.object_key)

    async def delete_partition(self, key: PartitionKey) -> None:
        await self._store.delete(key.object_key)

    async def iter_partition_keys(self, prefix: str):
        async for key in self._store.iter_keys(prefix):
            if key.endswith("part.parquet"):
                yield key
