"""Column mapping for user-supplied tabular data.

Real chain exports name the same column ``STRIKE``, ``Strike Price``,
``strike_price`` or ``K``. Inference handles the common cases so the preview is
useful immediately, but inference is only ever a *suggestion*: the user confirms
the mapping in the preview step before anything is committed, because a
misinterpreted column silently produces a plausible, wrong chain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class FieldType(StrEnum):
    STRING = "STRING"
    DECIMAL = "DECIMAL"
    INTEGER = "INTEGER"
    DATE = "DATE"
    DATETIME = "DATETIME"
    OPTION_TYPE = "OPTION_TYPE"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    field_type: FieldType
    required: bool
    aliases: tuple[str, ...]
    description: str


OPTION_CHAIN_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "strike",
        FieldType.DECIMAL,
        True,
        ("strike", "strikeprice", "strike_price", "k", "exerciseprice"),
        "Contract strike price.",
    ),
    FieldSpec(
        "option_type",
        FieldType.OPTION_TYPE,
        True,
        (
            "optiontype",
            "option_type",
            "type",
            "cp",
            "cpflag",
            "cepe",
            "pece",
            "callput",
            "right",
            "instrumenttype",
        ),
        "CALL/PUT, C/P or CE/PE.",
    ),
    FieldSpec(
        "expiry",
        FieldType.DATE,
        True,
        (
            "expiry",
            "expirydate",
            "expiry_date",
            "expirydt",
            "expdate",
            "expdt",
            "exp",
            "expiration",
            "maturity",
        ),
        "Contract expiry date.",
    ),
    FieldSpec(
        "bid_price",
        FieldType.DECIMAL,
        False,
        ("bid", "bidprice", "bid_price", "bestbid"),
        "Best bid price.",
    ),
    FieldSpec(
        "ask_price",
        FieldType.DECIMAL,
        False,
        ("ask", "askprice", "ask_price", "offer", "bestask"),
        "Best ask price.",
    ),
    FieldSpec(
        "last_price",
        FieldType.DECIMAL,
        False,
        ("last", "lastprice", "ltp", "close", "price"),
        "Last traded price.",
    ),
    FieldSpec(
        "bid_size",
        FieldType.DECIMAL,
        False,
        ("bidsize", "bidqty", "bidquantity", "bid_size"),
        "Quantity resting at the best bid.",
    ),
    FieldSpec(
        "ask_size",
        FieldType.DECIMAL,
        False,
        ("asksize", "askqty", "askquantity", "ask_size"),
        "Quantity resting at the best ask.",
    ),
    FieldSpec(
        "volume",
        FieldType.DECIMAL,
        False,
        ("volume", "vol", "tradedvolume", "totaltradedvolume"),
        "Session volume.",
    ),
    FieldSpec(
        "open_interest",
        FieldType.DECIMAL,
        False,
        ("openinterest", "oi", "open_interest"),
        "Open interest.",
    ),
    FieldSpec(
        "underlying_price",
        FieldType.DECIMAL,
        False,
        ("underlying", "underlyingprice", "spot", "spotprice", "underlyingvalue"),
        "Underlying price observed with the chain.",
    ),
    FieldSpec(
        "exchange_timestamp",
        FieldType.DATETIME,
        False,
        ("timestamp", "exchangetimestamp", "datetime", "time", "quotetime"),
        "Venue event time. Defaults to the snapshot as_of when absent.",
    ),
    FieldSpec(
        "symbol",
        FieldType.STRING,
        False,
        ("symbol", "underlyingsymbol", "ticker", "name", "root"),
        "Underlying symbol, if the file mixes underlyings.",
    ),
    FieldSpec(
        "sequence_number",
        FieldType.INTEGER,
        False,
        ("sequence", "seq", "seqno", "sequencenumber"),
        "Venue sequence number.",
    ),
)

FIELDS_BY_KIND: dict[str, tuple[FieldSpec, ...]] = {"OPTION_CHAIN": OPTION_CHAIN_FIELDS}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


#: Columns a historical OHLCV file carries. The same ``FieldSpec`` machinery the
#: option-chain reader uses, so header inference, coercion and the "a partial
#: mapping is an instruction" rule all behave identically for bars — one reader,
#: one set of conventions, one place a alias list is maintained.
BAR_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "exchange_timestamp",
        FieldType.DATETIME,
        True,
        ("timestamp", "datetime", "date", "time", "bartime", "start", "starttime", "dt", "ts"),
        "The bar's opening instant. Must carry a UTC offset.",
    ),
    FieldSpec(
        "open",
        FieldType.DECIMAL,
        True,
        ("open", "o", "openprice", "opening"),
        "Opening price.",
    ),
    FieldSpec("high", FieldType.DECIMAL, True, ("high", "h", "highprice", "max"), "Session high."),
    FieldSpec("low", FieldType.DECIMAL, True, ("low", "l", "lowprice", "min"), "Session low."),
    FieldSpec(
        "close",
        FieldType.DECIMAL,
        True,
        ("close", "c", "closeprice", "last", "closing", "adjclose", "adjustedclose"),
        "Closing price.",
    ),
    FieldSpec(
        "volume",
        FieldType.DECIMAL,
        True,
        ("volume", "v", "vol", "quantity", "qty", "tradedquantity"),
        "Traded volume over the bar.",
    ),
    FieldSpec(
        "symbol",
        FieldType.STRING,
        False,
        ("symbol", "ticker", "tradingsymbol", "instrument", "name", "scrip"),
        "Instrument symbol, resolved against the instrument master.",
    ),
    FieldSpec(
        "vwap",
        FieldType.DECIMAL,
        False,
        ("vwap", "averageprice", "avgprice", "typicalprice"),
        "Volume-weighted average price, only where the venue publishes it.",
    ),
    FieldSpec(
        "trade_count",
        FieldType.INTEGER,
        False,
        ("trades", "tradecount", "numtrades", "ntrades", "count"),
        "Number of trades in the bar.",
    ),
    FieldSpec(
        "end_timestamp",
        FieldType.DATETIME,
        False,
        ("endtimestamp", "end", "endtime", "closetime"),
        "The bar's closing instant, where the source states it.",
    ),
)


def normalize_header(header: str) -> str:
    return _NON_ALNUM.sub("", header.strip().lower())


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    """Canonical field name -> source column header."""

    mapping: dict[str, str] = field(default_factory=dict)

    def column_for(self, field_name: str) -> str | None:
        return self.mapping.get(field_name)

    def missing_required(self, specs: tuple[FieldSpec, ...]) -> tuple[str, ...]:
        return tuple(spec.name for spec in specs if spec.required and spec.name not in self.mapping)

    def unmapped_columns(self, headers: list[str]) -> tuple[str, ...]:
        used = set(self.mapping.values())
        return tuple(header for header in headers if header not in used)

    def to_dict(self) -> dict[str, str]:
        return dict(self.mapping)


def infer_mapping(headers: list[str], specs: tuple[FieldSpec, ...]) -> ColumnMapping:
    """Best-effort header inference. Always shown to the user before commit."""
    normalized = {normalize_header(header): header for header in headers}
    mapping: dict[str, str] = {}
    for spec in specs:
        for alias in (spec.name, *spec.aliases):
            candidate = normalized.get(normalize_header(alias))
            if candidate is not None and candidate not in mapping.values():
                mapping[spec.name] = candidate
                break
    return ColumnMapping(mapping=mapping)
