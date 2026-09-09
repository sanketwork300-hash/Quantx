"""The parts of a live feed that are the same for every provider.

Reconnection, subscription replay, duplicate suppression, ordering and staleness
are dull, they are identical whichever venue is on the other end, and every one
of them fails silently when it is wrong. So they are tested here without a
provider, a socket or a database anywhere near them.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.instruments.enums import AssetClass
from domains.instruments.models import make_instrument
from domains.market_data.models import Quote
from domains.market_data.streaming.bus import MarketEventBus
from domains.market_data.streaming.decoders import (
    DecoderUnavailable,
    FeedDecodeError,
    JsonFeedDecoder,
    ProtobufFeedDecoder,
)
from domains.market_data.streaming.events import (
    FeedStatus,
    MarketEvent,
    MarketEventType,
)
from domains.market_data.streaming.feed import FeedEntry, FeedTransport
from domains.market_data.streaming.live_state import LiveMarketStore
from domains.market_data.streaming.manager import MarketStreamManager, StreamOptions
from domains.market_data.streaming.reconnect import BackoffPolicy, ConnectionAttempts
from domains.market_data.streaming.subscriptions import SubscriptionRegistry
from infrastructure.cache.client import InMemoryCache

NIFTY = make_instrument(
    asset_class=AssetClass.INDEX, exchange="NSE", symbol="NIFTY", currency="INR"
)
KEY = "NSE_INDEX|Nifty 50"
BASE = datetime(2026, 9, 9, 9, 20, tzinfo=UTC)


class ScriptedTransport(FeedTransport):
    """Emits a fixed script of entries, then ends the connection."""

    name = "scripted"

    def __init__(self, *batches: list[FeedEntry], fail_with: Exception | None = None) -> None:
        self._batches = list(batches)
        self._fail_with = fail_with
        self.runs = 0
        self.keys_seen: list[set[str]] = []

    async def run(self, desired_keys, emit, on_connected=None):
        self.runs += 1
        if on_connected is not None:
            await on_connected()
        self.keys_seen.append(set(desired_keys()))
        if self._fail_with is not None:
            raise self._fail_with
        for batch in self._batches:
            await emit(batch)


def entry(timestamp: datetime, last: float, bid: float = 100.0, ask: float = 101.0):
    return FeedEntry(
        provider_key=KEY,
        fields={
            "timestamp": timestamp.isoformat(),
            "last_price": last,
            "bid_price": bid,
            "ask_price": ask,
        },
    )


def read_quote(instrument_id: uuid.UUID, fields) -> Quote:
    """A minimal normaliser, so these tests exercise the manager and not a spec."""
    return Quote(
        instrument_id=instrument_id,
        exchange_timestamp=datetime.fromisoformat(fields["timestamp"]),
        receive_timestamp=datetime.now(UTC),
        source="test",
        bid_price=Decimal(str(fields["bid_price"])),
        bid_size=Decimal(10),
        ask_price=Decimal(str(fields["ask_price"])),
        ask_size=Decimal(10),
        last_price=Decimal(str(fields["last_price"])),
    )


def build(transport, options: StreamOptions | None = None):
    store = LiveMarketStore(InMemoryCache())
    manager = MarketStreamManager(
        transport=transport,
        store=store,
        read_quote=read_quote,
        instrument_for_key=lambda key: NIFTY if key == KEY else None,
        options=options or StreamOptions(feed_name="test"),
    )
    manager.subscribe([(NIFTY, KEY)])
    return manager, store


class TestBackoff:
    def test_the_delay_grows_and_is_capped(self):
        policy = BackoffPolicy(initial_seconds=1, multiplier=2, maximum_seconds=10, jitter=0)
        assert [policy.delay_for(n) for n in (1, 2, 3, 4, 5, 6)] == [1, 2, 4, 8, 10, 10]

    def test_jitter_only_ever_shortens_the_delay(self):
        """So the cap stays a real cap: jittering upward would let a fleet
        exceed the maximum it was configured with."""
        policy = BackoffPolicy(initial_seconds=8, multiplier=1, maximum_seconds=8, jitter=0.25)
        assert policy.delay_for(1, random_value=1.0) == pytest.approx(6.0)
        assert policy.delay_for(1, random_value=0.0) == pytest.approx(8.0)

    def test_the_counter_resets_only_when_something_arrived(self):
        """A feed that accepts a connection and drops it immediately would
        otherwise be retried at the floor delay forever — a denial-of-service
        attack on the provider, carried out by our own client."""
        attempts = ConnectionAttempts(BackoffPolicy(jitter=0))
        attempts.next_delay()
        attempts.next_delay()
        assert attempts.attempt == 2

        attempts.opened_but_delivered_nothing()
        assert attempts.attempt == 2

        attempts.succeeded()
        assert attempts.attempt == 0


class TestSubscriptions:
    def test_two_subscribers_are_one_subscription(self):
        registry = SubscriptionRegistry()
        first = uuid.uuid4()
        assert registry.add({first}) == {first}
        assert registry.add({first}) == set()
        assert registry.subscriber_count(first) == 2

    def test_one_subscriber_leaving_does_not_stop_the_others_prices(self):
        registry = SubscriptionRegistry()
        first = uuid.uuid4()
        registry.add({first})
        registry.add({first})
        registry.mark_sent({first})
        assert registry.remove({first}) == set()
        assert registry.remove({first}) == {first}

    def test_a_new_connection_knows_nothing_and_everything_is_resent(self):
        """Resending the full set rather than the delta since the drop, because
        the delta since a drop is exactly what was lost."""
        registry = SubscriptionRegistry()
        ids = {uuid.uuid4(), uuid.uuid4()}
        registry.add(ids)
        registry.mark_sent(ids)
        assert registry.pending() == set()

        registry.connection_lost()
        assert registry.pending() == ids


class TestTheEventBus:
    async def test_a_slow_consumer_loses_the_oldest_events_and_is_told(self):
        """Dropping the newest would leave a slow consumer permanently looking
        at an old market; blocking would let one consumer stall the feed."""
        bus = MarketEventBus(queue_size=2)
        subscriber = bus.subscribe("slow")
        for index in range(5):
            await bus.publish(
                MarketEvent(
                    event_type=MarketEventType.QUOTE,
                    instrument_id=NIFTY.id,
                    source="test",
                    source_timestamp=BASE + timedelta(seconds=index),
                    ingest_timestamp=BASE + timedelta(seconds=index),
                )
            )
        assert subscriber.dropped == 3
        assert bus.dropped_events == 3

        newest = await subscriber.queue.get()
        assert newest.source_timestamp == BASE + timedelta(seconds=3)

    async def test_latency_is_the_gap_between_the_two_timestamps(self):
        event = MarketEvent(
            event_type=MarketEventType.QUOTE,
            instrument_id=NIFTY.id,
            source="test",
            source_timestamp=BASE,
            ingest_timestamp=BASE + timedelta(milliseconds=250),
        )
        assert event.latency_seconds == pytest.approx(0.25)


class TestDecoders:
    def test_a_json_frame_yields_its_entries(self):
        decoder = JsonFeedDecoder(entries_path="feeds")
        decoded = decoder.decode('{"feeds": {"NSE_INDEX|Nifty 50": {"ltp": 1.0}}}')
        assert set(decoder.entries(decoded)) == {"NSE_INDEX|Nifty 50"}

    def test_a_frame_that_is_not_json_is_reported_not_swallowed(self):
        with pytest.raises(FeedDecodeError):
            JsonFeedDecoder().decode("not json at all")

    def test_a_binary_decoder_without_its_schema_says_how_to_get_one(self):
        """Rather than shipping a reimplementation of a provider's wire format,
        which would produce plausible numbers from a format nobody checked."""
        decoder = ProtobufFeedDecoder("no.such.module", "FeedResponse")
        with pytest.raises(DecoderUnavailable, match="protoc|protobuf"):
            decoder.decode(b"\x00\x01")


class TestTheManagerDoesNotCorruptTheMarket:
    async def test_a_quote_is_stored_scored_and_published(self):
        manager, store = build(ScriptedTransport([entry(BASE, 100.5)]))
        subscriber = manager.bus.subscribe()

        await manager._ingest([entry(BASE, 100.5)])

        live = await store.get_quote(NIFTY.id)
        assert live.quote.last_price == Decimal("100.5")
        assert live.quality is not None
        assert 0.0 <= live.quality.overall_score <= 1.0

        event = await subscriber.queue.get()
        assert event.event_type is MarketEventType.QUOTE
        assert event.quality_score == live.quality.overall_score

    async def test_an_older_observation_never_overwrites_a_newer_one(self):
        """Feeds reorder and reconnections replay. Writing a stale price over a
        fresh one makes the market appear to move backwards, with no error."""
        manager, store = build(ScriptedTransport())
        await manager._ingest([entry(BASE + timedelta(seconds=5), 101.0)])
        await manager._ingest([entry(BASE, 100.0)])

        live = await store.get_quote(NIFTY.id)
        assert live.quote.last_price == Decimal("101.0")
        assert manager.counters.out_of_order == 1

    async def test_the_same_observation_twice_is_recognised_as_a_repeat(self):
        manager, _store = build(ScriptedTransport())
        await manager._ingest([entry(BASE, 100.0)])
        await manager._ingest([entry(BASE, 100.0)])

        assert manager.counters.duplicates == 1
        assert manager.counters.quotes_stored == 2

    async def test_an_entry_for_an_instrument_we_do_not_know_is_counted(self):
        manager, store = build(ScriptedTransport())
        await manager._ingest([FeedEntry(provider_key="NSE_EQ|SOMETHING", fields={})])

        assert manager.counters.unknown_instrument == 1
        assert await store.get_quote(NIFTY.id) is None

    async def test_an_unreadable_entry_is_counted_and_does_not_stop_the_feed(self):
        manager, store = build(ScriptedTransport())
        await manager._ingest(
            [FeedEntry(provider_key=KEY, fields={"garbage": True}), entry(BASE, 100.0)]
        )

        assert manager.counters.unreadable == 1
        assert manager.counters.quotes_stored == 1
        assert manager.last_error is not None

    async def test_a_connection_delivering_nothing_reports_stale(self):
        """A live socket delivering silence is indistinguishable from a calm
        session unless something says so."""
        manager, _store = build(
            ScriptedTransport(), StreamOptions(feed_name="test", stale_after_seconds=0.0)
        )
        manager.status = FeedStatus.CONNECTED
        manager.connected_since = datetime.now(UTC) - timedelta(seconds=10)

        assert manager.health().status is FeedStatus.STALE

    async def test_health_reports_what_happened_rather_than_what_was_expected(self):
        manager, store = build(ScriptedTransport())
        await manager._ingest([entry(BASE, 100.0)])
        await manager._write_health()

        health = await store.get_health("test")
        assert health.events_received == 1
        assert health.subscribed_instruments == 1


class TestTheManagerKeepsRunning:
    async def test_a_transport_that_raises_is_retried_rather_than_fatal(self):
        transport = ScriptedTransport(fail_with=OSError("connection reset"))
        manager, store = build(
            transport,
            StreamOptions(feed_name="test", backoff=BackoffPolicy(initial_seconds=0.01, jitter=0)),
        )

        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.08)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        assert transport.runs > 1
        assert manager.counters.reconnects > 0
        assert "connection reset" in (manager.last_error or "")

    async def test_every_reconnect_resends_the_whole_subscription(self):
        transport = ScriptedTransport(fail_with=OSError("dropped"))
        manager, _store = build(
            transport,
            StreamOptions(feed_name="test", backoff=BackoffPolicy(initial_seconds=0.01, jitter=0)),
        )

        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.08)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        assert len(transport.keys_seen) > 1
        assert all(seen == {KEY} for seen in transport.keys_seen)

    async def test_stopping_leaves_the_feed_reported_as_stopped(self):
        manager, store = build(
            ScriptedTransport([entry(BASE, 100.0)]),
            StreamOptions(feed_name="test", backoff=BackoffPolicy(initial_seconds=0.01, jitter=0)),
        )
        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.05)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        health = await store.get_health("test")
        assert health.status is FeedStatus.STOPPED


class TestThePollingTransport:
    """The transport this repository can verify end to end, and therefore the
    default. Its contract is narrow: ask for what is wanted, hand back what came
    with it, and say how often it did so."""

    async def test_it_asks_only_for_what_is_subscribed(self):
        from domains.market_data.streaming.feed import PollingFeedTransport

        asked: list[set[str]] = []

        async def fetch(keys: set[str]):
            asked.append(set(keys))
            return [entry(BASE, 100.0)]

        transport = PollingFeedTransport(fetch=fetch, interval_seconds=0.02)
        manager, store = build(transport)

        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.08)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        assert asked and all(seen == {KEY} for seen in asked)
        live = await store.get_quote(NIFTY.id)
        assert live is not None and live.quote.last_price == Decimal("100.0")

    async def test_it_does_not_call_the_provider_with_nothing_subscribed(self):
        """A poll for an empty set is a request that can only return nothing,
        and providers rate-limit."""
        from domains.market_data.streaming.feed import PollingFeedTransport

        calls = 0

        async def fetch(keys: set[str]):
            nonlocal calls
            calls += 1
            return []

        transport = PollingFeedTransport(fetch=fetch, interval_seconds=0.02)
        store = LiveMarketStore(InMemoryCache())
        manager = MarketStreamManager(
            transport=transport,
            store=store,
            read_quote=read_quote,
            instrument_for_key=lambda key: None,
            options=StreamOptions(feed_name="test"),
        )

        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.06)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        assert calls == 0

    async def test_it_reports_that_it_samples_rather_than_streams(self):
        from domains.market_data.streaming.feed import PollingFeedTransport

        async def fetch(keys):
            return []

        transport = PollingFeedTransport(fetch=fetch, interval_seconds=0.5)
        assert transport.delivers_every_update is False
        assert transport.interval_seconds == 0.5


class TestAConnectionThatDeliversNothing:
    async def test_it_is_counted_separately_from_a_working_one(self):
        """A socket that opens and closes without a message is a different
        problem from one that dropped mid-stream, and the backoff has to be able
        to tell them apart."""
        transport = ScriptedTransport()
        manager, _store = build(
            transport,
            StreamOptions(feed_name="test", backoff=BackoffPolicy(initial_seconds=0.01, jitter=0)),
        )

        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.06)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        assert manager.counters.empty_connections > 0
        assert manager.counters.quotes_stored == 0

    async def test_a_connection_that_delivered_is_not_counted_as_empty(self):
        transport = ScriptedTransport([entry(BASE, 100.0)])
        manager, _store = build(
            transport,
            StreamOptions(feed_name="test", backoff=BackoffPolicy(initial_seconds=0.01, jitter=0)),
        )

        task = asyncio.create_task(manager.run())
        await asyncio.sleep(0.03)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

        assert manager.counters.quotes_stored >= 1
        assert manager.counters.empty_connections == 0
