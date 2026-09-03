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
    #: Event-level add/cancel/modify/execute messages with sequencing. Distinct
    #: from ORDER_BOOK because a feed that publishes periodic depth snapshots
    #: supports book analytics and cannot support a queue or intensity model,
    #: and the difference has to be declarable rather than discovered.
    BOOK_EVENTS = "BOOK_EVENTS"
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


class BookEventType(StrEnum):
    """What an order-book event did.

    ``MODIFY`` is kept separate from an ADD/CANCEL pair because a venue that
    publishes it usually preserves queue priority for a size *reduction* and
    loses it for an increase, and collapsing the two would silently assert one
    of those. Nothing here infers a type: a feed that does not label its events
    is reported as unlabelled rather than classified by a heuristic.
    """

    ADD = "ADD"
    CANCEL = "CANCEL"
    MODIFY = "MODIFY"
    TRADE = "TRADE"

    @classmethod
    def parse(cls, value: str) -> BookEventType:
        token = str(value).strip().upper().replace(" ", "_").replace("-", "_")
        aliases = {
            "ADD": cls.ADD,
            "A": cls.ADD,
            "NEW": cls.ADD,
            "INSERT": cls.ADD,
            "SUBMIT": cls.ADD,
            "PLACE": cls.ADD,
            "CANCEL": cls.CANCEL,
            "C": cls.CANCEL,
            "DELETE": cls.CANCEL,
            "D": cls.CANCEL,
            "REMOVE": cls.CANCEL,
            "MODIFY": cls.MODIFY,
            "M": cls.MODIFY,
            "REPLACE": cls.MODIFY,
            "AMEND": cls.MODIFY,
            "UPDATE": cls.MODIFY,
            "TRADE": cls.TRADE,
            "T": cls.TRADE,
            "EXECUTE": cls.TRADE,
            "EXECUTION": cls.TRADE,
            "FILL": cls.TRADE,
            "E": cls.TRADE,
        }
        if token not in aliases:
            raise ValueError(f"unrecognised order-book event type: {value!r}")
        return aliases[token]


class BookSide(StrEnum):
    """Which side of the book an event touched."""

    BID = "BID"
    ASK = "ASK"

    @classmethod
    def parse(cls, value: str) -> BookSide:
        token = str(value).strip().upper()
        if token in {"BID", "B", "BUY", "BUYSIDE", "BIDS", "1"}:
            return cls.BID
        if token in {"ASK", "A", "SELL", "S", "OFFER", "SELLSIDE", "ASKS", "2"}:
            return cls.ASK
        raise ValueError(f"unrecognised book side: {value!r}")


class UploadKind(StrEnum):
    OPTION_CHAIN = "OPTION_CHAIN"
    POSITIONS = "POSITIONS"
    TRADES = "TRADES"
    QUOTES = "QUOTES"
    #: Periodic depth snapshots: one row per instant, levels across the row.
    BOOK_SNAPSHOTS = "BOOK_SNAPSHOTS"
    #: Event-level add/cancel/modify/execute messages: one row per message.
    BOOK_EVENTS = "BOOK_EVENTS"


class UploadStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    INGESTED = "INGESTED"
    REJECTED = "REJECTED"
