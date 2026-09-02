from __future__ import annotations

from enum import StrEnum


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    FX = "FX"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    CRYPTO_PERPETUAL = "CRYPTO_PERPETUAL"


class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"

    @property
    def code(self) -> str:
        """Single-character form used in canonical keys."""
        return "C" if self is OptionType.CALL else "P"

    @classmethod
    def parse(cls, value: str) -> OptionType:
        """Accept the many spellings real market data files use.

        Chain files from different sources say CE/PE, C/P, CALL/PUT, or c/p.
        Normalising here means nothing downstream has to guess.
        """
        token = value.strip().upper()
        if token in {"C", "CE", "CALL", "CALLS"}:
            return cls.CALL
        if token in {"P", "PE", "PUT", "PUTS"}:
            return cls.PUT
        raise ValueError(f"unrecognised option type: {value!r}")


class ExerciseStyle(StrEnum):
    EUROPEAN = "EUROPEAN"
    AMERICAN = "AMERICAN"
    BERMUDAN = "BERMUDAN"


class SettlementType(StrEnum):
    CASH = "CASH"
    PHYSICAL = "PHYSICAL"


class InstrumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


#: Asset classes whose contracts are dated.
DATED_CLASSES = frozenset({AssetClass.FUTURE, AssetClass.OPTION})

#: Asset classes that must never carry expiry, strike or option type.
UNDATED_CLASSES = frozenset(
    {
        AssetClass.EQUITY,
        AssetClass.INDEX,
        AssetClass.FX,
        AssetClass.CRYPTO_SPOT,
        AssetClass.CRYPTO_PERPETUAL,
    }
)
