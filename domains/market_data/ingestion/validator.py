"""Row-level domain validation for option-chain ingestion.

Parsing (``parser.py``) answers "is this cell the type it claims to be?".
Validation answers "can this row become an option quote at all?".

The distinction matters for the row accounting the pipeline reports:

* **rejected** — the row could not be parsed or could not become a quote. It has
  no instrument and no quote row; it is reported with its source row number and
  a reason.
* **excluded** — the row *did* become a quote, was scored, and was set aside by
  the exclusion policy. It is persisted in full with its reason and all flags.

Nothing is silently dropped in either case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from domains.instruments.enums import OptionType
from domains.market_data.ingestion.parser import ParsedRow


class RejectionReason(StrEnum):
    MISSING_STRIKE = "MISSING_STRIKE"
    NON_POSITIVE_STRIKE = "NON_POSITIVE_STRIKE"
    MISSING_EXPIRY = "MISSING_EXPIRY"
    MISSING_OPTION_TYPE = "MISSING_OPTION_TYPE"
    NO_PRICE_FIELDS = "NO_PRICE_FIELDS"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    UNPARSEABLE_ROW = "UNPARSEABLE_ROW"
    INSTRUMENT_UNRESOLVED = "INSTRUMENT_UNRESOLVED"
    INSTRUMENT_AMBIGUOUS = "INSTRUMENT_AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class RejectedRow:
    row_number: int
    reason: RejectionReason
    message: str
    raw: dict

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "reason": str(self.reason),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ValidatedOptionRow:
    row_number: int
    strike: Decimal
    expiry: date
    option_type: OptionType
    bid_price: Decimal | None
    ask_price: Decimal | None
    last_price: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    underlying_price: Decimal | None
    exchange_timestamp: object | None
    sequence_number: int | None
    symbol: str | None
    raw: dict


PRICE_FIELDS = ("bid_price", "ask_price", "last_price")


class OptionChainRowValidator:
    def __init__(self, expected_symbol: str | None = None) -> None:
        self._expected_symbol = expected_symbol.upper() if expected_symbol else None

    def validate(self, row: ParsedRow) -> ValidatedOptionRow | RejectedRow:
        values = row.values

        strike = values.get("strike")
        if strike is None:
            return self._reject(row, RejectionReason.MISSING_STRIKE, "Strike is empty.")
        if strike <= 0:
            return self._reject(
                row,
                RejectionReason.NON_POSITIVE_STRIKE,
                f"Strike must be strictly positive, got {strike}.",
            )

        expiry = values.get("expiry")
        if expiry is None:
            return self._reject(row, RejectionReason.MISSING_EXPIRY, "Expiry is empty.")

        option_type = values.get("option_type")
        if option_type is None:
            return self._reject(row, RejectionReason.MISSING_OPTION_TYPE, "Option type is empty.")

        symbol = values.get("symbol")
        if (
            self._expected_symbol is not None
            and symbol is not None
            and symbol.strip().upper() != self._expected_symbol
        ):
            return self._reject(
                row,
                RejectionReason.SYMBOL_MISMATCH,
                f"Row symbol {symbol!r} does not match the requested underlying "
                f"{self._expected_symbol!r}.",
            )

        if all(values.get(name) is None for name in PRICE_FIELDS):
            return self._reject(
                row,
                RejectionReason.NO_PRICE_FIELDS,
                "Row has no bid, ask or last price, so no observation can be formed.",
            )

        return ValidatedOptionRow(
            row_number=row.row_number,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            bid_price=values.get("bid_price"),
            ask_price=values.get("ask_price"),
            last_price=values.get("last_price"),
            bid_size=values.get("bid_size"),
            ask_size=values.get("ask_size"),
            volume=values.get("volume"),
            open_interest=values.get("open_interest"),
            underlying_price=values.get("underlying_price"),
            exchange_timestamp=values.get("exchange_timestamp"),
            sequence_number=values.get("sequence_number"),
            symbol=symbol,
            raw=row.raw,
        )

    @staticmethod
    def _reject(row: ParsedRow, reason: RejectionReason, message: str) -> RejectedRow:
        return RejectedRow(row_number=row.row_number, reason=reason, message=message, raw=row.raw)
