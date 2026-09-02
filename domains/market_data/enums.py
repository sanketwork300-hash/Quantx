from __future__ import annotations

from enum import StrEnum


class ProviderCapability(StrEnum):
    """What a provider can actually supply.

    Declared up front rather than discovered by catching ``NotImplementedError``
    mid-pipeline, so a caller can plan around a missing capability (skip
    microstructure analytics for a snapshot-only feed) instead of failing
    halfway through a calculation.
    """

    INSTRUMENTS = "INSTRUMENTS"
    QUOTES = "QUOTES"
    OPTION_CHAINS = "OPTION_CHAINS"
    ORDER_BOOK = "ORDER_BOOK"
    TRADES = "TRADES"
    BARS = "BARS"


class BarInterval(StrEnum):
    S1 = "1s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return {
            BarInterval.S1: 1,
            BarInterval.M1: 60,
            BarInterval.M5: 300,
            BarInterval.M15: 900,
            BarInterval.H1: 3600,
            BarInterval.D1: 86_400,
        }[self]


class AggressorSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class UploadKind(StrEnum):
    OPTION_CHAIN = "OPTION_CHAIN"
    POSITIONS = "POSITIONS"
    TRADES = "TRADES"
    QUOTES = "QUOTES"


class UploadStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    INGESTED = "INGESTED"
    REJECTED = "REJECTED"
