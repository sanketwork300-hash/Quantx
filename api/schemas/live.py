from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from api.schemas.common import APIModel
from api.schemas.market import QualityOut
from domains.market_data.streaming.events import FeedStatus


class LiveQuoteOut(APIModel):
    """A live observation, with everything needed to judge whether to trust it.

    ``age_seconds`` is carried explicitly rather than left to the caller to
    compute from the timestamp: a price with no visible age is a price that gets
    treated as current whatever its actual age, which is the whole failure mode
    a live feed has.
    """

    instrument_id: uuid.UUID
    symbol: str
    exchange: str
    asset_class: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    age_seconds: float
    source: str
    feed: str
    bid_price: Decimal | None
    bid_size: Decimal | None
    ask_price: Decimal | None
    ask_size: Decimal | None
    last_price: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    #: Mid of a genuine two-sided market, or null. Never a trade print standing
    #: in for a mid.
    mid_price: Decimal | None
    quality: QualityOut | None


class LiveQuotesOut(APIModel):
    items: list[LiveQuoteOut]
    #: Instruments that were asked about and for which no live price is held.
    #: Returned rather than omitted, so a short answer is never mistaken for a
    #: complete one.
    unavailable: list[uuid.UUID] = Field(default_factory=list)
    as_of: datetime


class FeedHealthOut(APIModel):
    feed: str
    status: FeedStatus
    updated_at: datetime
    connected_since: datetime | None
    last_event_at: datetime | None
    events_received: int
    reconnects: int
    subscribed_instruments: int
    last_error: str | None


class LiveStatusOut(APIModel):
    """What the platform is configured to do, and what it is actually doing."""

    provider: str
    transport: str
    #: False when the transport samples rather than delivering every change.
    #: Stated because a queue or intensity model must not be built on samples.
    delivers_every_update: bool
    poll_interval_seconds: float | None
    health: FeedHealthOut | None
    #: Present when the feed is not usable, naming what is missing.
    unavailable_reason: str | None = None


class SubscriptionRequest(APIModel):
    instrument_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class SubscriptionOut(APIModel):
    """Everything currently subscribed, after the change was applied."""

    feed: str
    instrument_ids: list[uuid.UUID]
    ttl_seconds: int


class LiveStateOut(APIModel):
    """A snapshot assembled from live quotes.

    Content-addressed like every other market state, so two calculations that
    report the same ``state_id`` provably saw the same prices.
    """

    state_id: str
    as_of_timestamp: datetime
    quote_count: int
    sources: list[str]
    #: Instruments requested that are not in the snapshot, and why they are not.
    unavailable: list[uuid.UUID] = Field(default_factory=list)
    quotes: dict[str, dict] = Field(default_factory=dict)


class InstrumentMasterOut(APIModel):
    """The result of loading a provider's instrument file.

    Reports the conservation sum rather than just a success count: rows in must
    equal instruments made plus rows rejected plus rows the segment filter
    excluded, and a load where that does not hold has lost something.
    """

    input_rows: int
    accepted: int
    rejected: int
    filtered_out: int
    conserved: bool
    rejection_reasons: dict[str, int]
    fields_missing: list[str]
    fields_unmapped: list[str]
    spec: str
