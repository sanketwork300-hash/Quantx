"""File layout for option-chain uploads.

``column_mapping.py`` answers "which column holds which field?". This module
answers the question that comes before it: **how are quote records arranged in
the file at all?**

Two layouts occur in real exports.

``LONG``
    One row per quote, with an explicit ``option_type`` column. Every field is
    named by a column header, so a :class:`ColumnMapping` describes the file
    completely. This was the only layout the pipeline understood.

``TWO_SIDED``
    The layout every retail chain export uses, NSE's included: one row per
    strike, call fields in a block of columns to the left of ``STRIKE`` and put
    fields mirrored to the right of it. There is no ``option_type`` column --
    the side is implied by *position* -- and the same header name (``BID``,
    ``ASK``, ``LTP``, ``OI``, ...) appears once on each side.

The duplicated names are why this cannot be bolted onto the mapping model.
``csv.DictReader`` keeps only the last column of a repeated name, so reading an
NSE chain as a flat table silently gives every *call* the *put's* bid, ask and
last price: a complete, plausible, wrong chain with no error anywhere. A layout
has to be resolved by column index before any name-based mapping runs.

Detection is a suggestion, never a decision. :func:`detect` reports what it
found and the evidence it used; the user confirms it in the preview step
exactly as they confirm an inferred column mapping, because the failure mode
here is a wrong answer rather than an error message.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from domains.market_data.ingestion.column_mapping import (
    OPTION_CHAIN_FIELDS,
    ColumnMapping,
    FieldSpec,
    normalize_header,
)

#: Lines searched for a header when the first line is not one. Chain exports put
#: at most a title and a merged "CALLS | PUTS" banner above the real header.
MAX_HEADER_SCAN_ROWS = 8

#: Fields that belong to one side of a two-sided chain. Everything else on such
#: a row (the strike, the underlying price, a timestamp) is shared by both
#: sides, and is copied to each.
SIDE_FIELDS: tuple[str, ...] = (
    "bid_price",
    "ask_price",
    "last_price",
    "bid_size",
    "ask_size",
    "volume",
    "open_interest",
)

#: Fields read once per source row and copied to both emitted quotes.
SHARED_FIELDS: tuple[str, ...] = (
    "strike",
    "underlying_price",
    "exchange_timestamp",
    "symbol",
    "sequence_number",
)

#: A side must carry at least one of these for the layout to be believable. A
#: block of columns with no price in it is not a side of a chain.
PRICE_FIELDS: tuple[str, ...] = ("bid_price", "ask_price", "last_price")

_STRIKE_ALIASES = frozenset(
    normalize_header(alias)
    for spec in OPTION_CHAIN_FIELDS
    if spec.name == "strike"
    for alias in (spec.name, *spec.aliases)
)

_OPTION_TYPE_ALIASES = frozenset(
    normalize_header(alias)
    for spec in OPTION_CHAIN_FIELDS
    if spec.name == "option_type"
    for alias in (spec.name, *spec.aliases)
)


class ChainLayout(StrEnum):
    LONG = "LONG"
    TWO_SIDED = "TWO_SIDED"


class LayoutError(ValueError):
    """The described layout cannot be applied to this file."""


@dataclass(frozen=True, slots=True)
class TwoSidedLayout:
    """Where each side's fields live, by 0-based column index.

    Indices rather than header names, because the names are ambiguous by
    construction: that ambiguity is the whole reason this type exists.

    ``expiry`` is carried here because a chain export names one expiry in its
    filename and repeats it in no column. It is supplied by the caller and is
    never inferred silently; :func:`filename_hints` offers a suggestion for the
    preview to display, and the user confirms it before ingestion.
    """

    header_row: int
    strike_column: int
    call_columns: dict[str, int]
    put_columns: dict[str, int]
    shared_columns: dict[str, int] = field(default_factory=dict)
    expiry: date | None = None

    def __post_init__(self) -> None:
        if self.header_row < 0:
            raise LayoutError("header_row must be non-negative.")
        if self.strike_column < 0:
            raise LayoutError("strike_column must be non-negative.")
        for side, columns in (("call", self.call_columns), ("put", self.put_columns)):
            if not any(name in columns for name in PRICE_FIELDS):
                raise LayoutError(
                    f"The {side} side has no bid, ask or last price column, so no "
                    f"{side} observation could be formed from any row."
                )

    def columns_for(self, side: str) -> dict[str, int]:
        return self.call_columns if side == "CALL" else self.put_columns

    def identity_mapping(self) -> ColumnMapping:
        """The mapping that applies to records this layout emits.

        :func:`split` resolves columns by index and emits records keyed by
        canonical field name, so the name-based mapping that runs afterwards is
        the identity over the fields actually present.
        """
        names = {"strike", "option_type", "expiry"}
        names.update(self.call_columns)
        names.update(self.put_columns)
        names.update(self.shared_columns)
        return ColumnMapping(mapping={name: name for name in sorted(names)})

    def to_dict(self) -> dict:
        return {
            "layout": str(ChainLayout.TWO_SIDED),
            "header_row": self.header_row,
            "strike_column": self.strike_column,
            "call_columns": dict(self.call_columns),
            "put_columns": dict(self.put_columns),
            "shared_columns": dict(self.shared_columns),
            "expiry": self.expiry.isoformat() if self.expiry else None,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> TwoSidedLayout:
        expiry = payload.get("expiry")
        return cls(
            header_row=int(payload["header_row"]),
            strike_column=int(payload["strike_column"]),
            call_columns={str(k): int(v) for k, v in dict(payload["call_columns"]).items()},
            put_columns={str(k): int(v) for k, v in dict(payload["put_columns"]).items()},
            shared_columns={
                str(k): int(v) for k, v in dict(payload.get("shared_columns") or {}).items()
            },
            expiry=date.fromisoformat(expiry) if expiry else None,
        )


@dataclass(frozen=True, slots=True)
class LayoutDetection:
    """What the file looks like, and why. Shown to the user, never applied blind."""

    layout: ChainLayout
    headers: tuple[str, ...]
    two_sided: TwoSidedLayout | None = None
    evidence: tuple[str, ...] = ()
    unmapped_columns: tuple[str, ...] = ()
    suggested_expiry: date | None = None
    suggested_symbol: str | None = None
    suggestion_source: str | None = None

    def to_dict(self) -> dict:
        return {
            "layout": str(self.layout),
            "headers": list(self.headers),
            "two_sided": self.two_sided.to_dict() if self.two_sided else None,
            "evidence": list(self.evidence),
            "unmapped_columns": list(self.unmapped_columns),
            "suggested_expiry": (
                self.suggested_expiry.isoformat() if self.suggested_expiry else None
            ),
            "suggested_symbol": self.suggested_symbol,
            "suggestion_source": self.suggestion_source,
        }


def read_rows(data: bytes, limit: int | None = None) -> list[list[str]]:
    """Physical rows of the file, as raw cells.

    ``csv`` handles the quoting that makes ``"1,877.00"`` one cell rather than
    two, and the CRLF line endings that exports carry.

    ``limit`` stops the reader early. Detection needs only the first few rows,
    and a 500,000-row chain should not be parsed twice to find out where its
    header is.
    """
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    if limit is None:
        return list(reader)
    return [row for _, row in zip(range(limit), reader, strict=False)]


def _index_aliases(specs: tuple[FieldSpec, ...]) -> dict[str, str]:
    """Normalised header token -> canonical field name."""
    index: dict[str, str] = {}
    for spec in specs:
        for alias in (spec.name, *spec.aliases):
            index.setdefault(normalize_header(alias), spec.name)
    return index


def _match_block(
    headers: list[str], span: range, alias_index: dict[str, str], wanted: tuple[str, ...]
) -> dict[str, int]:
    """Resolve one column block, first match wins so a mirrored side keeps order."""
    resolved: dict[str, int] = {}
    for position in span:
        name = alias_index.get(normalize_header(headers[position]))
        if name in wanted and name not in resolved:
            resolved[name] = position
    return resolved


def detect(data: bytes, filename: str | None = None) -> LayoutDetection:
    """Identify the layout of a chain export.

    A file is read as ``TWO_SIDED`` only when a header row carries a strike
    column with a priced block on each side of it and no ``option_type`` column
    anywhere. Anything else stays ``LONG`` and is described by an ordinary
    column mapping, so a file the old path handled keeps being handled the same
    way.
    """
    # One row past the scan window, so a header on the last scanned line still
    # has a row beneath it to be the header *of*.
    rows = read_rows(data, limit=MAX_HEADER_SCAN_ROWS + 1)
    if not rows:
        return LayoutDetection(layout=ChainLayout.LONG, headers=())

    alias_index = _index_aliases(OPTION_CHAIN_FIELDS)
    hint_expiry, hint_symbol = filename_hints(filename)

    for header_row in range(min(MAX_HEADER_SCAN_ROWS, len(rows) - 1)):
        headers = [cell.strip() for cell in rows[header_row]]
        normalized = [normalize_header(cell) for cell in headers]

        if any(token in _OPTION_TYPE_ALIASES for token in normalized):
            # The side is named in a column, so the file is already long-form
            # even if it also happens to be wide.
            break

        strikes = [i for i, token in enumerate(normalized) if token in _STRIKE_ALIASES]
        if len(strikes) != 1:
            continue
        strike_column = strikes[0]

        calls = _match_block(headers, range(strike_column), alias_index, SIDE_FIELDS)
        puts = _match_block(
            headers, range(strike_column + 1, len(headers)), alias_index, SIDE_FIELDS
        )
        if not any(name in calls for name in PRICE_FIELDS):
            continue
        if not any(name in puts for name in PRICE_FIELDS):
            continue

        shared = _match_block(
            headers, range(len(headers)), alias_index, tuple(n for n in SHARED_FIELDS)
        )
        shared.pop("strike", None)
        for taken in (*calls.values(), *puts.values()):
            shared = {k: v for k, v in shared.items() if v != taken}

        try:
            two_sided = TwoSidedLayout(
                header_row=header_row,
                strike_column=strike_column,
                call_columns=calls,
                put_columns=puts,
                shared_columns=shared,
                expiry=None,
            )
        except LayoutError:
            continue

        claimed = {strike_column, *calls.values(), *puts.values(), *shared.values()}
        unmapped = tuple(headers[i] for i in range(len(headers)) if i not in claimed and headers[i])
        repeated = sorted(
            {
                headers[calls[name]]
                for name in calls
                if name in puts and headers[calls[name]] == headers[puts[name]]
            }
        )
        evidence = [
            f"Row {header_row + 1} is the header: it names a strike column and no option type.",
            f"Column {strike_column} ({headers[strike_column]!r}) separates the two sides.",
            f"{len(calls)} call field(s) to its left, {len(puts)} put field(s) to its right.",
        ]
        if header_row:
            skipped = ", ".join(repr(cell) for cell in rows[header_row - 1] if cell.strip())
            evidence.append(f"Row {header_row} was a banner, not data: {skipped}.")
        if repeated:
            evidence.append(
                "Header name(s) appear once per side and cannot be told apart by "
                f"name: {', '.join(repeated)}."
            )
        return LayoutDetection(
            layout=ChainLayout.TWO_SIDED,
            headers=tuple(headers),
            two_sided=two_sided,
            evidence=tuple(evidence),
            unmapped_columns=unmapped,
            suggested_expiry=hint_expiry,
            suggested_symbol=hint_symbol,
            suggestion_source="filename" if (hint_expiry or hint_symbol) else None,
        )

    headers = tuple(cell.strip() for cell in rows[0])
    return LayoutDetection(
        layout=ChainLayout.LONG,
        headers=headers,
        evidence=("One row per quote; every field is named by a column header.",),
        suggested_expiry=hint_expiry,
        suggested_symbol=hint_symbol,
        suggestion_source="filename" if (hint_expiry or hint_symbol) else None,
    )


def split(data: bytes, layout: TwoSidedLayout) -> tuple[list[tuple[int, dict]], list[str]]:
    """Rewrite a two-sided file as one record per quote.

    Returns the records paired with their **source** row number -- the line the
    user sees in their own spreadsheet -- and the header row. Both quotes from a
    strike carry the same source row number, because they came from the same
    line and telling the user otherwise would send them to the wrong row.

    Every source row emits both sides unconditionally. A side that was not
    quoted is left for the validator to reject as ``NO_PRICE_FIELDS`` rather
    than dropped here: one decision point, one reason, and the row accounting
    stays exact.
    """
    rows = read_rows(data)
    if layout.header_row >= len(rows):
        raise LayoutError(
            f"The file has {len(rows)} row(s); row {layout.header_row + 1} was named as the header."
        )
    headers = [cell.strip() for cell in rows[layout.header_row]]

    records: list[tuple[int, dict]] = []
    for offset, row in enumerate(rows[layout.header_row + 1 :], start=1):
        if not any(cell.strip() for cell in row):
            # A trailing blank line is not a row the user wrote.
            continue
        shared: dict[str, str | None] = {
            "strike": _cell(row, layout.strike_column),
            "expiry": layout.expiry.isoformat() if layout.expiry else None,
        }
        for name, index in layout.shared_columns.items():
            shared[name] = _cell(row, index)

        for side in ("CALL", "PUT"):
            record = dict(shared)
            record["option_type"] = side
            for name, index in layout.columns_for(side).items():
                record[name] = _cell(row, index)
            records.append((offset, record))

    return records, headers


def _cell(row: list[str], index: int) -> str | None:
    """A cell, or ``None`` when the row is short. Never an empty-string stand-in."""
    if index >= len(row):
        return None
    return row[index]


_FILENAME_DATE = re.compile(
    r"(\d{1,2}[-_ ][A-Za-z]{3,9}[-_ ]\d{4}|\d{4}-\d{2}-\d{2})",
)
_FILENAME_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d")


def filename_hints(filename: str | None) -> tuple[date | None, str | None]:
    """Expiry and symbol a chain export puts in its filename and in no column.

    A *suggestion*, offered to the preview and confirmed by the user. Nothing
    here is applied to an ingestion on its own: a wrong expiry silently moves
    every contract along the term structure, which is precisely the class of
    guess this platform does not make.
    """
    if not filename:
        return None, None
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    match = _FILENAME_DATE.search(stem)
    if match is None:
        return None, None

    token = match.group(1).replace("_", "-").replace(" ", "-")
    parsed: date | None = None
    for fmt in _FILENAME_DATE_FORMATS:
        try:
            parsed = datetime.strptime(token, fmt).date()
        except ValueError:
            continue
        break

    words = [word for word in re.split(r"[-_ ]+", stem[: match.start()]) if word]
    symbol = words[-1].upper() if words and words[-1].isalpha() else None
    return parsed, symbol
