"""Tabular parsing with per-row error capture.

Two rules shape this module:

1. **A bad row never aborts the file.** It is captured with its 1-based source
   row number and a reason, and parsing continues. A user whose 40,000-row chain
   has three malformed rows needs the other 39,997 and a list of three problems,
   not a stack trace.

2. **No formula evaluation, ever.** Values are read as text and coerced by
   explicit parsers. A cell beginning with ``=`` is data, not a spreadsheet
   formula (docs/architecture.md, upload hardening).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from domains.instruments.enums import OptionType
from domains.market_data.ingestion.column_mapping import (
    ColumnMapping,
    FieldSpec,
    FieldType,
)

#: Date formats accepted for expiry columns, tried in order. ISO first so an
#: unambiguous file is never reinterpreted by a locale-specific format.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
)

DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)

NULL_TOKENS = frozenset({"", "-", "na", "n/a", "nan", "none", "null", "--"})


class RowParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedRow:
    #: 1-based row number in the source file, excluding the header. Reported to
    #: the user verbatim so they can find the row in their own spreadsheet.
    row_number: int
    values: dict
    raw: dict


@dataclass(frozen=True, slots=True)
class RowError:
    row_number: int
    column: str | None
    message: str
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "column": self.column,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ParseResult:
    headers: list[str]
    rows: list[ParsedRow]
    errors: list[RowError]
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    token = value.strip()
    # Strip thousands separators and currency-adjacent whitespace but do not
    # attempt to interpret anything else.
    if token.lower() in NULL_TOKENS:
        return None
    return token


def parse_decimal(value: str) -> Decimal:
    token = value.replace(",", "").replace("_", "")
    try:
        parsed = Decimal(token)
    except InvalidOperation as exc:
        raise RowParseError(f"not a number: {value!r}") from exc
    if not parsed.is_finite():
        raise RowParseError(f"non-finite number: {value!r}")
    return parsed


def parse_integer(value: str) -> int:
    try:
        return int(Decimal(value.replace(",", "")))
    except (InvalidOperation, ValueError) as exc:
        raise RowParseError(f"not an integer: {value!r}") from exc


def parse_date(value: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise RowParseError(f"unrecognised date: {value!r}")


def parse_datetime(value: str) -> datetime:
    for fmt in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RowParseError(f"unrecognised timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_COERCERS = {
    FieldType.STRING: lambda value: value,
    FieldType.DECIMAL: parse_decimal,
    FieldType.INTEGER: parse_integer,
    FieldType.DATE: parse_date,
    FieldType.DATETIME: parse_datetime,
    FieldType.OPTION_TYPE: OptionType.parse,
}


class TabularParser:
    def __init__(self, specs: tuple[FieldSpec, ...], max_rows: int) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._max_rows = max_rows

    @staticmethod
    def read_headers(data: bytes) -> list[str]:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
        for row in reader:
            return [cell.strip() for cell in row]
        return []

    def parse(self, data: bytes, mapping: ColumnMapping, limit: int | None = None) -> ParseResult:
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = [header.strip() for header in (reader.fieldnames or [])]

        cap = min(limit, self._max_rows) if limit is not None else self._max_rows
        rows: list[ParsedRow] = []
        errors: list[RowError] = []
        truncated = False

        for row_number, raw in enumerate(reader, start=1):
            if len(rows) >= cap:
                # Only a genuine overflow of the configured cap is truncation;
                # a deliberate preview limit is not an error condition.
                truncated = limit is None or cap == self._max_rows
                break

            cleaned = {
                (key.strip() if key else ""): _clean(value)
                for key, value in raw.items()
                if key is not None
            }
            try:
                values = self._coerce_row(cleaned, mapping)
            except RowParseError as exc:
                errors.append(
                    RowError(
                        row_number=row_number,
                        column=getattr(exc, "column", None),
                        message=str(exc),
                        raw=cleaned,
                    )
                )
                continue
            rows.append(ParsedRow(row_number=row_number, values=values, raw=cleaned))

        return ParseResult(headers=headers, rows=rows, errors=errors, truncated=truncated)

    def _coerce_row(self, raw: dict, mapping: ColumnMapping) -> dict:
        values: dict = {}
        for name, spec in self._specs.items():
            column = mapping.column_for(name)
            if column is None:
                if spec.required:
                    error = RowParseError(f"required field {name!r} is not mapped to a column")
                    error.column = None
                    raise error
                continue

            token = raw.get(column)
            if token is None:
                # An empty cell is a *domain* problem, not a parse problem. The
                # validator turns it into a precise reason (MISSING_EXPIRY,
                # MISSING_OPTION_TYPE, ...) rather than a generic parse failure,
                # so the user is told which field their row is missing.
                values[name] = None
                continue

            try:
                values[name] = _COERCERS[spec.field_type](token)
            except (RowParseError, ValueError) as exc:
                error = RowParseError(f"column {column!r}: {exc}")
                error.column = column
                raise error from exc
        return values
