"""Parquet storage for L2 snapshots and event tapes.

These are the two datasets in the platform that grow with market activity
rather than with user activity, which is exactly the line ``docs/database.md``
draws: an L2 history does not go in PostgreSQL. One trading day of depth for one
liquid contract is millions of rows, and the access pattern is analytical —
read three columns over a time range — which is what a columnar file is for and
what a relational row store is not.

**Prices and quantities are stored as decimals, not floats.** A stored
observation is a fact, and ``decimal128(38, 12)`` round-trips the tick prices a
venue published without the platform quietly re-rounding them. Floats appear
only when a number crosses into :mod:`quant.microstructure`, which is the same
boundary the rest of the platform draws.

The parquet file's key-value metadata carries the instrument, the source and
the code commit that wrote it, so a file found on its own is still identifiable.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from domains.market_data.enums import BookEventType, BookSide
from domains.market_data.models import BookEvent, OrderBookLevel, OrderBookSnapshot
from infrastructure.storage.base import ObjectStore

STORAGE_VERSION = "microstructure-parquet@1.0.0"

#: 38 digits with 12 after the point covers every venue tick size the platform
#: has met, and crypto quantities with eight decimals, without truncation.
PRICE_TYPE = pa.decimal128(38, 12)
_QUANTIZE = Decimal(1).scaleb(-12)

SNAPSHOT_SCHEMA = pa.schema(
    [
        pa.field("exchange_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("receive_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("sequence_number", pa.int64()),
        pa.field("bid_prices", pa.list_(PRICE_TYPE), nullable=False),
        pa.field("bid_sizes", pa.list_(PRICE_TYPE), nullable=False),
        pa.field("bid_orders", pa.list_(pa.int32())),
        pa.field("ask_prices", pa.list_(PRICE_TYPE), nullable=False),
        pa.field("ask_sizes", pa.list_(PRICE_TYPE), nullable=False),
        pa.field("ask_orders", pa.list_(pa.int32())),
    ]
)

EVENT_SCHEMA = pa.schema(
    [
        pa.field("exchange_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("receive_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("sequence_number", pa.int64()),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("side", pa.string()),
        pa.field("price", PRICE_TYPE),
        pa.field("quantity", PRICE_TYPE),
        pa.field("order_id", pa.string()),
    ]
)


def _quantize(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_QUANTIZE)


def _metadata(instrument_id: uuid.UUID, source: str, code_commit: str, rows: int) -> dict:
    return {
        b"qip.storage_version": STORAGE_VERSION.encode(),
        b"qip.instrument_id": str(instrument_id).encode(),
        b"qip.source": source.encode(),
        b"qip.code_commit": code_commit.encode(),
        b"qip.rows": str(rows).encode(),
        b"qip.written_at": datetime.now(UTC).isoformat().encode(),
    }


def _write(table: pa.Table, metadata: dict) -> bytes:
    table = table.replace_schema_metadata(metadata)
    sink = io.BytesIO()
    # zstd because these files are read far more often than written and the
    # columns are highly repetitive; the ratio is worth the write cost.
    pq.write_table(table, sink, compression="zstd", version="2.6")
    return sink.getvalue()


# ------------------------------------------------------------- serialisation
def snapshots_to_parquet(
    snapshots: tuple[OrderBookSnapshot, ...],
    instrument_id: uuid.UUID,
    source: str,
    code_commit: str,
) -> bytes:
    """One row per snapshot, levels as list columns.

    List columns rather than the ``bid_px_1 ... bid_px_20`` wide layout users
    upload, because depth genuinely varies snapshot to snapshot and a fixed
    width would have to pad — and a padded level is indistinguishable from a
    level quoted at zero, which is a real thing on some venues.
    """
    table = pa.table(
        {
            "exchange_timestamp": pa.array(
                [snapshot.exchange_timestamp for snapshot in snapshots],
                pa.timestamp("us", tz="UTC"),
            ),
            "receive_timestamp": pa.array(
                [snapshot.receive_timestamp for snapshot in snapshots],
                pa.timestamp("us", tz="UTC"),
            ),
            "sequence_number": pa.array(
                [snapshot.sequence_number for snapshot in snapshots], pa.int64()
            ),
            "bid_prices": pa.array(
                [[_quantize(level.price) for level in s.bids] for s in snapshots],
                pa.list_(PRICE_TYPE),
            ),
            "bid_sizes": pa.array(
                [[_quantize(level.quantity) for level in s.bids] for s in snapshots],
                pa.list_(PRICE_TYPE),
            ),
            "bid_orders": pa.array(
                [[level.order_count for level in s.bids] for s in snapshots],
                pa.list_(pa.int32()),
            ),
            "ask_prices": pa.array(
                [[_quantize(level.price) for level in s.asks] for s in snapshots],
                pa.list_(PRICE_TYPE),
            ),
            "ask_sizes": pa.array(
                [[_quantize(level.quantity) for level in s.asks] for s in snapshots],
                pa.list_(PRICE_TYPE),
            ),
            "ask_orders": pa.array(
                [[level.order_count for level in s.asks] for s in snapshots],
                pa.list_(pa.int32()),
            ),
        },
        schema=SNAPSHOT_SCHEMA,
    )
    return _write(table, _metadata(instrument_id, source, code_commit, len(snapshots)))


def events_to_parquet(
    events: tuple[BookEvent, ...],
    instrument_id: uuid.UUID,
    source: str,
    code_commit: str,
) -> bytes:
    table = pa.table(
        {
            "exchange_timestamp": pa.array(
                [event.exchange_timestamp for event in events],
                pa.timestamp("us", tz="UTC"),
            ),
            "receive_timestamp": pa.array(
                [event.receive_timestamp for event in events],
                pa.timestamp("us", tz="UTC"),
            ),
            "sequence_number": pa.array(
                [event.sequence_number for event in events], pa.int64()
            ),
            "event_type": pa.array([str(event.event_type) for event in events], pa.string()),
            "side": pa.array(
                [str(event.side) if event.side else None for event in events], pa.string()
            ),
            "price": pa.array([_quantize(event.price) for event in events], PRICE_TYPE),
            "quantity": pa.array([_quantize(event.quantity) for event in events], PRICE_TYPE),
            "order_id": pa.array([event.order_id for event in events], pa.string()),
        },
        schema=EVENT_SCHEMA,
    )
    return _write(table, _metadata(instrument_id, source, code_commit, len(events)))


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def snapshots_from_parquet(
    data: bytes, instrument_id: uuid.UUID, source: str, columns: list[str] | None = None
) -> tuple[OrderBookSnapshot, ...]:
    """Read snapshots back. ``columns`` prunes the read for cheap scans."""
    table = pq.read_table(io.BytesIO(data), columns=columns)
    rows = table.to_pylist()
    snapshots: list[OrderBookSnapshot] = []
    for row in rows:
        snapshots.append(
            OrderBookSnapshot(
                instrument_id=instrument_id,
                exchange_timestamp=_as_utc(row["exchange_timestamp"]),
                receive_timestamp=_as_utc(row["receive_timestamp"])
                or _as_utc(row["exchange_timestamp"]),
                bids=_levels(row["bid_prices"], row["bid_sizes"], row.get("bid_orders")),
                asks=_levels(row["ask_prices"], row["ask_sizes"], row.get("ask_orders")),
                source=source,
                sequence_number=row.get("sequence_number"),
            )
        )
    return tuple(snapshots)


def _levels(prices, sizes, orders) -> tuple[OrderBookLevel, ...]:
    orders = orders or [None] * len(prices)
    return tuple(
        OrderBookLevel(price=price, quantity=size, order_count=count)
        for price, size, count in zip(prices, sizes, orders, strict=True)
    )


def events_from_parquet(
    data: bytes, instrument_id: uuid.UUID, source: str, columns: list[str] | None = None
) -> tuple[BookEvent, ...]:
    table = pq.read_table(io.BytesIO(data), columns=columns)
    events: list[BookEvent] = []
    for row in table.to_pylist():
        events.append(
            BookEvent(
                instrument_id=instrument_id,
                exchange_timestamp=_as_utc(row["exchange_timestamp"]),
                event_type=BookEventType(row["event_type"]),
                source=source,
                side=BookSide(row["side"]) if row.get("side") else None,
                price=row.get("price"),
                quantity=row.get("quantity"),
                sequence_number=row.get("sequence_number"),
                order_id=row.get("order_id"),
                receive_timestamp=_as_utc(row.get("receive_timestamp")),
            )
        )
    return tuple(events)


# ---------------------------------------------------------------- the store
class MicrostructureStore:
    """Object-store keys and round-trips for one user's L2 datasets.

    Keys are server-generated from the dataset id, so nothing a user supplies
    reaches a storage path — the same rule as the upload store.
    """

    def __init__(self, store: ObjectStore, code_commit: str) -> None:
        self._store = store
        self._code_commit = code_commit

    @staticmethod
    def snapshot_key(user_id: uuid.UUID, dataset_id: uuid.UUID) -> str:
        return f"microstructure/{user_id}/{dataset_id}/snapshots.parquet"

    @staticmethod
    def event_key(user_id: uuid.UUID, dataset_id: uuid.UUID) -> str:
        return f"microstructure/{user_id}/{dataset_id}/events.parquet"

    @staticmethod
    def rejection_key(user_id: uuid.UUID, dataset_id: uuid.UUID) -> str:
        return f"microstructure/{user_id}/{dataset_id}/rejections.json"

    @staticmethod
    def series_key(user_id: uuid.UUID, report_id: uuid.UUID) -> str:
        return f"microstructure/{user_id}/reports/{report_id}/series.parquet"

    async def put_snapshots(
        self,
        user_id: uuid.UUID,
        dataset_id: uuid.UUID,
        instrument_id: uuid.UUID,
        snapshots: tuple[OrderBookSnapshot, ...],
        source: str,
    ) -> str:
        key = self.snapshot_key(user_id, dataset_id)
        await self._store.put(
            key,
            snapshots_to_parquet(snapshots, instrument_id, source, self._code_commit),
            content_type="application/vnd.apache.parquet",
        )
        return key

    async def put_events(
        self,
        user_id: uuid.UUID,
        dataset_id: uuid.UUID,
        instrument_id: uuid.UUID,
        events: tuple[BookEvent, ...],
        source: str,
    ) -> str:
        key = self.event_key(user_id, dataset_id)
        await self._store.put(
            key,
            events_to_parquet(events, instrument_id, source, self._code_commit),
            content_type="application/vnd.apache.parquet",
        )
        return key

    async def put_rejections(
        self, user_id: uuid.UUID, dataset_id: uuid.UUID, payload: dict
    ) -> str:
        """The complete rejection list, which is unbounded and so not a column.

        Counts by reason live on the dataset row; the per-row detail lives here,
        so "every rejected row names its source row number and reason" holds for
        every row rather than for the first page of them.
        """
        key = self.rejection_key(user_id, dataset_id)
        await self._store.put(
            key,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )
        return key

    async def get_snapshots(
        self, key: str, instrument_id: uuid.UUID, source: str
    ) -> tuple[OrderBookSnapshot, ...]:
        return snapshots_from_parquet(await self._store.get(key), instrument_id, source)

    async def get_events(
        self, key: str, instrument_id: uuid.UUID, source: str
    ) -> tuple[BookEvent, ...]:
        return events_from_parquet(await self._store.get(key), instrument_id, source)

    async def get_rejections(self, key: str) -> dict:
        return json.loads((await self._store.get(key)).decode("utf-8"))

    async def put_series(self, user_id: uuid.UUID, report_id: uuid.UUID, table: pa.Table) -> str:
        key = self.series_key(user_id, report_id)
        sink = io.BytesIO()
        pq.write_table(table, sink, compression="zstd", version="2.6")
        await self._store.put(
            key, sink.getvalue(), content_type="application/vnd.apache.parquet"
        )
        return key

    async def get_series(self, key: str) -> list[dict]:
        return pq.read_table(io.BytesIO(await self._store.get(key))).to_pylist()
