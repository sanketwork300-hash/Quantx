"""Arrow schemas for the warehouse.

Two conventions carried over from the microstructure store, for the same
reasons. **Prices are decimals, not floats**: a stored observation is a fact, and
``decimal128(38, 12)`` round-trips the tick prices a venue published without the
platform quietly re-rounding them. **Timestamps are microsecond UTC**, because a
warehouse whose files disagree about their time base is a warehouse whose joins
are wrong.

Every schema carries a ``flags`` column. That is the platform's "suspicious data
is flagged and kept" rule made physical: a row the validator was unhappy about
reaches the partition *with its flags attached*, so a reader can exclude it,
weight it down, or look at it — rather than finding a clean series with a hole
where the awkward row used to be.
"""

from __future__ import annotations

from decimal import Decimal

import pyarrow as pa

from domains.warehouse.enums import DatasetKind

STORAGE_VERSION = "warehouse-parquet@1.0.0"

#: 38 digits with 12 after the point covers every venue tick size the platform
#: has met, and crypto quantities with eight decimals, without truncation.
PRICE_TYPE = pa.decimal128(38, 12)
QUANTIZE = Decimal(1).scaleb(-12)

TIMESTAMP = pa.timestamp("us", tz="UTC")

#: Columns every kind shares, in the same positions, so a reader that only wants
#: "when and what instrument" does not need to know the kind.
_COMMON = [
    pa.field("instrument_id", pa.string(), nullable=False),
    pa.field("exchange_timestamp", TIMESTAMP, nullable=False),
    pa.field("flags", pa.list_(pa.string()), nullable=False),
]

BARS_SCHEMA = pa.schema(
    [
        *_COMMON,
        pa.field("interval", pa.string(), nullable=False),
        pa.field("end_timestamp", TIMESTAMP),
        pa.field("open", PRICE_TYPE, nullable=False),
        pa.field("high", PRICE_TYPE, nullable=False),
        pa.field("low", PRICE_TYPE, nullable=False),
        pa.field("close", PRICE_TYPE, nullable=False),
        pa.field("volume", PRICE_TYPE, nullable=False),
        #: Only where the venue publishes it. A VWAP the platform reconstructed
        #: is a derived estimate and does not belong in an observation column.
        pa.field("vwap", PRICE_TYPE),
        pa.field("trade_count", pa.int64()),
    ]
)

TRADES_SCHEMA = pa.schema(
    [
        *_COMMON,
        pa.field("price", PRICE_TYPE, nullable=False),
        pa.field("quantity", PRICE_TYPE, nullable=False),
        #: Nullable because most tapes do not publish it. Inferring the
        #: aggressor from a tick rule is a model, not an observation.
        pa.field("aggressor_side", pa.string()),
        pa.field("trade_id", pa.string()),
    ]
)

QUOTES_SCHEMA = pa.schema(
    [
        *_COMMON,
        pa.field("receive_timestamp", TIMESTAMP),
        pa.field("bid_price", PRICE_TYPE),
        pa.field("bid_size", PRICE_TYPE),
        pa.field("ask_price", PRICE_TYPE),
        pa.field("ask_size", PRICE_TYPE),
        pa.field("last_price", PRICE_TYPE),
        pa.field("volume", PRICE_TYPE),
        pa.field("open_interest", PRICE_TYPE),
        pa.field("sequence_number", pa.int64()),
    ]
)

SCHEMAS: dict[DatasetKind, pa.Schema] = {
    DatasetKind.BARS: BARS_SCHEMA,
    DatasetKind.TRADES: TRADES_SCHEMA,
    DatasetKind.QUOTES: QUOTES_SCHEMA,
}


def schema_for(kind: DatasetKind) -> pa.Schema:
    return SCHEMAS[kind]


def quantize(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(QUANTIZE)
