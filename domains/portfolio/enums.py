from __future__ import annotations

from enum import StrEnum


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @classmethod
    def for_quantity(cls, quantity) -> PositionSide:
        return cls.LONG if quantity >= 0 else cls.SHORT

    @classmethod
    def parse(cls, value: str) -> PositionSide:
        """Accept the spellings real broker exports use."""
        token = value.strip().upper()
        if token in {"LONG", "L", "BUY", "B", "+"}:
            return cls.LONG
        if token in {"SHORT", "S", "SELL", "-"}:
            return cls.SHORT
        raise ValueError(f"unrecognised side: {value!r}")


class PositionSource(StrEnum):
    MANUAL = "MANUAL"
    CSV_IMPORT = "CSV_IMPORT"
    BROKER_API = "BROKER_API"


class ValuationMethod(StrEnum):
    """How a position's price was obtained. Always recorded.

    The user must be able to see which positions were marked to market and
    which were marked to model, without inferring it from anything.
    """

    #: Mid of a valid two-sided market. The default and the only unqualified one.
    MARKET_MID = "MARKET_MID"
    #: Last trade, because there was no two-sided market. An explicit fallback:
    #: a print is not a market, and the substitution is recorded rather than
    #: silently made.
    MARKET_LAST = "MARKET_LAST"
    #: A market price was used but the quote was stale at the valuation time.
    STALE_MARKET = "STALE_MARKET"
    #: No usable market price; valued from the fitted surface. A model output.
    MODEL_REFERENCE = "MODEL_REFERENCE"
    #: Neither a market price nor a model price could be produced.
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_market(self) -> bool:
        return self in {
            ValuationMethod.MARKET_MID,
            ValuationMethod.MARKET_LAST,
            ValuationMethod.STALE_MARKET,
        }


class GreekSource(StrEnum):
    """Which volatility the Greeks were computed at."""

    #: The volatility implied by this contract's own observed price.
    MARKET_IV = "MARKET_IV"
    #: The fitted surface's reference volatility, used when the contract has no
    #: invertible market price of its own.
    REFERENCE_IV = "REFERENCE_IV"
    #: Linear instruments have no volatility input.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNAVAILABLE = "UNAVAILABLE"


class AggregationDimension(StrEnum):
    UNDERLYING = "UNDERLYING"
    EXPIRY = "EXPIRY"
    ASSET_CLASS = "ASSET_CLASS"
    STRATEGY_TAG = "STRATEGY_TAG"
    CURRENCY = "CURRENCY"
