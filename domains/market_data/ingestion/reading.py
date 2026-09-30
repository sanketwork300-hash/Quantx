"""What a file was read as, reported field by field and row by row.

The failure this module exists to prevent is specific: a chain read with the
wrong column for a field produces a *plausible* chain and no error anywhere.
Nothing crashes, a snapshot appears, every downstream analysis runs, and the
numbers are wrong. So the reading itself is a first-class result:

* :func:`describe` says which column each field was read from, and whether that
  column was worked out from the file or named by the caller. A field that is
  not in the file at all says so, rather than being absent from the report.
* :func:`sample` shows the file's first rows *in file order*, including the ones
  that could not be read. A sample of only the successes is the misleading
  case: it looks perfect however badly the file was read.
* :func:`assess` decides whether the reading worked at all. A file that mostly
  could not be read is refused rather than ingested into a near-empty snapshot,
  because an empty snapshot is indistinguishable from a quiet market. A *sample*
  is held to less than the file: its rows being empty says where the file
  starts, not how it was read.

The distinction :func:`assess` turns on is between a row that could not be read
and a row that has nothing in it. A two-sided exchange export carries blank call
or put prices at far strikes as a matter of course, and a chain with no expiry
column is a different thing entirely from a chain whose far wings are quiet. So
only *structural* rejections -- the ones that say a column does not hold what it
was taken to hold -- count against the reading.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from domains.market_data.ingestion.column_mapping import ColumnMapping, FieldSpec
from domains.market_data.ingestion.layout import TwoSidedLayout
from domains.market_data.ingestion.parser import DateProblem, ParseResult
from domains.market_data.ingestion.validator import (
    OptionChainRowValidator,
    RejectedRow,
    RejectionReason,
)

#: Rejections that say the reading is wrong rather than the row is empty. Each
#: one means the column taken for a field does not hold that field: it holds
#: text where a number belongs, or nothing at all where every row of a real
#: chain carries a value.
STRUCTURAL_REJECTIONS: frozenset[RejectionReason] = frozenset(
    {
        RejectionReason.UNPARSEABLE_ROW,
        RejectionReason.MISSING_STRIKE,
        RejectionReason.NON_POSITIVE_STRIKE,
        RejectionReason.MISSING_EXPIRY,
        RejectionReason.MISSING_OPTION_TYPE,
    }
)

#: The share of examined rows that may fail structurally before the reading
#: itself is held to be wrong. A majority, deliberately: misreadings are
#: overwhelmingly all-or-nothing -- a column either holds expiries or it does
#: not -- while genuinely dirty data is a minority of rows in an otherwise
#: readable file. The rule is stated in the refusal message so a user who
#: disagrees can see what was applied.
UNREADABLE_MAJORITY = 0.5


class ReadingSource(StrEnum):
    """Where a field's column came from. Every field reports one."""

    #: A column whose header matched this field, worked out from the file.
    DETECTED_COLUMN = "DETECTED_COLUMN"
    #: A column the caller named for this field.
    SUPPLIED_COLUMN = "SUPPLIED_COLUMN"
    #: Not read from a column: the side of a two-sided export is the block the
    #: column sits in, left or right of the strike.
    IMPLIED_BY_POSITION = "IMPLIED_BY_POSITION"
    #: Not in the file at all; stated alongside it. A two-sided export names one
    #: expiry in its filename and repeats it in no column.
    STATED_SEPARATELY = "STATED_SEPARATELY"
    #: No column in the file holds this field.
    NOT_IN_FILE = "NOT_IN_FILE"


class Side(StrEnum):
    BOTH = "BOTH"
    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """One column a field is read from, named the way the file names it."""

    side: Side
    header: str | None
    index: int | None = None

    def to_dict(self) -> dict:
        return {"side": str(self.side), "header": self.header, "index": self.index}


@dataclass(frozen=True, slots=True)
class FieldReading:
    """Which column a field was read from, and whether that was said or worked out."""

    field: str
    required: bool
    source: ReadingSource
    columns: tuple[ColumnRef, ...] = ()
    #: Present when the column alone does not explain the reading -- a stated
    #: expiry, a side implied by position, a field absent from the file.
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "required": self.required,
            "source": str(self.source),
            "columns": [column.to_dict() for column in self.columns],
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SampleRow:
    """One source row as it was read, whether or not the reading worked."""

    row_number: int
    read: bool
    values: dict[str, str | None] = field(default_factory=dict)
    #: Why the row produced no quote. ``None`` for a row that was read.
    problem: str | None = None
    reason: str | None = None
    #: Set when the row was refused for a reason that says the *reading* is
    #: wrong rather than the row is empty. Drives the verdict below.
    structural: bool = False

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "read": self.read,
            "values": self.values,
            "problem": self.problem,
            "reason": self.reason,
            "structural": self.structural,
        }


class ReadingProblem(StrEnum):
    FILE_HAS_NO_DATA_ROWS = "FILE_HAS_NO_DATA_ROWS"
    REQUIRED_FIELD_NOT_FOUND = "REQUIRED_FIELD_NOT_FOUND"
    #: A field is read from a header name the file uses more than once.
    AMBIGUOUS_COLUMN = "AMBIGUOUS_COLUMN"
    #: A date column does not say whether the day or the month comes first.
    AMBIGUOUS_DATE_ORDER = "AMBIGUOUS_DATE_ORDER"
    NO_ROW_COULD_BE_READ = "NO_ROW_COULD_BE_READ"
    MOST_ROWS_COULD_NOT_BE_READ = "MOST_ROWS_COULD_NOT_BE_READ"


@dataclass(frozen=True, slots=True)
class ReadingVerdict:
    """Whether the file was read, with the counts that decided it.

    ``rows_examined == rows_read + rows_unreadable + rows_empty`` always holds:
    the same conservation rule the ingestion summary obeys, applied to the
    reading, so a row is never quietly missing from the arithmetic.
    """

    readable: bool
    rows_examined: int
    rows_read: int
    rows_unreadable: int
    rows_empty: int
    problem: ReadingProblem | None = None
    message: str | None = None
    #: Rejection reason -> count, over the rows that could not be read.
    reasons: dict[str, int] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()
    #: Lines of the file the counts above came from. A two-sided export yields
    #: two quotes per line, so the counts are quotes and this is what the user
    #: can find in their spreadsheet. ``None`` when the two are the same thing.
    source_rows: int | None = None

    def to_dict(self) -> dict:
        return {
            "readable": self.readable,
            "rows_examined": self.rows_examined,
            "rows_read": self.rows_read,
            "rows_unreadable": self.rows_unreadable,
            "rows_empty": self.rows_empty,
            "problem": str(self.problem) if self.problem is not None else None,
            "message": self.message,
            "reasons": dict(self.reasons),
            "missing_required": list(self.missing_required),
            "source_rows": self.source_rows,
        }


class ReadingRefused(ValueError):
    """The file could not be read, so nothing was written.

    Raised instead of persisting a snapshot that would be empty or mostly
    empty. A snapshot is an observation record, and an observation record that
    says "almost nothing was here" because the file was read wrongly is worse
    than no record at all: every later analysis takes it at face value.
    """

    def __init__(self, verdict: ReadingVerdict, reading: Sequence[FieldReading] = ()) -> None:
        super().__init__(verdict.message or "The file could not be read.")
        self.code = str(verdict.problem) if verdict.problem is not None else "FILE_NOT_READ"
        self.verdict = verdict
        self.reading = tuple(reading)

    @property
    def details(self) -> dict:
        """Structured diagnosis, carried into the job's error payload."""
        return {
            "code": self.code,
            "verdict": self.verdict.to_dict(),
            "reading": [item.to_dict() for item in self.reading],
        }


def describe(
    specs: tuple[FieldSpec, ...],
    applied: ColumnMapping,
    headers: Sequence[str],
    *,
    supplied: ColumnMapping | None = None,
    layout: TwoSidedLayout | None = None,
    detected_layout: TwoSidedLayout | None = None,
    expiry_source: str | None = None,
) -> tuple[FieldReading, ...]:
    """Say, field by field, which column the file was read from.

    ``supplied`` is what the caller asked for and ``applied`` is what was used;
    the two differ exactly where a reading was worked out rather than stated,
    and the difference is what the report shows. A caller who corrected one
    column should see that one column marked as theirs and the rest still
    marked as detected.
    """
    if layout is not None:
        return _describe_two_sided(
            specs, headers, layout, detected=detected_layout, expiry_source=expiry_source
        )
    return _describe_long(specs, applied, headers, supplied=supplied)


def _describe_long(
    specs: tuple[FieldSpec, ...],
    applied: ColumnMapping,
    headers: Sequence[str],
    *,
    supplied: ColumnMapping | None,
) -> tuple[FieldReading, ...]:
    named = supplied.to_dict() if supplied is not None else {}
    index_of = {header: position for position, header in enumerate(headers)}
    readings: list[FieldReading] = []
    for spec in specs:
        column = applied.column_for(spec.name)
        if column is None:
            readings.append(
                FieldReading(
                    field=spec.name,
                    required=spec.required,
                    source=ReadingSource.NOT_IN_FILE,
                    detail=(
                        "No column in this file was matched to this field."
                        if spec.required
                        else "No column in this file carries it, so it is not read."
                    ),
                )
            )
            continue
        source = (
            ReadingSource.SUPPLIED_COLUMN
            if named.get(spec.name) == column
            else ReadingSource.DETECTED_COLUMN
        )
        readings.append(
            FieldReading(
                field=spec.name,
                required=spec.required,
                source=source,
                columns=(ColumnRef(Side.BOTH, column, index_of.get(column)),),
            )
        )
    return tuple(readings)


def _describe_two_sided(
    specs: tuple[FieldSpec, ...],
    headers: Sequence[str],
    layout: TwoSidedLayout,
    *,
    detected: TwoSidedLayout | None,
    expiry_source: str | None,
) -> tuple[FieldReading, ...]:
    def attribute(*, side: str, name: str, index: int) -> ReadingSource:
        """Detected where the applied column is the one the file suggested.

        Compared column by column rather than layout by layout, so a user who
        corrects one column sees that column marked as theirs and the rest still
        marked as the platform's reading.
        """
        if detected is None:
            return ReadingSource.SUPPLIED_COLUMN
        if side == "STRIKE":
            found = detected.strike_column
        elif side == "CALL":
            found = detected.call_columns.get(name)
        elif side == "PUT":
            found = detected.put_columns.get(name)
        else:
            found = detected.shared_columns.get(name)
        return ReadingSource.DETECTED_COLUMN if found == index else ReadingSource.SUPPLIED_COLUMN

    def header_at(index: int) -> str | None:
        return headers[index] if 0 <= index < len(headers) else None

    if layout.expiry is None:
        expiry_detail = "The file names no expiry column and none was supplied."
    else:
        origin = f"suggested from the {expiry_source}" if expiry_source else "as supplied"
        expiry_detail = (
            f"The file names no expiry column. Every contract is dated "
            f"{layout.expiry.isoformat()}, {origin}. If that is wrong, every contract "
            f"sits at the wrong point of the term structure."
        )

    readings: list[FieldReading] = [
        FieldReading(
            field="strike",
            required=True,
            source=attribute(side="STRIKE", name="strike", index=layout.strike_column),
            columns=(ColumnRef(Side.BOTH, header_at(layout.strike_column), layout.strike_column),),
            detail=(
                "One row per strike: the columns left of it are the calls, right of it the puts."
            ),
        ),
        FieldReading(
            field="option_type",
            required=True,
            source=ReadingSource.IMPLIED_BY_POSITION,
            detail=(
                "No column says call or put. The side is which block of the row a "
                "price sits in, so each source row becomes one call quote and one "
                "put quote."
            ),
        ),
        FieldReading(
            field="expiry",
            required=True,
            source=(
                ReadingSource.STATED_SEPARATELY
                if layout.expiry is not None
                else ReadingSource.NOT_IN_FILE
            ),
            detail=expiry_detail,
        ),
    ]

    named = {spec.name for spec in specs}
    for name in sorted(
        set(layout.call_columns) | set(layout.put_columns) | set(layout.shared_columns)
    ):
        if name in {"strike", "option_type", "expiry"} or name not in named:
            continue
        columns: list[ColumnRef] = []
        shared = layout.shared_columns.get(name)
        if shared is not None:
            columns.append(ColumnRef(Side.BOTH, header_at(shared), shared))
        call = layout.call_columns.get(name)
        if call is not None:
            columns.append(ColumnRef(Side.CALL, header_at(call), call))
        put = layout.put_columns.get(name)
        if put is not None:
            columns.append(ColumnRef(Side.PUT, header_at(put), put))
        spec = next(item for item in specs if item.name == name)
        sources = {
            attribute(side=str(column.side), name=name, index=column.index)
            for column in columns
            if column.index is not None
        }
        readings.append(
            FieldReading(
                field=name,
                required=spec.required,
                # One column corrected makes the field the caller's: the report
                # must not imply the platform chose a column the caller replaced.
                source=(
                    ReadingSource.SUPPLIED_COLUMN
                    if ReadingSource.SUPPLIED_COLUMN in sources
                    else ReadingSource.DETECTED_COLUMN
                ),
                columns=tuple(columns),
            )
        )

    read = {item.field for item in readings}
    for spec in specs:
        if spec.name in read:
            continue
        readings.append(
            FieldReading(
                field=spec.name,
                required=spec.required,
                source=ReadingSource.NOT_IN_FILE,
                detail="No column on either side of this file carries it.",
            )
        )
    return tuple(readings)


def obstacle(result: ParseResult) -> tuple[ReadingProblem, str] | None:
    """A reason the file cannot be read one way, whatever its rows hold.

    These are not counted in rows, because they are not about rows. A header
    name that occurs twice gives a field two columns to come from, and a column
    of dates like ``03/04/2026`` gives every date two readings. Either way a
    choice would have to be made for the user, the result would look entirely
    plausible, and nothing downstream could tell it was a choice. So nothing is
    chosen: the file is refused and the refusal says what would settle it.
    """
    if result.duplicate_headers:
        names = ", ".join(repr(name) for name in result.duplicate_headers)
        return (
            ReadingProblem.AMBIGUOUS_COLUMN,
            f"The header names {names} more than once and a field is read from a column "
            "of that name, so which column was meant cannot be told from the name. "
            "Nothing was ingested. Rename one of them in the file and upload it again.",
        )
    for reading in result.date_readings:
        if reading.problem is DateProblem.AMBIGUOUS:
            return (
                ReadingProblem.AMBIGUOUS_DATE_ORDER,
                f"Column {reading.column!r} writes its dates as numbers and no value in it "
                f"says whether the day or the month comes first: {reading.example!r} reads "
                "either way. Nothing was ingested rather than one being picked. State the "
                "date order (DMY or MDY) and try again.",
            )
        if reading.problem is DateProblem.CONFLICTING:
            return (
                ReadingProblem.AMBIGUOUS_DATE_ORDER,
                f"Column {reading.column!r} holds dates that can only be day-first and dates "
                f"that can only be month-first ({reading.example}), so it does not read as "
                "one column of dates. Nothing was ingested. State the date order (DMY or "
                "MDY) to read it one way; the values that do not fit are then refused row "
                "by row.",
            )
    return None


def _render(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def sample(
    result: ParseResult, *, validator: OptionChainRowValidator | None = None
) -> tuple[SampleRow, ...]:
    """The file's first rows as they were read, failures included and in order.

    A sample built only from the rows that parsed is the misleading artefact
    this replaces: it looks correct no matter how badly the file was read,
    because the rows that prove otherwise are exactly the ones it omits.
    """
    check = validator or OptionChainRowValidator()
    rows: list[SampleRow] = [
        SampleRow(
            row_number=error.row_number,
            read=False,
            values={},
            problem=error.message,
            reason=str(RejectionReason.UNPARSEABLE_ROW),
            structural=True,
        )
        for error in result.errors
    ]
    for parsed in result.rows:
        outcome = check.validate(parsed)
        values = {name: _render(value) for name, value in parsed.values.items()}
        if isinstance(outcome, RejectedRow):
            rows.append(
                SampleRow(
                    row_number=parsed.row_number,
                    read=False,
                    values=values,
                    problem=outcome.message,
                    reason=str(outcome.reason),
                    structural=outcome.reason in STRUCTURAL_REJECTIONS,
                )
            )
        else:
            rows.append(SampleRow(row_number=parsed.row_number, read=True, values=values))

    rows.sort(key=lambda row: row.row_number)
    return tuple(rows)


def assess(
    *,
    rows_examined: int,
    rows_read: int,
    rows_unreadable: int,
    rows_empty: int,
    reasons: dict[str, int] | None = None,
    missing_required: Sequence[str] = (),
    whole_file: bool = True,
    source_rows: int | None = None,
    obstacle: tuple[ReadingProblem, str] | None = None,
) -> ReadingVerdict:
    """Decide whether the file was read, from the counts the reading produced.

    ``whole_file`` distinguishes the commit path, which has seen every row, from
    the preview, which has seen a sample. The structural rules decide both the
    same way. The one rule a sample cannot apply is "nothing became a quote":
    an exchange chain opens on its far strikes, where neither side is quoted,
    so a sample of nothing but empty rows is where the file starts rather than
    a reading that failed. Refusing it turned away files the commit path reads
    without complaint. Only the whole file can say nothing was there.
    """
    counts = dict(reasons or {})
    lines = source_rows if source_rows is not None else rows_examined
    scope = "file" if whole_file else f"first {lines} row(s)"
    examined = (
        f"{rows_examined} row(s) in the {scope}"
        if lines == rows_examined
        else f"{rows_examined} quote(s) taken from the {scope}"
    )
    mostly_unreadable = rows_unreadable > rows_examined * UNREADABLE_MAJORITY

    def refuse(problem: ReadingProblem, message: str) -> ReadingVerdict:
        return ReadingVerdict(
            readable=False,
            rows_examined=rows_examined,
            rows_read=rows_read,
            rows_unreadable=rows_unreadable,
            rows_empty=rows_empty,
            problem=problem,
            message=message,
            reasons=counts,
            missing_required=tuple(missing_required),
            source_rows=source_rows,
        )

    if missing_required:
        return refuse(
            ReadingProblem.REQUIRED_FIELD_NOT_FOUND,
            "No column in this file was matched to the required field(s): "
            f"{', '.join(missing_required)}. Correct the mapping and try again.",
        )
    if obstacle is not None:
        return refuse(*obstacle)
    if rows_examined == 0:
        return refuse(
            ReadingProblem.FILE_HAS_NO_DATA_ROWS,
            "The file carries a header and no data rows, so there is nothing to read.",
        )
    if rows_read == 0 and (whole_file or mostly_unreadable):
        return refuse(
            ReadingProblem.NO_ROW_COULD_BE_READ,
            f"Not one row of the {scope} could become a quote, so nothing was "
            "ingested. This is a reading that did not work rather than a market "
            f"with nothing in it. Reasons: {_reasons(counts)}.",
        )
    if mostly_unreadable:
        return refuse(
            ReadingProblem.MOST_ROWS_COULD_NOT_BE_READ,
            f"{rows_unreadable} of the {examined} could not "
            "be read at all, which is more than half, so the columns were taken for "
            "the wrong fields rather than the data being dirty. Nothing was ingested. "
            f"Reasons: {_reasons(counts)}.",
        )
    return ReadingVerdict(
        readable=True,
        rows_examined=rows_examined,
        rows_read=rows_read,
        rows_unreadable=rows_unreadable,
        rows_empty=rows_empty,
        # Said rather than left for the counts to imply: a sample with nothing
        # in it is not a verdict on the file.
        message=(
            f"No row in the {scope} carries a price, which is how an exchange chain "
            "opens at its far strikes. Whether the file holds any quote at all is "
            "decided over the whole file when it is ingested."
            if rows_read == 0
            else None
        ),
        reasons=counts,
        source_rows=source_rows,
    )


def assess_rejections(
    rows_read: int,
    rejections: Sequence[RejectedRow],
    *,
    missing_required: Sequence[str] = (),
    obstacle: tuple[ReadingProblem, str] | None = None,
) -> ReadingVerdict:
    """Assess a whole file from the rows it produced and the rows it refused."""
    unreadable = sum(1 for row in rejections if row.reason in STRUCTURAL_REJECTIONS)
    return assess(
        rows_examined=rows_read + len(rejections),
        rows_read=rows_read,
        rows_unreadable=unreadable,
        rows_empty=len(rejections) - unreadable,
        reasons=dict(Counter(str(row.reason) for row in rejections)),
        missing_required=missing_required,
        whole_file=True,
        obstacle=obstacle,
    )


def assess_sample(
    rows: Sequence[SampleRow],
    *,
    missing_required: Sequence[str] = (),
    obstacle: tuple[ReadingProblem, str] | None = None,
) -> ReadingVerdict:
    """Assess the sample the preview shows, by exactly the same rule."""
    return assess(
        rows_examined=len(rows),
        rows_read=sum(1 for row in rows if row.read),
        rows_unreadable=sum(1 for row in rows if not row.read and row.structural),
        rows_empty=sum(1 for row in rows if not row.read and not row.structural),
        reasons=dict(Counter(row.reason for row in rows if not row.read and row.reason)),
        missing_required=missing_required,
        whole_file=False,
        source_rows=len({row.row_number for row in rows}),
        obstacle=obstacle,
    )


def _reasons(reasons: dict[str, int]) -> str:
    if not reasons:
        return "none recorded"
    return ", ".join(f"{code} x{count}" for code, count in sorted(reasons.items()))
