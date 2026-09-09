"""The canonical live event.

Every message that enters the platform from a live feed becomes one of these
before anything else looks at it. Two fields are the reason the type exists:

``source_timestamp`` and ``ingest_timestamp`` are both kept. The gap between
them is feed latency, and a platform that overwrites the first with the second
can no longer tell a stale feed from a slow one — which is the difference
between "the market is quiet" and "we have stopped receiving".

``sequence_number`` is nullable and never invented. A feed that does not
sequence its messages cannot support gap detection, and a counter we made up
would assert that it does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MarketEventType(StrEnum):
    QUOTE = "QUOTE"
    TRADE = "TRADE"
    ORDER_BOOK = "ORDER_BOOK"
    BAR = "BAR"
    OPTION_CHAIN = "OPTION_CHAIN"
    OI_UPDATE = "OI_UPDATE"
    MARKET_STATUS = "MARKET_STATUS"


class FeedStatus(StrEnum):
    """What the connection is doing, as the API reports it."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    #: Connected, but nothing has arrived within the staleness window. Kept
    #: distinct from DISCONNECTED because a live socket delivering nothing is a
    #: different problem from a socket that dropped, and looks identical to a
    #: quiet market unless it is named.
    STALE = "STALE"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One normalised message from a live feed."""

    event_type: MarketEventType
    instrument_id: uuid.UUID
    source: str
    #: When the venue says it happened.
    source_timestamp: datetime
    #: When we received it. Never written into ``source_timestamp``.
    ingest_timestamp: datetime
    payload: dict = field(default_factory=dict)
    sequence_number: int | None = None
    #: The quality engine's overall score for the observation this event
    #: carries, when the event carries a scoreable observation. ``None`` means
    #: not scored, never "we assume it is fine".
    quality_score: float | None = None
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        for name in ("source_timestamp", "ingest_timestamp"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    @property
    def latency_seconds(self) -> float:
        """How long the message took to reach us. Negative means clock skew."""
        return (self.ingest_timestamp - self.source_timestamp).total_seconds()

    def to_dict(self) -> dict:
        return {
            "event_id": str(self.event_id),
            "event_type": str(self.event_type),
            "instrument_id": str(self.instrument_id),
            "source": self.source,
            "source_timestamp": self.source_timestamp.isoformat(),
            "ingest_timestamp": self.ingest_timestamp.isoformat(),
            "latency_seconds": self.latency_seconds,
            "sequence_number": self.sequence_number,
            "quality_score": self.quality_score,
            "payload": self.payload,
        }
