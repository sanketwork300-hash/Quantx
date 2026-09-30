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
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from domains.instruments.enums import OptionType
from domains.market_data.ingestion.column_mapping import (
    ColumnMapping,
    FieldSpec,
    FieldType,
    infer_mapping,
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

#: Delimiters a tabular export is written with, in the order a tie is settled.
DELIMITERS: tuple[str, ...] = (",", "\t", ";")

#: Lines examined to find the delimiter and the header row. Exports put at most
#: a title and a banner above the real header.
MAX_HEADER_SCAN_ROWS = 8

#: A date written as three numbers, where the first two could each be the day.
_NUMERIC_DATE = re.compile(r"^(\d{1,2})([/\-.])(\d{1,2})\2(\d{4})$")

#: A number grouped in thousands (1,234,567) or the Indian way (12,34,567),
#: which is what an NSE export writes. Anything else with a comma in it is not
#: a grouped number, and deleting the comma would invent one.
_GROUPED = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d{1,2}(,\d{2})*,\d{3})(\.\d+)?$")

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
class CellIssue:
    """An optional cell that could not be read, in a row that was kept.

    The row is still a row: a chain whose ``time`` column holds ``15:30:00``
    has perfectly good strikes and prices. The cell is read as absent and
    reported here, so the value is set aside with a reason rather than the
    whole row going with it.
    """

    row_number: int
    field: str
    column: str
    value: str
    message: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    headers: list[str]
    rows: list[ParsedRow]
    errors: list[RowError]
    truncated: bool = False
    #: Only ever populated by a parser built with ``lenient_optional``.
    cell_issues: list[CellIssue] = field(default_factory=list)
    #: The character the cells were separated by, read off the file.
    delimiter: str = ","
    #: 0-based line the header was found on. Lines above it were not data.
    header_row: int = 0
    #: Header names that occur more than once *and* that the mapping points at,
    #: so which column was meant cannot be told from the name.
    duplicate_headers: tuple[str, ...] = ()
    #: One per date column whose numeric dates needed an order. Only ever
    #: populated by a parser built with ``resolve_date_order``.
    date_readings: tuple[DateReading, ...] = ()
    #: Column -> count of timestamps that stated no offset and were read as UTC.
    naive_timestamps: dict[str, int] = field(default_factory=dict)

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


def _ungroup(value: str) -> str:
    """Remove thousands separators, and only thousands separators.

    ``24000,50`` is a decimal comma, not a grouped number. Deleting the comma
    reads it as 2,400,050 -- a price a hundred times too large, with no error
    anywhere -- so a comma that cannot be a group separator is refused.
    """
    if "," in value and not _GROUPED.match(value.strip()):
        raise RowParseError(
            f"not a number: {value!r} (the comma is not a thousands separator; "
            "a decimal comma is not read as a decimal point)"
        )
    return value.replace(",", "")


def parse_decimal(value: str) -> Decimal:
    token = _ungroup(value).replace("_", "")
    try:
        parsed = Decimal(token)
    except InvalidOperation as exc:
        raise RowParseError(f"not a number: {value!r}") from exc
    if not parsed.is_finite():
        raise RowParseError(f"non-finite number: {value!r}")
    return parsed


def parse_integer(value: str) -> int:
    try:
        parsed = Decimal(_ungroup(value))
    except (InvalidOperation, ValueError) as exc:
        raise RowParseError(f"not an integer: {value!r}") from exc
    # 1.9 is not 1. Truncating it would store a number the file does not hold.
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise RowParseError(f"not an integer: {value!r}")
    return int(parsed)


def parse_date(value: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise RowParseError(f"unrecognised date: {value!r}")


def read_datetime(value: str) -> tuple[datetime, bool]:
    """A timestamp, and whether its offset had to be assumed.

    A timestamp that states no offset is read as UTC. That is an assumption
    about the file, not a fact in it, so it is returned alongside the value for
    the caller to report.
    """
    parsed: datetime | None = None
    for fmt in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        break
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise RowParseError(f"unrecognised timestamp: {value!r}") from exc
    if parsed.tzinfo is not None:
        return parsed, False
    return parsed.replace(tzinfo=UTC), True


def parse_datetime(value: str) -> datetime:
    return read_datetime(value)[0]


class DateOrder(StrEnum):
    """Which of a numeric date's first two numbers is the day."""

    DAY_FIRST = "DMY"
    MONTH_FIRST = "MDY"


class DateProblem(StrEnum):
    #: No value in the column says which number is the day.
    AMBIGUOUS = "AMBIGUOUS"
    #: Some values can only be day-first and others only month-first.
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True, slots=True)
class DateReading:
    """How a column of numeric dates was read, and on what evidence.

    ``03/04/2026`` is 3 April or 4 March, and nothing in the cell says which.
    Reading each cell on its own takes the first format that fits, so one
    column can come out day-first in some rows and month-first in others. The
    order is therefore settled once for the column: from a value that can only
    be read one way, or from the caller, and otherwise not at all.
    """

    field: str
    column: str
    order: DateOrder | None
    #: True when the caller stated the order rather than the column showing it.
    stated: bool = False
    #: A value from the column: the one that settled the order, or one that
    #: could not be settled.
    example: str | None = None
    problem: DateProblem | None = None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "column": self.column,
            "order": str(self.order) if self.order is not None else None,
            "stated": self.stated,
            "example": self.example,
            "problem": str(self.problem) if self.problem is not None else None,
        }


def _numeric_date(value: str, order: DateOrder) -> date:
    match = _NUMERIC_DATE.match(value)
    first, second, year = int(match.group(1)), int(match.group(3)), int(match.group(4))
    day, month = (first, second) if order is DateOrder.DAY_FIRST else (second, first)
    try:
        return date(year, month, day)
    except ValueError as exc:
        written = "day first" if order is DateOrder.DAY_FIRST else "month first"
        raise RowParseError(
            f"not a date when read {written}, as the rest of this column is: {value!r}"
        ) from exc


def detect_delimiter(text: str) -> str:
    """Which character separates the cells: a comma, a tab or a semicolon.

    Read off the file's first lines rather than assumed. The delimiter chosen
    is the one that splits those lines into the most cells *consistently*: a
    comma inside a quoted ``"1,877.00"`` is not a separator, and a title line
    that happens to contain one should not outvote the rows beneath it. A tie
    goes to the comma.
    """
    lines = [line for line in text.splitlines()[: MAX_HEADER_SCAN_ROWS * 4] if line.strip()]
    lines = lines[: MAX_HEADER_SCAN_ROWS + 1]
    if not lines:
        return ","
    best, best_score = ",", 0
    for delimiter in DELIMITERS:
        widths = Counter(len(row) for row in csv.reader(lines, delimiter=delimiter))
        width, agreeing = max(widths.items(), key=lambda item: (item[1] * (item[0] - 1), item[0]))
        score = agreeing * (width - 1)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def find_header_row(
    rows: Sequence[Sequence[str]],
    specs: tuple[FieldSpec, ...],
    mapping: ColumnMapping | None = None,
) -> int:
    """Which of the file's first lines names its columns.

    Usually the first. An export that puts a title above its header has one
    further down, and reading the title as the header leaves every field
    unmatched. The header is the line that names the most fields: the columns
    a supplied mapping points at, or otherwise the fields matched by header
    name. A line naming fewer than two is not taken for a header over line one.
    """
    named = set(mapping.mapping.values()) if mapping is not None else set()
    best, best_score = 0, 1
    for index, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        cells = [cell.strip() for cell in row]
        score = (
            sum(1 for cell in cells if cell in named)
            if named
            else len(infer_mapping(cells, specs).mapping)
        )
        if score > best_score:
            best, best_score = index, score
    return best


class NotAnOption:
    """The value of an option-type cell that names something else: a future.

    An exchange bhavcopy lists futures beside options and marks them ``XX``.
    That is not an unreadable cell -- the column holds exactly what it was
    taken to hold -- so it is carried to the validator as a fact about the row
    rather than raised as a parse failure.
    """

    def __repr__(self) -> str:
        return "NOT_AN_OPTION"

    __str__ = __repr__


NOT_AN_OPTION = NotAnOption()


_COERCERS = {
    FieldType.STRING: lambda value: value,
    FieldType.DECIMAL: parse_decimal,
    FieldType.INTEGER: parse_integer,
    FieldType.DATE: parse_date,
    FieldType.DATETIME: parse_datetime,
    FieldType.OPTION_TYPE: OptionType.parse,
}


@dataclass(slots=True)
class _Run:
    """What one pass over a file learns besides its rows."""

    date_orders: dict[str, DateOrder | None] = field(default_factory=dict)
    naive_timestamps: Counter = field(default_factory=Counter)
    issues: list[tuple[str, str, str, str]] = field(default_factory=list)


class TabularParser:
    def __init__(
        self,
        specs: tuple[FieldSpec, ...],
        max_rows: int,
        lenient_optional: bool = False,
        resolve_date_order: bool = False,
        non_option_tokens: frozenset[str] = frozenset(),
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._max_rows = max_rows
        #: When set, an optional cell that fails coercion is read as absent and
        #: reported as a :class:`CellIssue` instead of rejecting its row. Off by
        #: default: a trade or position importer may have no use for a row
        #: whose optional field it could not read, and says so by not opting in.
        self._lenient_optional = lenient_optional
        #: When set, the order of a numeric date (day first or month first) is
        #: settled once per column and reported, and a column that does not
        #: settle it is left unread rather than guessed. Off by default, where
        #: each cell takes the first format that fits.
        self._resolve_date_order = resolve_date_order
        #: Option-type values that mean "this row is not an option".
        self._non_option_tokens = frozenset(token.upper() for token in non_option_tokens)

    @staticmethod
    def read_headers(data: bytes, specs: tuple[FieldSpec, ...] | None = None) -> list[str]:
        """The header line, without parsing the file.

        With ``specs`` the header is looked for among the first lines, the way
        :meth:`parse` looks for it; without, it is the first line.
        """
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text), delimiter=detect_delimiter(text))
        head = [row for _, row in zip(range(MAX_HEADER_SCAN_ROWS), reader, strict=False)]
        if not head:
            return []
        index = find_header_row(head, specs) if specs is not None else 0
        return [cell.strip() for cell in head[index]]

    def parse(
        self,
        data: bytes,
        mapping: ColumnMapping,
        limit: int | None = None,
        *,
        date_order: DateOrder | None = None,
    ) -> ParseResult:
        text = data.decode("utf-8-sig", errors="replace")
        delimiter = detect_delimiter(text)
        lines = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        header_row = find_header_row(lines, tuple(self._specs.values()), mapping)
        headers = [cell.strip() for cell in lines[header_row]] if lines else []

        # Cells are taken by position, so a row shorter than the header reads
        # as absent cells rather than shifting, and extra cells are ignored.
        # Where a header name repeats the later column is the one a name
        # reaches; ``duplicate_headers`` says when that choice mattered.
        records: list[tuple[int, dict]] = []
        for cells in lines[header_row + 1 :]:
            if not cells:
                continue
            records.append(
                (
                    len(records) + 1,
                    {
                        header: (cells[index] if index < len(cells) else None)
                        for index, header in enumerate(headers)
                    },
                )
            )

        mapped = set(mapping.mapping.values())
        counts = Counter(header for header in headers if header)
        result = self.parse_records(records, headers, mapping, limit=limit, date_order=date_order)
        return ParseResult(
            headers=result.headers,
            rows=result.rows,
            errors=result.errors,
            truncated=result.truncated,
            cell_issues=result.cell_issues,
            delimiter=delimiter,
            header_row=header_row,
            duplicate_headers=tuple(
                header for header, count in counts.items() if count > 1 and header in mapped
            ),
            date_readings=result.date_readings,
            naive_timestamps=result.naive_timestamps,
        )

    def parse_records(
        self,
        records: Iterable[tuple[int, dict]],
        headers: list[str],
        mapping: ColumnMapping,
        limit: int | None = None,
        *,
        date_order: DateOrder | None = None,
    ) -> ParseResult:
        """Coerce records that have already been resolved to columns.

        Split out from :meth:`parse` so that a file whose layout cannot be
        described by column *names* -- a two-sided option chain, where the same
        header appears on the call side and the put side -- is resolved by
        column index first and then runs through exactly this coercion. Two
        readers of the same file that coerced values differently would be two
        ingestion paths, and only one of them would be tested.

        ``row_number`` is the caller's, not this method's: a two-sided splitter
        emits two records from one line and both must report the line the user
        can actually find in their spreadsheet.
        """
        cap = min(limit, self._max_rows) if limit is not None else self._max_rows
        rows: list[ParsedRow] = []
        errors: list[RowError] = []
        cell_issues: list[CellIssue] = []
        truncated = False

        run = _Run()
        date_readings: tuple[DateReading, ...] = ()
        if self._resolve_date_order:
            # Settled over every record, not just the ones a preview limit will
            # coerce: a sample and the file it came from must not read the same
            # column two different ways.
            records = list(records)
            date_readings = self._date_readings(records, mapping, date_order)
            run.date_orders = {reading.field: reading.order for reading in date_readings}

        for row_number, raw in records:
            if len(rows) >= cap:
                # Only a genuine overflow of the configured cap is truncation;
                # a deliberate preview limit is not an error condition.
                truncated = limit is None or cap == self._max_rows
                break

            cleaned = {(key.strip() if key else ""): _clean(value) for key, value in raw.items()}
            run.issues = []
            try:
                values = self._coerce_row(cleaned, mapping, run)
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
            cell_issues.extend(CellIssue(row_number, *issue) for issue in run.issues)

        return ParseResult(
            headers=headers,
            rows=rows,
            errors=errors,
            truncated=truncated,
            cell_issues=cell_issues,
            date_readings=date_readings,
            naive_timestamps=dict(run.naive_timestamps),
        )

    def _date_readings(
        self,
        records: list[tuple[int, dict]],
        mapping: ColumnMapping,
        stated: DateOrder | None,
    ) -> tuple[DateReading, ...]:
        """Settle the order of each date column that writes its dates as numbers."""
        readings: list[DateReading] = []
        for name, spec in self._specs.items():
            column = mapping.column_for(name)
            if spec.field_type is not FieldType.DATE or column is None:
                continue
            day_first: str | None = None
            month_first: str | None = None
            undecided: str | None = None
            for _, raw in records:
                token = _clean(raw.get(column))
                match = _NUMERIC_DATE.match(token) if token else None
                if match is None:
                    continue
                first, second = int(match.group(1)), int(match.group(3))
                if first > 12 >= second:
                    day_first = day_first or token
                elif second > 12 >= first:
                    month_first = month_first or token
                elif first != second and first <= 12:
                    undecided = undecided or token
            if not (day_first or month_first or undecided):
                continue
            if stated is not None:
                reading = DateReading(
                    name, column, stated, stated=True, example=day_first or month_first
                )
            elif day_first and month_first:
                reading = DateReading(
                    name,
                    column,
                    None,
                    example=f"{day_first} and {month_first}",
                    problem=DateProblem.CONFLICTING,
                )
            elif day_first:
                reading = DateReading(name, column, DateOrder.DAY_FIRST, example=day_first)
            elif month_first:
                reading = DateReading(name, column, DateOrder.MONTH_FIRST, example=month_first)
            else:
                reading = DateReading(
                    name, column, None, example=undecided, problem=DateProblem.AMBIGUOUS
                )
            readings.append(reading)
        return tuple(readings)

    def _coerce(self, name: str, spec: FieldSpec, column: str, token: str, run: _Run):
        if spec.field_type is FieldType.DATETIME:
            parsed, assumed = read_datetime(token)
            if assumed:
                run.naive_timestamps[column] += 1
            return parsed
        if spec.field_type is FieldType.OPTION_TYPE and token.upper() in self._non_option_tokens:
            return NOT_AN_OPTION
        if spec.field_type is FieldType.DATE and self._resolve_date_order:
            match = _NUMERIC_DATE.match(token)
            if match is not None:
                if match.group(1) == match.group(3):
                    return _numeric_date(token, DateOrder.DAY_FIRST)
                order = run.date_orders.get(name)
                if order is None:
                    raise RowParseError(
                        f"ambiguous date: {token!r} is day-first or month-first and "
                        "this column does not settle which"
                    )
                return _numeric_date(token, order)
        return _COERCERS[spec.field_type](token)

    def _coerce_row(self, raw: dict, mapping: ColumnMapping, run: _Run | None = None) -> dict:
        run = run if run is not None else _Run()
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
                values[name] = self._coerce(name, spec, column, token, run)
            except (RowParseError, ValueError) as exc:
                if self._lenient_optional and not spec.required:
                    run.issues.append((name, column, token, str(exc)))
                    values[name] = None
                    continue
                error = RowParseError(f"column {column!r}: {exc}")
                error.column = column
                raise error from exc
        return values
