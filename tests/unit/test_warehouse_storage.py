"""Where a row goes, and whether a query finds it again.

The partition layout is the load-bearing part of this phase: it is what makes
"queryable" a real property rather than a wrapper around a full scan. So the
tests are about the two things that would quietly break it — a path that does
not round-trip, and a query that reads more than it claims to.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from domains.warehouse.enums import CorporateActionTreatment, DatasetKind, DatasetLayer
from domains.warehouse.partitioning import (
    PartitionError,
    PartitionKey,
    day_of,
    glob_for,
    parse_key,
)
from domains.warehouse.query import QueryRequest, ReadPath, WarehouseQuery
from domains.warehouse.storage import WarehouseStore, partition_rows, rows_to_table
from domains.warehouse.validation import validate_bars
from infrastructure.storage.local import LocalObjectStore

INSTRUMENT = uuid.UUID(int=11)
OTHER = uuid.UUID(int=12)
START = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def bars(count: int, instrument: uuid.UUID = INSTRUMENT, start: float = 100.0) -> list[dict]:
    rows = []
    for day in range(count):
        price = Decimal(f"{start + day:.2f}")
        rows.append(
            {
                "instrument_id": instrument,
                "exchange_timestamp": START + timedelta(days=day),
                "interval": "1d",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": Decimal("1000"),
            }
        )
    return rows


@pytest.fixture
def store() -> LocalObjectStore:
    return LocalObjectStore(Path(tempfile.mkdtemp()))


class TestPartitionKeys:
    def test_a_key_round_trips_through_its_path(self):
        """A file found on its own has to be identifiable from where it sits."""
        key = PartitionKey(
            DatasetLayer.NORMALIZED, DatasetKind.BARS, "NSE", START.date(), INSTRUMENT
        )
        assert parse_key(key.object_key) == key

    def test_the_path_is_hive_partitioned_so_a_reader_can_prune_it(self):
        key = PartitionKey(DatasetLayer.RAW, DatasetKind.TRADES, "NSE", START.date(), INSTRUMENT)
        assert "exchange=NSE" in key.object_key
        assert "year=2026/month=03/day=02" in key.object_key
        assert f"instrument_id={INSTRUMENT}" in key.object_key

    def test_a_path_that_is_not_a_partition_is_refused(self):
        for candidate in ("nope", "warehouse/x/y/z", "warehouse/raw/bars/part.parquet"):
            with pytest.raises(PartitionError):
                parse_key(candidate)

    def test_an_exchange_that_would_escape_the_layout_is_refused(self):
        with pytest.raises(PartitionError):
            PartitionKey(DatasetLayer.RAW, DatasetKind.BARS, "../../etc", START.date(), INSTRUMENT)

    def test_the_day_is_utc_whatever_zone_the_timestamp_carried(self):
        """One dataset spanning several exchanges has to partition consistently,
        and a reader must not need each venue's session boundary to find a file.
        """
        ist = timezone(timedelta(hours=5, minutes=30))
        assert day_of(datetime(2026, 9, 10, 2, 0, tzinfo=ist)).isoformat() == "2026-09-09"

    def test_a_naive_timestamp_cannot_be_partitioned(self):
        with pytest.raises(PartitionError):
            day_of(datetime(2026, 9, 10, 2, 0))

    def test_a_glob_narrows_to_what_the_filters_allow(self):
        pattern = glob_for(DatasetLayer.NORMALIZED, DatasetKind.BARS, "NSE", INSTRUMENT)
        assert "exchange=NSE" in pattern
        assert f"instrument_id={INSTRUMENT}" in pattern
        assert pattern.endswith("part.parquet")


class TestWritingPartitions:
    async def test_one_file_per_instrument_per_day(self, store):
        report = validate_bars(bars(3), CorporateActionTreatment.UNADJUSTED)
        buckets = partition_rows(report.rows, DatasetLayer.NORMALIZED, DatasetKind.BARS, "NSE")
        assert len(buckets) == 3

        warehouse = WarehouseStore(store, "test-commit")
        for key, rows in buckets.items():
            _key, count, size = await warehouse.write_partition(key, rows, source="vendor")
            assert count == 1
            assert size > 0

    async def test_prices_survive_as_decimals(self, store):
        """A stored observation is a fact. A float round trip would re-round the
        tick prices the venue published."""
        rows = validate_bars(bars(1), CorporateActionTreatment.UNADJUSTED).rows
        key = next(iter(partition_rows(rows, DatasetLayer.RAW, DatasetKind.BARS, "NSE")))
        warehouse = WarehouseStore(store, "c")
        await warehouse.write_partition(key, rows, source="vendor")

        table = await warehouse.read_partition(key)
        assert table.column("close").to_pylist()[0] == Decimal("100.000000000000")

    async def test_flags_travel_into_the_partition(self, store):
        """The platform's 'flagged and kept' rule, made physical: a row the
        validator was unhappy about arrives with its flags attached rather than
        leaving a hole where it used to be."""
        rows = bars(60)
        rows[30]["close"] = Decimal("4000")
        rows[30]["high"] = Decimal("4001")
        report = validate_bars(rows, CorporateActionTreatment.UNADJUSTED)
        flagged = [row for row in report.rows if row.flags]
        assert flagged

        table = rows_to_table(flagged, DatasetKind.BARS)
        assert table.column("flags").to_pylist()[0]

    async def test_the_footer_identifies_the_file_on_its_own(self, store):
        rows = validate_bars(bars(1), CorporateActionTreatment.UNADJUSTED).rows
        key = next(iter(partition_rows(rows, DatasetLayer.RAW, DatasetKind.BARS, "NSE")))
        warehouse = WarehouseStore(store, "abc123")
        await warehouse.write_partition(key, rows, source="vendor")

        metadata = await warehouse.partition_metadata(key)
        assert metadata["exchange"] == "NSE"
        assert metadata["kind"] == "bars"
        assert metadata["source"] == "vendor"
        assert metadata["code_commit"] == "abc123"
        assert metadata["rows"] == "1"

    async def test_rewriting_a_day_replaces_rather_than_appends(self, store):
        """Which is what makes a re-ingestion idempotent, and removes the
        question of what a half-appended partition means."""
        warehouse = WarehouseStore(store, "c")
        rows = validate_bars(bars(1), CorporateActionTreatment.UNADJUSTED).rows
        key = next(iter(partition_rows(rows, DatasetLayer.RAW, DatasetKind.BARS, "NSE")))

        await warehouse.write_partition(key, rows, source="vendor")
        await warehouse.write_partition(key, rows, source="vendor")
        assert (await warehouse.read_partition(key)).num_rows == 1


class TestQuerying:
    async def _load(self, store, rows):
        warehouse = WarehouseStore(store, "c")
        report = validate_bars(rows, CorporateActionTreatment.UNADJUSTED)
        for key, part in partition_rows(
            report.rows, DatasetLayer.NORMALIZED, DatasetKind.BARS, "NSE"
        ).items():
            await warehouse.write_partition(key, part, source="vendor")
        return report

    async def test_everything_written_comes_back(self, store):
        await self._load(store, bars(5))
        result = await WarehouseQuery(store).run(
            QueryRequest(DatasetLayer.NORMALIZED, DatasetKind.BARS, exchange="NSE")
        )
        assert result.rows == 5
        assert result.read_path is ReadPath.DIRECT

    async def test_a_date_range_reads_only_the_days_it_needs(self, store):
        """The point of the layout. ``partitions_read`` counts files that
        actually contributed rows, so a broken prune shows up here."""
        await self._load(store, bars(10))
        result = await WarehouseQuery(store).run(
            QueryRequest(
                DatasetLayer.NORMALIZED,
                DatasetKind.BARS,
                start=START + timedelta(days=3),
                end=START + timedelta(days=5),
            )
        )
        assert result.rows == 3
        assert result.partitions_read == 3

    async def test_one_instrument_can_be_read_out_of_many(self, store):
        await self._load(store, [*bars(3), *bars(3, instrument=OTHER, start=500.0)])
        result = await WarehouseQuery(store).run(
            QueryRequest(DatasetLayer.NORMALIZED, DatasetKind.BARS, instrument_ids=(OTHER,))
        )
        assert result.rows == 3
        assert {row for row in result.table.column("instrument_id").to_pylist()} == {str(OTHER)}

    async def test_columns_can_be_projected(self, store):
        await self._load(store, bars(3))
        result = await WarehouseQuery(store).run(
            QueryRequest(
                DatasetLayer.NORMALIZED,
                DatasetKind.BARS,
                columns=("exchange_timestamp", "close"),
            )
        )
        assert result.table.column_names == ["exchange_timestamp", "close"]

    async def test_flagged_rows_are_returned_unless_the_caller_excludes_them(self, store):
        """The default is to return everything with its flags visible: whether
        to use an outlier is the caller's judgement, not the warehouse's."""
        rows = bars(60)
        rows[30]["close"] = Decimal("4000")
        rows[30]["high"] = Decimal("4001")
        await self._load(store, rows)

        everything = await WarehouseQuery(store).run(
            QueryRequest(DatasetLayer.NORMALIZED, DatasetKind.BARS)
        )
        clean = await WarehouseQuery(store).run(
            QueryRequest(DatasetLayer.NORMALIZED, DatasetKind.BARS, exclude_flagged=True)
        )
        assert everything.rows == 60
        assert clean.rows < everything.rows

    async def test_a_limit_says_that_it_truncated(self, store):
        """So a short answer is never mistaken for a complete one."""
        await self._load(store, bars(10))
        result = await WarehouseQuery(store).run(
            QueryRequest(DatasetLayer.NORMALIZED, DatasetKind.BARS, limit=4)
        )
        assert result.rows == 4
        assert result.truncated is True

    async def test_an_empty_warehouse_is_an_empty_answer_not_an_error(self, store):
        result = await WarehouseQuery(store).run(
            QueryRequest(DatasetLayer.NORMALIZED, DatasetKind.BARS)
        )
        assert result.rows == 0
        assert result.partitions_read == 0
