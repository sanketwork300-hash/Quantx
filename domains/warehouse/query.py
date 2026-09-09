"""Querying the warehouse.

DuckDB over Hive-partitioned Parquet. The point of the layout is realised here:
a query for one exchange over one week reads the files for that exchange and
that week, because the partition values are columns and DuckDB prunes on them.

Two read paths, and which one ran is **reported** rather than left to be
inferred from the latency:

* ``DIRECT`` — the object store is filesystem-backed, so DuckDB is handed a glob
  and does its own partition pruning, predicate pushdown and column projection.
  Nothing is loaded into this process that the query did not ask for.
* ``MATERIALISED`` — the store is remote, so the partitions are fetched and
  registered as an in-memory table first. Correct, and bounded by memory, which
  is why the query carries a partition limit and says when it hit it.

A query that would have to materialise more than it is allowed to **fails with
that fact** rather than silently truncating. A short answer that looks complete
is the failure this whole module is arranged to avoid.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

import pyarrow as pa
import pyarrow.parquet as pq

from domains.warehouse.enums import DatasetKind, DatasetLayer
from domains.warehouse.partitioning import PartitionKey, glob_for, parse_key
from infrastructure.storage.base import ObjectStore

#: How many partitions a materialised read will pull before refusing. One
#: instrument-day of bars is small; one of quotes is not, and a query spanning a
#: year of both should be told it is too large rather than fill a worker's heap.
DEFAULT_MAX_PARTITIONS = 500


class ReadPath(StrEnum):
    DIRECT = "DIRECT"
    MATERIALISED = "MATERIALISED"


class QueryTooLarge(Exception):
    """The query would materialise more partitions than it is allowed to.

    Carries the numbers so the caller can narrow the range rather than guess.
    """

    def __init__(self, requested: int, allowed: int) -> None:
        super().__init__(
            f"this query spans {requested} partitions and the limit is {allowed}; "
            "narrow the date range or the instrument list"
        )
        self.requested = requested
        self.allowed = allowed


@dataclass(frozen=True, slots=True)
class QueryRequest:
    layer: DatasetLayer
    kind: DatasetKind
    exchange: str | None = None
    instrument_ids: tuple[uuid.UUID, ...] = ()
    start: datetime | None = None
    end: datetime | None = None
    columns: tuple[str, ...] = ()
    limit: int | None = None
    #: Rows the validator flagged are in the partitions. Excluding them is the
    #: caller's decision, not the warehouse's, and the default is to return
    #: everything with its flags visible.
    exclude_flagged: bool = False

    def covers(self, key: PartitionKey) -> bool:
        if self.exchange is not None and key.exchange != self.exchange:
            return False
        if self.instrument_ids and key.instrument_id not in self.instrument_ids:
            return False
        if self.start is not None and key.day < self.start.date():
            return False
        return not (self.end is not None and key.day > self.end.date())


@dataclass(frozen=True, slots=True)
class QueryResult:
    table: pa.Table
    read_path: ReadPath
    partitions_read: int
    rows: int
    #: True when a ``limit`` cut the answer short. Carried so a truncated result
    #: is never mistaken for a complete one.
    truncated: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_provenance(self) -> dict:
        return {
            "read_path": str(self.read_path),
            "partitions_read": self.partitions_read,
            "rows": self.rows,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


def _predicates(request: QueryRequest) -> list[str]:
    clauses: list[str] = []
    if request.start is not None:
        clauses.append(f"exchange_timestamp >= TIMESTAMPTZ '{request.start.isoformat()}'")
    if request.end is not None:
        clauses.append(f"exchange_timestamp <= TIMESTAMPTZ '{request.end.isoformat()}'")
    if request.instrument_ids:
        joined = ", ".join(f"'{value}'" for value in request.instrument_ids)
        clauses.append(f"instrument_id IN ({joined})")
    if request.exclude_flagged:
        clauses.append("len(flags) = 0")
    return clauses


def _projection(request: QueryRequest) -> str:
    if not request.columns:
        return "*"
    # Quoted so a column name can never be read as an expression.
    return ", ".join(f'"{name}"' for name in request.columns)


class WarehouseQuery:
    """Analytical reads over the warehouse."""

    def __init__(
        self,
        store: ObjectStore,
        max_partitions: int = DEFAULT_MAX_PARTITIONS,
    ) -> None:
        self._store = store
        self._max_partitions = max_partitions

    async def run(self, request: QueryRequest) -> QueryResult:
        local_path = getattr(self._store, "local_path", None)
        if callable(local_path):
            return self._direct(request, local_path)
        return await self._materialised(request)

    # ------------------------------------------------------------ direct read
    def _direct(self, request: QueryRequest, local_path) -> QueryResult:
        import duckdb

        pattern = str(
            local_path(
                glob_for(
                    request.layer,
                    request.kind,
                    request.exchange,
                    request.instrument_ids[0] if len(request.instrument_ids) == 1 else None,
                )
            )
        )
        clauses = _predicates(request)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = f" LIMIT {int(request.limit)}" if request.limit else ""

        connection = duckdb.connect()
        try:
            source = f"read_parquet('{pattern}', hive_partitioning = true, union_by_name = true)"
            try:
                table = connection.execute(
                    f"SELECT {_projection(request)} FROM {source}{where}"
                    f" ORDER BY exchange_timestamp{limit}"
                ).to_arrow_table()
            except duckdb.IOException:
                # No file matched the glob. An empty warehouse is an empty
                # answer, not an error.
                return QueryResult(
                    table=pa.table({}), read_path=ReadPath.DIRECT, partitions_read=0, rows=0
                )
            # Counted *with the same predicates*, so this is the number of
            # partitions that actually contributed rows rather than the number
            # the glob matched. Reporting the glob count would overstate how
            # much was read and hide whether pruning worked at all.
            counted = connection.execute(
                f"SELECT count(DISTINCT filename) FROM read_parquet('{pattern}', "
                f"hive_partitioning = true, union_by_name = true, filename = true){where}"
            ).fetchone()
            files = counted[0] if counted else 0
        finally:
            connection.close()

        return QueryResult(
            table=table,
            read_path=ReadPath.DIRECT,
            partitions_read=int(files or 0),
            rows=table.num_rows,
            truncated=bool(request.limit) and table.num_rows >= int(request.limit),
        )

    # ------------------------------------------------- materialised read
    async def _materialised(self, request: QueryRequest) -> QueryResult:
        import duckdb

        keys: list[PartitionKey] = []
        prefix = f"warehouse/{request.layer}/{request.kind}/"
        async for raw in self._store.iter_keys(prefix):
            if not raw.endswith("part.parquet"):
                continue
            try:
                key = parse_key(raw)
            except Exception:
                continue
            if request.covers(key):
                keys.append(key)

        if len(keys) > self._max_partitions:
            raise QueryTooLarge(len(keys), self._max_partitions)
        if not keys:
            return QueryResult(
                table=pa.table({}), read_path=ReadPath.MATERIALISED, partitions_read=0, rows=0
            )

        tables = [
            pq.read_table(io.BytesIO(await self._store.get(key.object_key)))
            for key in sorted(keys, key=lambda item: (item.day, str(item.instrument_id)))
        ]
        combined = pa.concat_tables(tables, promote_options="default")

        clauses = _predicates(request)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = f" LIMIT {int(request.limit)}" if request.limit else ""

        connection = duckdb.connect()
        try:
            connection.register("partitions", combined)
            table = connection.execute(
                f"SELECT {_projection(request)} FROM partitions{where}"
                f" ORDER BY exchange_timestamp{limit}"
            ).to_arrow_table()
        finally:
            connection.close()

        return QueryResult(
            table=table,
            read_path=ReadPath.MATERIALISED,
            partitions_read=len(keys),
            rows=table.num_rows,
            truncated=bool(request.limit) and table.num_rows >= int(request.limit),
            warnings=(
                "the object store is not filesystem-backed, so partitions were fetched "
                "and filtered in memory rather than pushed down to the reader",
            ),
        )


def coverage_days(keys: Sequence[PartitionKey]) -> tuple[date | None, date | None]:
    """First and last day covered by a set of partitions."""
    if not keys:
        return None, None
    days = sorted(key.day for key in keys)
    return days[0], days[-1]
