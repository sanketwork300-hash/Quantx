"""In-process distribution of live events.

Small on purpose. Its one interesting decision is what happens when a consumer
cannot keep up: the queue is bounded, the **oldest** event is dropped, and the
drop is counted. Dropping the newest would mean a slow consumer permanently sees
an old market; blocking would mean one slow consumer stalls the feed for
everyone; and dropping silently would mean nobody ever learns that it happened.

For market data specifically, dropping the oldest is the right sacrifice: a
quote is a snapshot, and the freshest one supersedes the ones behind it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from domains.market_data.streaming.events import MarketEvent


@dataclass
class Subscriber:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1000))
    #: Events discarded because this subscriber was too slow.
    dropped: int = 0
    name: str = ""


class MarketEventBus:
    """Fan-out to in-process consumers."""

    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscribers: list[Subscriber] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped_events(self) -> int:
        return sum(subscriber.dropped for subscriber in self._subscribers)

    def subscribe(self, name: str = "") -> Subscriber:
        subscriber = Subscriber(queue=asyncio.Queue(maxsize=self._queue_size), name=name)
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    async def publish(self, event: MarketEvent) -> None:
        for subscriber in self._subscribers:
            if subscriber.queue.full():
                try:
                    subscriber.queue.get_nowait()
                    subscriber.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                    pass
            subscriber.queue.put_nowait(event)

    async def events(self, subscriber: Subscriber) -> AsyncIterator[MarketEvent]:
        while True:
            yield await subscriber.queue.get()
