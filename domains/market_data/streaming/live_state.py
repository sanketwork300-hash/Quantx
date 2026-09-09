"""The live market state: what is true right now, and how long ago it became true.

Redis holds this rather than Postgres, for the reason the architecture gives:
tick-rate writes do not belong in a transactional database. But the store is
deliberately *not* the historical record — it is a cache with a TTL, and the
TTL is the point. An entry that stops being refreshed disappears, so a stale
price cannot be served indefinitely as though the feed were still running.

Every stored quote keeps its own exchange timestamp, so age is measured against
the observation rather than against when we happened to write it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from domains.market_data.models import Quote
from domains.market_data.quality.flags import MarketDataQuality, QualityFlag
from domains.market_data.streaming.events import FeedStatus
from infrastructure.cache.client import Cache

#: Key layout. Namespaced so a shared Redis is legible, and versioned so a
#: change to the stored shape cannot be read by the previous code as though it
#: were the old shape.
KEY_PREFIX = "qip:v1"


def quote_key(instrument_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}:quote:{instrument_id}"


def book_key(instrument_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}:orderbook:{instrument_id}"


def status_key(feed: str) -> str:
    return f"{KEY_PREFIX}:feed_status:{feed}"


def subscription_key(feed: str) -> str:
    return f"{KEY_PREFIX}:subscriptions:{feed}"


def _decimal_out(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _decimal_in(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class LiveQuote:
    """A stored live quote and the quality it was scored with.

    The quality travels with the quote rather than being recomputed on read,
    because it was measured against the state of the world when the quote
    arrived — recomputing it later would score it against a different moment and
    quietly disagree with what was published at the time.
    """

    quote: Quote
    quality: MarketDataQuality | None = None
    feed: str = ""

    def age_seconds(self, as_of: datetime | None = None) -> float:
        return self.quote.age_seconds(as_of or datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class FeedHealth:
    """What the connection is doing, as last written by the stream manager."""

    feed: str
    status: FeedStatus
    updated_at: datetime
    connected_since: datetime | None = None
    last_event_at: datetime | None = None
    events_received: int = 0
    reconnects: int = 0
    subscribed_instruments: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "feed": self.feed,
            "status": str(self.status),
            "updated_at": self.updated_at.isoformat(),
            "connected_since": (self.connected_since.isoformat() if self.connected_since else None),
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "events_received": self.events_received,
            "reconnects": self.reconnects,
            "subscribed_instruments": self.subscribed_instruments,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> FeedHealth:
        def when(key: str) -> datetime | None:
            raw = payload.get(key)
            return datetime.fromisoformat(raw) if raw else None

        return cls(
            feed=payload["feed"],
            status=FeedStatus(payload["status"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            connected_since=when("connected_since"),
            last_event_at=when("last_event_at"),
            events_received=int(payload.get("events_received", 0)),
            reconnects=int(payload.get("reconnects", 0)),
            subscribed_instruments=int(payload.get("subscribed_instruments", 0)),
            last_error=payload.get("last_error"),
        )


class LiveMarketStore:
    """Reads and writes the live state.

    Takes a :class:`Cache` rather than a Redis client, so the in-memory cache
    used in tests and single-process development exercises exactly this code.
    """

    def __init__(self, cache: Cache, ttl_seconds: int = 300) -> None:
        self._cache = cache
        self._ttl = ttl_seconds

    async def put_quote(
        self,
        quote: Quote,
        quality: MarketDataQuality | None = None,
        feed: str = "",
    ) -> None:
        payload = {
            "instrument_id": str(quote.instrument_id),
            "exchange_timestamp": quote.exchange_timestamp.isoformat(),
            "receive_timestamp": quote.receive_timestamp.isoformat(),
            "source": quote.source,
            "bid_price": _decimal_out(quote.bid_price),
            "bid_size": _decimal_out(quote.bid_size),
            "ask_price": _decimal_out(quote.ask_price),
            "ask_size": _decimal_out(quote.ask_size),
            "last_price": _decimal_out(quote.last_price),
            "last_size": _decimal_out(quote.last_size),
            "volume": _decimal_out(quote.volume),
            "open_interest": _decimal_out(quote.open_interest),
            "sequence_number": quote.sequence_number,
            "metadata": quote.metadata,
            "quality": quality.to_dict() if quality is not None else None,
            "feed": feed,
        }
        await self._cache.set(quote_key(quote.instrument_id), payload, self._ttl)

    async def get_quote(self, instrument_id: uuid.UUID) -> LiveQuote | None:
        payload = await self._cache.get(quote_key(instrument_id))
        if not payload:
            return None
        return _live_quote_from(payload)

    async def get_quotes(self, instrument_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, LiveQuote]:
        found: dict[uuid.UUID, LiveQuote] = {}
        for instrument_id in instrument_ids:
            live = await self.get_quote(instrument_id)
            if live is not None:
                found[instrument_id] = live
        return found

    async def put_book(self, instrument_id: uuid.UUID, payload: dict) -> None:
        await self._cache.set(book_key(instrument_id), payload, self._ttl)

    async def get_book(self, instrument_id: uuid.UUID) -> dict | None:
        return await self._cache.get(book_key(instrument_id))

    # -------------------------------------------------------- subscriptions
    #
    # Interest is registered by the API process and read by the feed worker, so
    # it has to live somewhere both can see. Each instrument carries its own
    # expiry rather than the set carrying one: interest from a browser tab that
    # was closed should decay without taking the rest of the set with it.
    async def register_interest(
        self,
        feed: str,
        instrument_ids: Iterable[uuid.UUID],
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> set[uuid.UUID]:
        moment = now or datetime.now(UTC)
        current = await self._cache.get(subscription_key(feed)) or {}
        expires_at = (moment + timedelta(seconds=ttl_seconds)).isoformat()
        for instrument_id in instrument_ids:
            current[str(instrument_id)] = expires_at
        live = _prune(current, moment)
        await self._cache.set(subscription_key(feed), live, ttl_seconds * 2)
        return {uuid.UUID(key) for key in live}

    async def drop_interest(
        self, feed: str, instrument_ids: Iterable[uuid.UUID], ttl_seconds: int
    ) -> set[uuid.UUID]:
        current = await self._cache.get(subscription_key(feed)) or {}
        for instrument_id in instrument_ids:
            current.pop(str(instrument_id), None)
        live = _prune(current, datetime.now(UTC))
        await self._cache.set(subscription_key(feed), live, ttl_seconds * 2)
        return {uuid.UUID(key) for key in live}

    async def interest(self, feed: str, now: datetime | None = None) -> set[uuid.UUID]:
        current = await self._cache.get(subscription_key(feed)) or {}
        return {uuid.UUID(key) for key in _prune(current, now or datetime.now(UTC))}

    async def put_health(self, health: FeedHealth) -> None:
        # Health outlives quotes: a feed that has been down for an hour should
        # still be able to say so, where a price from an hour ago should not.
        await self._cache.set(status_key(health.feed), health.to_dict(), self._ttl * 4)

    async def get_health(self, feed: str) -> FeedHealth | None:
        payload = await self._cache.get(status_key(feed))
        return FeedHealth.from_dict(payload) if payload else None


def _live_quote_from(payload: dict) -> LiveQuote:
    quality_payload = payload.get("quality")
    quality = None
    if quality_payload:
        quality = MarketDataQuality(
            stale_score=quality_payload["stale_score"],
            spread_score=quality_payload["spread_score"],
            liquidity_score=quality_payload["liquidity_score"],
            consistency_score=quality_payload["consistency_score"],
            completeness_score=quality_payload["completeness_score"],
            overall_score=quality_payload["overall_score"],
            flags=tuple(QualityFlag.from_dict(flag) for flag in quality_payload.get("flags") or ()),
        )

    quote = Quote(
        instrument_id=uuid.UUID(payload["instrument_id"]),
        exchange_timestamp=datetime.fromisoformat(payload["exchange_timestamp"]),
        receive_timestamp=datetime.fromisoformat(payload["receive_timestamp"]),
        source=payload["source"],
        bid_price=_decimal_in(payload.get("bid_price")),
        bid_size=_decimal_in(payload.get("bid_size")),
        ask_price=_decimal_in(payload.get("ask_price")),
        ask_size=_decimal_in(payload.get("ask_size")),
        last_price=_decimal_in(payload.get("last_price")),
        last_size=_decimal_in(payload.get("last_size")),
        volume=_decimal_in(payload.get("volume")),
        open_interest=_decimal_in(payload.get("open_interest")),
        sequence_number=payload.get("sequence_number"),
        metadata=dict(payload.get("metadata") or {}),
    )
    return LiveQuote(quote=quote, quality=quality, feed=payload.get("feed", ""))


def sorted_by_age(quotes: Sequence[LiveQuote], as_of: datetime) -> tuple[LiveQuote, ...]:
    return tuple(sorted(quotes, key=lambda live: live.age_seconds(as_of)))


def _prune(entries: dict, now: datetime) -> dict:
    """Drop interest whose renewal window has passed."""
    live = {}
    for key, expires_at in entries.items():
        try:
            if datetime.fromisoformat(expires_at) > now:
                live[key] = expires_at
        except (TypeError, ValueError):
            continue
    return live
