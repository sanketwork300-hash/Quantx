"""Getting bar rows out of a file, whatever the file is.

CSV goes through the platform's existing :class:`TabularParser`, so header
inference, type coercion and per-row error reporting behave exactly as they do
for an option chain. Parquet is read with Arrow and then handed to the same
coercion, so the two formats cannot disagree about what a column means.

Instruments are resolved, not assumed. A file may name one instrument for the
whole batch, or carry a symbol column resolved row by row against the instrument
master. A symbol that matches nothing — or matches more than one thing — is
**reported**, never attached to the closest candidate: a bar filed under the
wrong instrument is a price series that looks perfectly reasonable and is
somebody else's.
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from domains.instruments.enums import AssetClass
from domains.instruments.service import InstrumentService
from domains.market_data.ingestion.column_mapping import (
    BAR_FIELDS,
    ColumnMapping,
    infer_mapping,
)
from domains.market_data.ingestion.parser import ParseResult, TabularParser

#: Parquet's magic number, which both begins and ends a valid file.
PARQUET_MAGIC = b"PAR1"

#: An ISO-8601 timestamp that names a zone: ends in ``Z``, or carries
#: ``+HH:MM`` / ``-HH:MM`` after the time.
_OFFSET = re.compile(r"(?:[Zz]|[+-]\d{2}:?\d{2})$")


def has_timezone_offset(text: object) -> bool:
    """Whether a timestamp's *source text* named a zone.

    The shared :class:`TabularParser` stamps UTC on a naive timestamp. That is
    right for an option chain, whose as-of moment is supplied separately, and
    wrong for a historical file: a year of bars stamped in the venue's local
    time and read as UTC is a year of bars shifted by hours, and nothing
    downstream would ever say so.

    So the reader asks the source text before coercion and puts a naive datetime
    back where the text named no zone, which lets the validator refuse it by
    name rather than having the rule silently never fire.
    """
    if isinstance(text, datetime):
        return text.tzinfo is not None
    if not isinstance(text, str):
        return False
    return bool(_OFFSET.search(text.strip()))


class ReaderError(Exception):
    """The file could not be read as a table at all."""


@dataclass(frozen=True, slots=True)
class UnresolvedSymbol:
    """A symbol the instrument master could not turn into one instrument."""

    symbol: str
    reason: str
    rows: int
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "reason": self.reason,
            "rows": self.rows,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True, slots=True)
class ReadResult:
    """Rows ready for validation, plus everything that did not get that far."""

    rows: list[dict] = field(default_factory=list)
    headers: tuple[str, ...] = ()
    mapping: ColumnMapping = field(default_factory=ColumnMapping)
    mapping_inferred: bool = False
    parse_errors: tuple[dict, ...] = ()
    unresolved: tuple[UnresolvedSymbol, ...] = ()
    rows_in_file: int = 0

    @property
    def unresolved_rows(self) -> int:
        return sum(item.rows for item in self.unresolved)

    @property
    def conserved(self) -> bool:
        """Every row in the file is a row read, a parse error, or an unresolved
        symbol. Nothing evaporates between the file and the validator."""
        return self.rows_in_file == len(self.rows) + len(self.parse_errors) + self.unresolved_rows

    def to_dict(self) -> dict:
        return {
            "rows_in_file": self.rows_in_file,
            "rows_read": len(self.rows),
            "parse_errors": len(self.parse_errors),
            "unresolved_rows": self.unresolved_rows,
            "conserved": self.conserved,
            "mapping": self.mapping.to_dict(),
            "mapping_inferred": self.mapping_inferred,
            "unresolved": [item.to_dict() for item in self.unresolved],
            "errors": [dict(item) for item in self.parse_errors[:100]],
        }


def is_parquet(data: bytes) -> bool:
    """Sniffed from the bytes, not from a filename. A name is a claim; a magic
    number is a fact."""
    return len(data) >= 8 and data[:4] == PARQUET_MAGIC and data[-4:] == PARQUET_MAGIC


def parquet_to_records(data: bytes) -> tuple[list[dict], list[str]]:
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(io.BytesIO(data))
    except Exception as exc:  # pyarrow raises several unrelated types
        raise ReaderError(f"the file is not readable Parquet: {exc}") from exc
    return table.to_pylist(), list(table.schema.names)


class BarFileReader:
    """Reads a bar file into rows the validator can take."""

    def __init__(self, instruments: InstrumentService, max_rows: int) -> None:
        self._instruments = instruments
        self._max_rows = max_rows
        self._parser = TabularParser(BAR_FIELDS, max_rows)
        self._timestamp_column: str | None = None

    async def read(
        self,
        data: bytes,
        exchange: str,
        mapping: ColumnMapping | None = None,
        instrument_id: uuid.UUID | None = None,
        interval: str = "1d",
    ) -> ReadResult:
        if is_parquet(data):
            records, headers = parquet_to_records(data)
            supplied = mapping or ColumnMapping()
            inferred = not supplied.to_dict()
            if inferred:
                supplied = infer_mapping(headers, BAR_FIELDS)
            missing = supplied.missing_required(BAR_FIELDS)
            if missing:
                raise ReaderError(
                    f"the file does not carry {', '.join(missing)}; supply a column "
                    "mapping naming which columns hold them"
                )
            # Parquet arrives typed and the shared coercer takes text, so the
            # values are stringified rather than given their own coercion path.
            # One path means CSV and Parquet cannot come to disagree about what
            # a column means.
            stringified = [
                {key: (None if value is None else str(value)) for key, value in record.items()}
                for record in records
            ]
            parsed = self._parser.parse_records(enumerate(stringified, start=1), headers, supplied)
        else:
            headers = self._parser.read_headers(data)
            supplied = mapping or ColumnMapping()
            inferred = not supplied.to_dict()
            if inferred:
                supplied = infer_mapping(headers, BAR_FIELDS)
            missing = supplied.missing_required(BAR_FIELDS)
            if missing:
                raise ReaderError(
                    f"the file does not carry {', '.join(missing)}; supply a column "
                    "mapping naming which columns hold them"
                )
            parsed = self._parser.parse(data, supplied)

        self._timestamp_column = supplied.column_for("exchange_timestamp")
        rows, unresolved = await self._attach_instruments(parsed, exchange, instrument_id, interval)
        return ReadResult(
            rows=rows,
            headers=tuple(headers),
            mapping=supplied,
            mapping_inferred=inferred,
            parse_errors=tuple(item.to_dict() for item in parsed.errors),
            unresolved=unresolved,
            rows_in_file=_rows_in(parsed),
        )

    async def _attach_instruments(
        self,
        parsed: ParseResult,
        exchange: str,
        instrument_id: uuid.UUID | None,
        interval: str,
    ) -> tuple[list[dict], tuple[UnresolvedSymbol, ...]]:
        rows: list[dict] = []
        unresolved: dict[str, UnresolvedSymbol] = {}
        cache: dict[str, uuid.UUID | UnresolvedSymbol] = {}

        for row in parsed.rows:
            values = dict(row.values)
            values["interval"] = interval

            source_text = row.raw.get(self._timestamp_column) if self._timestamp_column else None
            moment = values.get("exchange_timestamp")
            if isinstance(moment, datetime) and not has_timezone_offset(source_text):
                # The parser defaulted this to UTC. Hand the validator the naive
                # value the file actually had, so a whole series is not silently
                # shifted by the venue's offset.
                values["exchange_timestamp"] = moment.replace(tzinfo=None)

            if instrument_id is not None:
                values["instrument_id"] = instrument_id
                rows.append(values)
                continue

            symbol = values.get("symbol")
            if not symbol:
                unresolved.setdefault(
                    "", UnresolvedSymbol("", "the row names no symbol and none was supplied", 0)
                )
                item = unresolved[""]
                unresolved[""] = UnresolvedSymbol(item.symbol, item.reason, item.rows + 1)
                continue

            key = str(symbol).strip().upper()
            if key not in cache:
                cache[key] = await self._resolve(key, exchange)

            resolved = cache[key]
            if isinstance(resolved, uuid.UUID):
                values["instrument_id"] = resolved
                rows.append(values)
            else:
                previous = unresolved.get(key, resolved)
                unresolved[key] = UnresolvedSymbol(
                    previous.symbol, previous.reason, previous.rows + 1, previous.candidates
                )

        return rows, tuple(unresolved.values())

    async def _resolve(self, symbol: str, exchange: str) -> uuid.UUID | UnresolvedSymbol:
        """Exactly one match, or a reported failure.

        Ambiguity is a first-class outcome here as it is in the instrument
        resolver: two instruments with the same symbol on one exchange is a
        question the platform cannot answer for the user, and answering it
        anyway would file a price series under the wrong contract.
        """
        matches = await self._instruments.search(exchange=exchange, symbol=symbol, limit=5)
        usable = [
            item
            for item in matches
            if item.asset_class in {AssetClass.EQUITY, AssetClass.INDEX, AssetClass.FUTURE}
        ]
        if not usable:
            return UnresolvedSymbol(
                symbol,
                f"no instrument on {exchange} matches this symbol; load the instrument "
                "master, or supply an instrument id for the whole file",
                0,
            )
        if len(usable) > 1:
            return UnresolvedSymbol(
                symbol,
                f"{len(usable)} instruments on {exchange} match this symbol, so the rows "
                "cannot be attributed to one of them",
                0,
                tuple(item.canonical_key for item in usable),
            )
        return usable[0].id


def _rows_in(parsed: ParseResult) -> int:
    return len(parsed.rows) + len(parsed.errors)
