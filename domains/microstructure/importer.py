"""Reading a user's L2 data: depth snapshots and event tapes.

Two shapes arrive in practice and both are supported.

**Wide CSV** is what a venue export or a capture tool writes: one row per
instant, with the levels spread across the row as ``BID_PX_1, BID_SZ_1,
BID_PX_2, ...``. The indexed-column layout defeats the platform's ordinary
one-column-per-field mapping, so this module detects the level columns itself —
and, as everywhere else, the detection is a *suggestion* shown in a mandatory
preview. A book whose price and size columns were read the wrong way round
produces analytics that are wrong in every number and look entirely normal.

**Parquet** is what a serious capture pipeline writes and what this platform
stores. A parquet upload in the canonical schema is read directly.

The import rules are the platform's usual ones. A bad row never aborts the
file; it is captured with its 1-based source row number and a closed-vocabulary
reason, and the counts conserve. Nothing is repaired: a book whose levels are
not ordered best-first is refused rather than sorted, because sorting would
silently rescue a file whose price and size columns are transposed.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from domains.market_data.enums import BookEventType, BookSide
from domains.market_data.ingestion.column_mapping import (
    ColumnMapping,
    FieldSpec,
    FieldType,
    infer_mapping,
    normalize_header,
)
from domains.market_data.ingestion.parser import (
    NULL_TOKENS,
    RowParseError,
    TabularParser,
    parse_datetime,
    parse_decimal,
)
from domains.market_data.models import BookEvent, OrderBookLevel, OrderBookSnapshot
from domains.microstructure.models import (
    EventBatch,
    EventRejection,
    RejectedRow,
    SnapshotBatch,
    SnapshotRejection,
)

#: Parquet's magic number, at the head and the foot of every valid file.
PARQUET_MAGIC = b"PAR1"


# ------------------------------------------------------- wide-CSV detection
_BID_TOKENS = frozenset({"bid", "b", "buy", "bids"})
_ASK_TOKENS = frozenset({"ask", "a", "sell", "offer", "asks", "off"})
_PRICE_TOKENS = frozenset({"px", "price", "p", "prc", "rate", ""})
_SIZE_TOKENS = frozenset({"sz", "size", "qty", "quantity", "q", "vol", "volume", "amt", "amount"})
_ORDER_TOKENS = frozenset({"orders", "ordercount", "count", "num", "n", "norders", "ct"})

_SIDE_FIELD_INDEX = re.compile(
    r"^(bid|ask|buy|sell|offer|bids|asks|off|b|a)"
    r"(px|price|prc|rate|p|sz|size|qty|quantity|vol|volume|amount|amt|q"
    r"|orders|ordercount|count|norders|num|ct|n)?"
    r"(\d+)$"
)
_SIDE_INDEX_FIELD = re.compile(
    r"^(bid|ask|buy|sell|offer|bids|asks|off|b|a)"
    r"(\d+)"
    r"(px|price|prc|rate|p|sz|size|qty|quantity|vol|volume|amount|amt|q"
    r"|orders|ordercount|count|norders|num|ct|n)$"
)

_TIMESTAMP_ALIASES = (
    "timestamp",
    "time",
    "datetime",
    "exchangetimestamp",
    "exchangetime",
    "ts",
    "eventtime",
    "snapshottime",
    "recvtime",
)
_RECEIVE_ALIASES = ("receivetimestamp", "receivetime", "receivedat", "localtime", "capturetime")
_SEQUENCE_ALIASES = ("sequence", "seq", "seqno", "sequencenumber", "updateid", "messageid")


def _classify(token: str | None) -> str | None:
    token = token or ""
    if token in _PRICE_TOKENS:
        return "price"
    if token in _SIZE_TOKENS:
        return "size"
    if token in _ORDER_TOKENS:
        return "orders"
    return None


@dataclass(frozen=True, slots=True)
class LevelColumns:
    """The wide-CSV columns recognised for one book, level by level."""

    timestamp: str | None
    receive_timestamp: str | None
    sequence: str | None
    #: ``side -> level index -> {"price": header, "size": header, ...}``
    levels: dict[str, dict[int, dict[str, str]]]
    unrecognised: tuple[str, ...]

    @property
    def depth(self) -> int:
        """Levels usable on both sides: each needs a price *and* a size."""
        usable = {
            side: sorted(
                index
                for index, columns in indices.items()
                if "price" in columns and "size" in columns
            )
            for side, indices in self.levels.items()
        }
        return min(len(usable.get("bid", [])), len(usable.get("ask", [])))

    def ordered(self, side: str) -> list[tuple[int, dict[str, str]]]:
        return sorted(
            (
                (index, columns)
                for index, columns in self.levels.get(side, {}).items()
                if "price" in columns and "size" in columns
            ),
            key=lambda item: item[0],
        )

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "receive_timestamp": self.receive_timestamp,
            "sequence": self.sequence,
            "depth": self.depth,
            "levels": {
                side: {str(index): columns for index, columns in sorted(indices.items())}
                for side, indices in self.levels.items()
            },
            "unrecognised_columns": list(self.unrecognised),
        }


def detect_level_columns(headers: list[str]) -> LevelColumns:
    """Recognise ``BID_PX_1``-style level columns in a wide L2 header.

    Handles both orderings a capture tool produces — ``side, field, index`` and
    ``side, index, field`` — over the usual abbreviations. A bare ``BID1`` with
    no field token is read as the *price*, because in every wide export met so
    far the size column carries a size token and the price column is the one
    left bare; that reading is stated in the preview rather than assumed
    silently, and a file where it is wrong is corrected by supplying the mapping.

    Everything it cannot place goes into ``unrecognised`` rather than being
    guessed at, so the preview can show what was ignored.
    """
    levels: dict[str, dict[int, dict[str, str]]] = {"bid": {}, "ask": {}}
    unrecognised: list[str] = []
    timestamp = receive = sequence = None

    normalized = {header: normalize_header(header) for header in headers}
    for header, token in normalized.items():
        if timestamp is None and token in _TIMESTAMP_ALIASES:
            timestamp = header
            continue
        if receive is None and token in _RECEIVE_ALIASES:
            receive = header
            continue
        if sequence is None and token in _SEQUENCE_ALIASES:
            sequence = header
            continue

        match = _SIDE_FIELD_INDEX.match(token)
        if match:
            side_token, field_token, index_token = match.groups()
        else:
            match = _SIDE_INDEX_FIELD.match(token)
            if not match:
                unrecognised.append(header)
                continue
            side_token, index_token, field_token = match.groups()

        if side_token in _BID_TOKENS:
            side = "bid"
        elif side_token in _ASK_TOKENS:
            side = "ask"
        else:  # pragma: no cover - the regex admits nothing else
            unrecognised.append(header)
            continue

        kind = _classify(field_token)
        if kind is None:
            unrecognised.append(header)
            continue
        levels[side].setdefault(int(index_token), {})[kind] = header

    return LevelColumns(
        timestamp=timestamp,
        receive_timestamp=receive,
        sequence=sequence,
        levels=levels,
        unrecognised=tuple(unrecognised),
    )


# ------------------------------------------------------------ snapshot import
def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    token = value.strip()
    return None if token.lower() in NULL_TOKENS else token


def _decimal(value: str | None) -> Decimal | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return parse_decimal(cleaned)
    except (RowParseError, InvalidOperation) as exc:
        raise RowParseError(str(exc)) from exc


class SnapshotImporter:
    """Wide CSV (or canonical parquet) into :class:`OrderBookSnapshot` records."""

    def __init__(self, instrument_id: uuid.UUID, source: str, max_rows: int = 5_000_000) -> None:
        self._instrument_id = instrument_id
        self._source = source
        self._max_rows = max_rows

    def parse(
        self,
        data: bytes,
        columns: LevelColumns | None = None,
        limit: int | None = None,
    ) -> SnapshotBatch:
        if data[:4] == PARQUET_MAGIC:
            return self._parse_parquet(data, limit)
        return self._parse_csv(data, columns, limit)

    # -- parquet ------------------------------------------------------------
    def _parse_parquet(self, data: bytes, limit: int | None) -> SnapshotBatch:
        from domains.microstructure.storage import snapshots_from_parquet

        snapshots = snapshots_from_parquet(data, self._instrument_id, self._source)
        kept = snapshots[:limit] if limit is not None else snapshots
        return SnapshotBatch(
            snapshots=kept,
            rejected=(),
            rows_in=len(kept),
            detected_levels=max((min(len(s.bids), len(s.asks)) for s in kept), default=0),
            detected_columns={"format": "parquet", "schema": "canonical"},
        )

    # -- csv ----------------------------------------------------------------
    def _parse_csv(
        self, data: bytes, columns: LevelColumns | None, limit: int | None
    ) -> SnapshotBatch:
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = [header.strip() for header in (reader.fieldnames or [])]
        detected = columns or detect_level_columns(headers)

        cap = min(limit, self._max_rows) if limit is not None else self._max_rows
        snapshots: list[OrderBookSnapshot] = []
        rejected: list[RejectedRow] = []
        seen: set[tuple[datetime, int | None]] = set()
        rows_in = 0

        for row_number, raw in enumerate(reader, start=1):
            if rows_in >= cap:
                break
            rows_in += 1
            cleaned = {
                (key.strip() if key else ""): value for key, value in raw.items() if key is not None
            }
            outcome = self._row(row_number, cleaned, detected, seen)
            if isinstance(outcome, RejectedRow):
                rejected.append(outcome)
            else:
                snapshots.append(outcome)

        return SnapshotBatch(
            snapshots=tuple(snapshots),
            rejected=tuple(rejected),
            rows_in=rows_in,
            detected_levels=detected.depth,
            detected_columns=detected.to_dict(),
        )

    def _row(
        self,
        row_number: int,
        raw: dict,
        columns: LevelColumns,
        seen: set[tuple[datetime, int | None]],
    ) -> OrderBookSnapshot | RejectedRow:
        if columns.timestamp is None:
            return RejectedRow(
                row_number,
                SnapshotRejection.MISSING_TIMESTAMP,
                "No column in this file was recognised as the snapshot "
                "timestamp, so no row can be placed in time.",
                raw,
            )
        try:
            stamp_text = _clean(raw.get(columns.timestamp))
            if stamp_text is None:
                return RejectedRow(
                    row_number,
                    SnapshotRejection.MISSING_TIMESTAMP,
                    f"Column {columns.timestamp!r} is empty on this row.",
                    raw,
                )
            exchange_timestamp = parse_datetime(stamp_text)
            receive_timestamp = exchange_timestamp
            if columns.receive_timestamp:
                receive_text = _clean(raw.get(columns.receive_timestamp))
                if receive_text is not None:
                    receive_timestamp = parse_datetime(receive_text)

            sequence: int | None = None
            if columns.sequence:
                sequence_text = _clean(raw.get(columns.sequence))
                if sequence_text is not None:
                    sequence = int(Decimal(sequence_text.replace(",", "")))
        except (RowParseError, InvalidOperation, ValueError) as exc:
            return RejectedRow(row_number, SnapshotRejection.UNPARSEABLE_ROW, str(exc), raw)

        key = (exchange_timestamp, sequence)
        if key in seen:
            return RejectedRow(
                row_number,
                SnapshotRejection.DUPLICATE_OBSERVATION,
                f"A snapshot at {exchange_timestamp.isoformat()} with sequence "
                f"{sequence} already appeared in this file. The duplicate is kept "
                "out of the dataset rather than double-counting the book.",
                raw,
            )

        sides: dict[str, tuple[OrderBookLevel, ...]] = {}
        for side in ("bid", "ask"):
            outcome = self._levels(row_number, raw, columns, side)
            if isinstance(outcome, RejectedRow):
                return outcome
            sides[side] = outcome

        if not sides["bid"] and not sides["ask"]:
            return RejectedRow(
                row_number,
                SnapshotRejection.NO_LEVELS,
                "Neither side of this row carries a priced level with a size, so "
                "there is no book here to measure.",
                raw,
            )

        try:
            snapshot = OrderBookSnapshot(
                instrument_id=self._instrument_id,
                exchange_timestamp=exchange_timestamp,
                receive_timestamp=receive_timestamp,
                bids=sides["bid"],
                asks=sides["ask"],
                source=self._source,
                sequence_number=sequence,
            )
        except ValueError as exc:
            return RejectedRow(
                row_number,
                SnapshotRejection.LEVELS_OUT_OF_ORDER,
                f"{exc}. The levels are not repaired by sorting them: a file whose "
                "price and size columns are transposed produces exactly this, and "
                "sorting would turn a detectable mistake into a plausible book.",
                raw,
            )
        seen.add(key)
        return snapshot

    def _levels(
        self, row_number: int, raw: dict, columns: LevelColumns, side: str
    ) -> tuple[OrderBookLevel, ...] | RejectedRow:
        levels: list[OrderBookLevel] = []
        for index, mapping in columns.ordered(side):
            try:
                price = _decimal(raw.get(mapping["price"]))
                quantity = _decimal(raw.get(mapping["size"]))
                order_count = None
                if "orders" in mapping:
                    order_text = _clean(raw.get(mapping["orders"]))
                    order_count = int(Decimal(order_text)) if order_text else None
            except (RowParseError, InvalidOperation, ValueError) as exc:
                return RejectedRow(
                    row_number,
                    SnapshotRejection.UNPARSEABLE_ROW,
                    f"{side} level {index}: {exc}",
                    raw,
                )

            if price is None and quantity is None:
                # The book is shallower than the file is wide. Stop here rather
                # than skipping the hole: a level beyond an empty one is not
                # level `index`, and renumbering it would misreport the depth.
                break
            if price is None:
                return RejectedRow(
                    row_number,
                    SnapshotRejection.PRICE_WITHOUT_QUANTITY,
                    f"{side} level {index} has a size but no price, so there is "
                    "nothing to rest it at.",
                    raw,
                )
            if quantity is None:
                return RejectedRow(
                    row_number,
                    SnapshotRejection.PRICE_WITHOUT_QUANTITY,
                    f"{side} level {index} has a price but no size. A level with "
                    "no size is not a level, and treating it as zero would "
                    "assert an empty level the venue did not publish.",
                    raw,
                )
            if price < 0:
                return RejectedRow(
                    row_number,
                    SnapshotRejection.NEGATIVE_PRICE,
                    f"{side} level {index} is priced at {price}.",
                    raw,
                )
            if quantity < 0:
                return RejectedRow(
                    row_number,
                    SnapshotRejection.NEGATIVE_QUANTITY,
                    f"{side} level {index} carries a size of {quantity}.",
                    raw,
                )
            levels.append(OrderBookLevel(price=price, quantity=quantity, order_count=order_count))
        return tuple(levels)


# --------------------------------------------------------------- event import
BOOK_EVENT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "timestamp",
        FieldType.DATETIME,
        True,
        ("timestamp", "time", "datetime", "exchangetimestamp", "eventtime", "ts"),
        "When the venue processed the message.",
    ),
    FieldSpec(
        "event_type",
        FieldType.STRING,
        True,
        ("eventtype", "event", "action", "type", "messagetype", "msgtype", "update"),
        "ADD, CANCEL, MODIFY or TRADE. Never inferred from a size change.",
    ),
    FieldSpec(
        "side",
        FieldType.STRING,
        False,
        ("side", "bidask", "direction", "buysell"),
        "BID or ASK. Without it, no event can be attributed to a side.",
    ),
    FieldSpec(
        "price",
        FieldType.DECIMAL,
        False,
        ("price", "px", "limitprice", "level", "levelprice"),
        "The price level the message touched.",
    ),
    FieldSpec(
        "quantity",
        FieldType.DECIMAL,
        False,
        ("quantity", "qty", "size", "sz", "volume", "amount", "shares"),
        "How much the message added or removed. Always positive.",
    ),
    FieldSpec(
        "sequence_number",
        FieldType.INTEGER,
        False,
        ("sequence", "seq", "seqno", "sequencenumber", "updateid", "messageid"),
        "Venue sequence number. Without it, tape completeness is unknowable.",
    ),
    FieldSpec(
        "order_id",
        FieldType.STRING,
        False,
        ("orderid", "order_id", "id", "orderref", "orderreference"),
        "The venue's identifier for the order the message concerns.",
    ),
    FieldSpec(
        "receive_timestamp",
        FieldType.DATETIME,
        False,
        ("receivetimestamp", "receivetime", "localtime", "capturetime", "receivedat"),
        "When the capture saw it, if the file says.",
    ),
)


class EventImporter:
    """A tape of order-book messages into :class:`BookEvent` records."""

    def __init__(self, instrument_id: uuid.UUID, source: str, max_rows: int = 5_000_000) -> None:
        self._instrument_id = instrument_id
        self._source = source
        self._parser = TabularParser(BOOK_EVENT_FIELDS, max_rows=max_rows)
        self._max_rows = max_rows

    @staticmethod
    def infer(headers: list[str]) -> ColumnMapping:
        return infer_mapping(headers, BOOK_EVENT_FIELDS)

    def parse(
        self, data: bytes, mapping: ColumnMapping | None = None, limit: int | None = None
    ) -> EventBatch:
        if data[:4] == PARQUET_MAGIC:
            from domains.microstructure.storage import events_from_parquet

            events = events_from_parquet(data, self._instrument_id, self._source)
            kept = events[:limit] if limit is not None else events
            return EventBatch(
                events=kept,
                rejected=(),
                rows_in=len(kept),
                detected_columns={"format": "parquet", "schema": "canonical"},
            )

        headers = TabularParser.read_headers(data)
        applied = mapping or self.infer(headers)
        parsed = self._parser.parse(data, applied, limit=limit)

        rejected: list[RejectedRow] = [
            RejectedRow(
                row_number=error.row_number,
                reason=EventRejection.UNPARSEABLE_ROW,
                message=error.message,
                raw=error.raw,
            )
            for error in parsed.errors
        ]
        events: list[BookEvent] = []
        for row in parsed.rows:
            outcome = self._row(row.row_number, row.values, row.raw)
            if isinstance(outcome, RejectedRow):
                rejected.append(outcome)
            else:
                events.append(outcome)

        return EventBatch(
            events=tuple(events),
            rejected=tuple(rejected),
            rows_in=len(parsed.rows) + len(parsed.errors),
            detected_columns=applied.to_dict(),
        )

    def _row(self, row_number: int, values: dict, raw: dict) -> BookEvent | RejectedRow:
        stamp = values.get("timestamp")
        if stamp is None:
            return RejectedRow(
                row_number,
                EventRejection.MISSING_TIMESTAMP,
                "The row has no timestamp, so it cannot be placed in the tape.",
                raw,
            )
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)

        type_text = values.get("event_type")
        if not type_text:
            return RejectedRow(
                row_number,
                EventRejection.MISSING_EVENT_TYPE,
                "The row has no event type. What a message did is not inferable "
                "from its price and size, and a cancellation counted as a trade "
                "would drain a queue that never drained.",
                raw,
            )
        try:
            event_type = BookEventType.parse(type_text)
        except ValueError as exc:
            return RejectedRow(row_number, EventRejection.UNRECOGNISED_EVENT_TYPE, str(exc), raw)

        side = None
        side_text = values.get("side")
        if side_text:
            try:
                side = BookSide.parse(side_text)
            except ValueError as exc:
                return RejectedRow(row_number, EventRejection.UNRECOGNISED_SIDE, str(exc), raw)

        price = values.get("price")
        quantity = values.get("quantity")
        if price is not None and price < 0:
            return RejectedRow(
                row_number,
                EventRejection.NEGATIVE_PRICE,
                f"A message priced at {price} is not a price level.",
                raw,
            )
        if quantity is not None and quantity < 0:
            return RejectedRow(
                row_number,
                EventRejection.NEGATIVE_QUANTITY,
                f"A message carrying {quantity} is not a quantity; a removal is "
                "carried by the event type, not by a sign.",
                raw,
            )

        receive = values.get("receive_timestamp")
        if receive is not None and receive.tzinfo is None:
            receive = receive.replace(tzinfo=UTC)

        return BookEvent(
            instrument_id=self._instrument_id,
            exchange_timestamp=stamp,
            event_type=event_type,
            source=self._source,
            side=side,
            price=price,
            quantity=quantity,
            sequence_number=values.get("sequence_number"),
            order_id=values.get("order_id"),
            receive_timestamp=receive,
        )
