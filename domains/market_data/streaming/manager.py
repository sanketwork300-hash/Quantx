"""The live feed, from frame to market state.

Everything that is the same for every feed lives here: reconnection,
subscription replay, duplicate suppression, out-of-order rejection, staleness
detection and health reporting. What differs per provider — how a frame arrives
and what its fields are called — is the transport and the normalisation spec,
both injected.

Four behaviours are worth reading carefully, because each of them is a way live
market data goes quietly wrong:

**An older observation never overwrites a newer one.** Feeds reorder, and a
reconnection can replay. Writing a stale price over a fresh one produces a
market that appears to move backwards, and no error anywhere.

**A repeat is recognised as a repeat.** The same exchange timestamp and the same
prices is not a new observation; it is the same one again. It is passed to the
quality engine as a duplicate rather than counted as market activity.

**A connection that delivers nothing is not a quiet market.** After
``stale_after_seconds`` with no event the feed reports ``STALE``. A live socket
delivering silence looks exactly like a calm session unless something says so.

**A frame we cannot read is counted, not swallowed and not fatal.** One bad
frame must not drop a working connection, and a rising count is the only signal
that a decoder has gone wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from domains.instruments.models import Instrument
from domains.market_data.models import Quote
from domains.market_data.quality.engine import MarketDataQualityEngine, QuoteContext
from domains.market_data.streaming.bus import MarketEventBus
from domains.market_data.streaming.events import (
    FeedStatus,
    MarketEvent,
    MarketEventType,
)
from domains.market_data.streaming.feed import FeedEntry, FeedTransport, FeedTransportError
from domains.market_data.streaming.live_state import FeedHealth, LiveMarketStore
from domains.market_data.streaming.reconnect import BackoffPolicy, ConnectionAttempts
from domains.market_data.streaming.subscriptions import SubscriptionRegistry


@dataclass
class StreamCounters:
    """What the connection has actually done. Reported, never inferred."""

    events_received: int = 0
    quotes_stored: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    unreadable: int = 0
    unknown_instrument: int = 0
    reconnects: int = 0
    #: Connections that opened and ended without delivering anything. A rising
    #: count with a healthy-looking socket is the signature of a subscription
    #: the provider is not honouring.
    empty_connections: int = 0

    def to_dict(self) -> dict:
        return {
            "events_received": self.events_received,
            "quotes_stored": self.quotes_stored,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "unreadable": self.unreadable,
            "unknown_instrument": self.unknown_instrument,
            "reconnects": self.reconnects,
            "empty_connections": self.empty_connections,
        }


@dataclass(frozen=True, slots=True)
class StreamOptions:
    feed_name: str = "upstox"
    #: No event for this long while connected means the feed is reported STALE.
    stale_after_seconds: float = 30.0
    #: How often health is written to the live store.
    health_interval_seconds: float = 5.0
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)


class MarketStreamManager:
    """Owns one feed connection and everything that happens to its messages."""

    def __init__(
        self,
        transport: FeedTransport,
        store: LiveMarketStore,
        read_quote: Callable[[uuid.UUID, Mapping], Quote],
        instrument_for_key: Callable[[str], Instrument | None],
        options: StreamOptions | None = None,
        bus: MarketEventBus | None = None,
        quality: MarketDataQualityEngine | None = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._read_quote = read_quote
        self._instrument_for_key = instrument_for_key
        self._options = options or StreamOptions()
        self._bus = bus or MarketEventBus()
        self._quality = quality or MarketDataQualityEngine()

        self.registry = SubscriptionRegistry()
        self.counters = StreamCounters()
        self.status = FeedStatus.DISCONNECTED
        self.connected_since: datetime | None = None
        self.last_event_at: datetime | None = None
        self.last_error: str | None = None

        self._attempts = ConnectionAttempts(policy=self._options.backoff)
        self._stop = asyncio.Event()
        #: instrument id -> provider key, and the reverse, for the ids this
        #: manager has been asked about. Populated on subscribe so the hot path
        #: never has to reach a database.
        self._keys: dict[uuid.UUID, str] = {}
        self._instruments: dict[uuid.UUID, Instrument] = {}
        #: The last observation per instrument, for duplicate and ordering checks.
        self._last: dict[uuid.UUID, tuple[datetime, Decimal | None]] = {}

    # ------------------------------------------------------------- lifecycle
    @property
    def bus(self) -> MarketEventBus:
        return self._bus

    def subscribe(self, instruments: Sequence[tuple[Instrument, str]]) -> set[uuid.UUID]:
        """Register interest in instruments, given their provider keys."""
        ids = set()
        for instrument, provider_key in instruments:
            self._keys[instrument.id] = provider_key
            self._instruments[instrument.id] = instrument
            ids.add(instrument.id)
        return self.registry.add(ids)

    def unsubscribe(self, instrument_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        return self.registry.remove(instrument_ids)

    def stop(self) -> None:
        self._stop.set()
        stopper = getattr(self._transport, "stop", None)
        if callable(stopper):
            stopper()

    def desired_keys(self) -> set[str]:
        return {
            self._keys[instrument_id]
            for instrument_id in self.registry.wanted
            if instrument_id in self._keys
        }

    async def run(self) -> None:
        """Connect, and keep reconnecting until stopped.

        The delay is taken *before* each retry rather than after a failure, so a
        provider that refuses instantly is not hammered at full speed.
        """
        health_task = asyncio.create_task(self._publish_health_periodically())
        try:
            while not self._stop.is_set():
                self.status = FeedStatus.CONNECTING
                await self._write_health()
                delivered_before = self.counters.events_received
                try:
                    await self._transport.run(
                        desired_keys=self.desired_keys,
                        emit=self._ingest,
                        on_connected=self._on_connected,
                    )
                except (FeedTransportError, OSError) as exc:
                    self.last_error = str(exc)[:200]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a transport bug must not kill the loop
                    self.last_error = f"{type(exc).__name__}: {exc}"[:200]

                if self.counters.events_received == delivered_before:
                    # Opening is not evidence that a connection works. Counting
                    # this separately is what keeps the backoff from resetting
                    # on a provider that accepts and immediately drops.
                    self._attempts.opened_but_delivered_nothing()
                    self.counters.empty_connections = self._attempts.empty_connections

                if self._stop.is_set():
                    break

                self.registry.connection_lost()
                self.counters.reconnects += 1
                self.status = FeedStatus.RECONNECTING
                await self._write_health()

                delay = self._attempts.next_delay()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
        finally:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_task
            self.status = FeedStatus.STOPPED
            self.connected_since = None
            await self._write_health()

    async def _on_connected(self) -> None:
        self.status = FeedStatus.CONNECTED
        self.connected_since = datetime.now(UTC)
        self.last_error = None
        await self._write_health()

    # --------------------------------------------------------------- ingest
    async def _ingest(self, entries: Sequence[FeedEntry]) -> None:
        for entry in entries:
            await self._ingest_entry(entry)

    async def _ingest_entry(self, entry: FeedEntry) -> None:
        self.counters.events_received += 1
        self.last_event_at = datetime.now(UTC)
        # A connection that has delivered a message has demonstrably worked, so
        # the backoff may reset. Opening alone is not evidence of that.
        self._attempts.succeeded()

        instrument = self._instrument_for_key(entry.provider_key)
        if instrument is None:
            # An instrument we did not ask about, or one the master does not
            # know. Counted rather than dropped silently: it means the
            # subscription and the instrument directory disagree.
            self.counters.unknown_instrument += 1
            return

        try:
            quote = self._read_quote(instrument.id, entry.fields)
        except Exception as exc:
            self.counters.unreadable += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return

        previous = self._last.get(instrument.id)
        is_duplicate = False
        if previous is not None:
            previous_time, previous_price = previous
            if quote.exchange_timestamp < previous_time:
                self.counters.out_of_order += 1
                return
            if quote.exchange_timestamp == previous_time and quote.last_price == previous_price:
                is_duplicate = True
                self.counters.duplicates += 1

        quality = self._quality.score_quote(
            quote,
            QuoteContext(
                asset_class=instrument.asset_class,
                as_of=quote.receive_timestamp,
                tick_size=instrument.tick_size,
                previous_price=previous[1] if previous is not None else None,
                is_duplicate=is_duplicate,
                multiplier_assumed=instrument.multiplier_is_assumed,
            ),
        )

        self._last[instrument.id] = (quote.exchange_timestamp, quote.last_price)
        await self._store.put_quote(quote, quality, feed=self._options.feed_name)
        self.counters.quotes_stored += 1

        await self._bus.publish(
            MarketEvent(
                event_type=MarketEventType.QUOTE,
                instrument_id=instrument.id,
                source=quote.source,
                source_timestamp=quote.exchange_timestamp,
                ingest_timestamp=quote.receive_timestamp,
                sequence_number=quote.sequence_number,
                quality_score=quality.overall_score,
                payload={
                    "bid_price": _out(quote.bid_price),
                    "ask_price": _out(quote.ask_price),
                    "last_price": _out(quote.last_price),
                    "volume": _out(quote.volume),
                    "open_interest": _out(quote.open_interest),
                    "duplicate": is_duplicate,
                },
            )
        )

    # --------------------------------------------------------------- health
    def health(self) -> FeedHealth:
        status = self.status
        if status is FeedStatus.CONNECTED and self._is_stale():
            status = FeedStatus.STALE
        return FeedHealth(
            feed=self._options.feed_name,
            status=status,
            updated_at=datetime.now(UTC),
            connected_since=self.connected_since,
            last_event_at=self.last_event_at,
            events_received=self.counters.events_received,
            reconnects=self.counters.reconnects,
            subscribed_instruments=len(self.registry.wanted),
            last_error=self.last_error,
        )

    def _is_stale(self) -> bool:
        reference = self.last_event_at or self.connected_since
        if reference is None:
            return False
        idle = (datetime.now(UTC) - reference).total_seconds()
        return idle > self._options.stale_after_seconds

    async def _write_health(self) -> None:
        with contextlib.suppress(Exception):
            await self._store.put_health(self.health())

    async def _publish_health_periodically(self) -> None:
        while not self._stop.is_set():
            await self._write_health()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._options.health_interval_seconds
                )


def _out(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
