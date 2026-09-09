"""Feed transports: where frames come from.

Two implementations of one interface, and the choice between them is a
deployment decision rather than an architectural one.

:class:`PollingFeedTransport` asks the REST provider on a timer. It is the
transport this repository can verify end to end, it works against any provider
with a quote endpoint, and its latency is bounded by its interval — which it
states rather than hides.

:class:`WebSocketFeedTransport` holds a socket open and hands frames to a
decoder. The connection lifetime, the subscription protocol and the frame
decoding are separated so that the parts that are the same for every feed can be
tested without the part that is specific to one.

Neither transport interprets a frame. Both yield ``FeedEntry`` objects — a
provider key and the raw fields that came with it — and the normalisation that
follows is shared with the request/response path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from domains.market_data.streaming.decoders import FeedDecodeError, FeedDecoder


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One instrument's fields as the feed sent them."""

    provider_key: str
    fields: Mapping[str, Any]
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FeedTransportError(Exception):
    """The transport could not run. Always retryable by the manager."""


class FeedTransport(ABC):
    """A source of frames, for one connection's lifetime.

    ``run`` returns when the connection ends, for any reason. Reconnection is
    the manager's job, not the transport's, so that the retry policy is written
    once rather than once per transport.
    """

    name: str = "abstract"
    #: Whether this transport delivers every change, or samples the market at
    #: an interval. Reported to callers rather than assumed, because "you have
    #: seen every tick" is a claim a sampling transport must not make.
    delivers_every_update: bool = False

    @abstractmethod
    async def run(
        self,
        desired_keys: Callable[[], set[str]],
        emit: Callable[[Sequence[FeedEntry]], Any],
        on_connected: Callable[[], Any] | None = None,
    ) -> None: ...


class PollingFeedTransport(FeedTransport):
    """Ask for quotes on a timer.

    Honest about what it is: the interval is reported, and
    ``delivers_every_update`` is False because between two polls the market may
    have moved several times and this transport did not see it. A UI fed from
    here is showing the latest state at the sample rate, which is what a UI can
    render anyway; a queue or intensity model must not be built on it, and the
    capability declaration is what stops that happening by accident.
    """

    name = "polling"
    delivers_every_update = False

    def __init__(
        self,
        fetch: Callable[[set[str]], Any],
        interval_seconds: float = 1.0,
        stop: asyncio.Event | None = None,
    ) -> None:
        self._fetch = fetch
        self._interval = max(interval_seconds, 0.05)
        self._stop = stop or asyncio.Event()

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def stop(self) -> None:
        self._stop.set()

    async def run(
        self,
        desired_keys: Callable[[], set[str]],
        emit: Callable[[Sequence[FeedEntry]], Any],
        on_connected: Callable[[], Any] | None = None,
    ) -> None:
        if on_connected is not None:
            await _maybe_await(on_connected())

        while not self._stop.is_set():
            keys = desired_keys()
            if keys:
                entries = await _maybe_await(self._fetch(keys))
                if entries:
                    await _maybe_await(emit(tuple(entries)))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)


class WebSocketConnector(ABC):
    """Opens the socket. Separated so the URL dance is testable on its own.

    Providers commonly require an authorized redirect: a REST call returns a
    short-lived URL, and the socket is opened against that rather than against a
    fixed endpoint.
    """

    @abstractmethod
    async def connect(self) -> Any:
        """Return an open connection supporting ``send``, ``recv`` and ``close``."""


class WebSocketFeedTransport(FeedTransport):
    """Hold a socket open, decode frames, resend subscriptions on reconnect."""

    name = "websocket"
    delivers_every_update = True

    def __init__(
        self,
        connector: WebSocketConnector,
        decoder: FeedDecoder,
        subscribe_message: Callable[[set[str]], str | bytes],
        stop: asyncio.Event | None = None,
        resubscribe_interval_seconds: float = 0.5,
    ) -> None:
        self._connector = connector
        self._decoder = decoder
        self._subscribe_message = subscribe_message
        self._stop = stop or asyncio.Event()
        self._resubscribe_interval = resubscribe_interval_seconds
        #: Frames that arrived and could not be read. Counted rather than
        #: raised: one malformed frame must not drop a working connection, and
        #: a rising count is the signal that the decoder is wrong.
        self.undecodable_frames = 0
        self.last_decode_error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run(
        self,
        desired_keys: Callable[[], set[str]],
        emit: Callable[[Sequence[FeedEntry]], Any],
        on_connected: Callable[[], Any] | None = None,
    ) -> None:
        try:
            connection = await self._connector.connect()
        except Exception as exc:
            raise FeedTransportError(f"could not open the feed socket: {exc}") from exc

        sent: set[str] = set()
        if on_connected is not None:
            await _maybe_await(on_connected())

        async def keep_subscriptions_current() -> None:
            nonlocal sent
            while not self._stop.is_set():
                pending = desired_keys() - sent
                if pending:
                    await connection.send(self._subscribe_message(pending))
                    sent |= pending
                await asyncio.sleep(self._resubscribe_interval)

        subscriber = asyncio.create_task(keep_subscriptions_current())
        try:
            while not self._stop.is_set():
                frame = await connection.recv()
                if frame is None:
                    break
                try:
                    decoded = self._decoder.decode(frame)
                    entries = self._decoder.entries(decoded)
                except FeedDecodeError as exc:
                    self.undecodable_frames += 1
                    self.last_decode_error = str(exc)[:200]
                    continue
                if entries:
                    await _maybe_await(
                        emit(
                            tuple(
                                FeedEntry(provider_key=key, fields=fields)
                                for key, fields in entries.items()
                            )
                        )
                    )
        finally:
            subscriber.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await subscriber
            with contextlib.suppress(Exception):
                await connection.close()


class UpstoxWebSocketConnector(WebSocketConnector):
    """Authorize, then open the socket at the URL the provider returns.

    The authorize step is the provider's own: a fixed socket URL is not
    published, and hard-coding one would break the first time they rotate it.
    """

    def __init__(
        self,
        authorize_url: str,
        token_source: Callable[[], Any],
        open_socket: Callable[[str], Any] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._authorize_url = authorize_url
        self._token = token_source
        self._open_socket = open_socket
        self._timeout = timeout_seconds

    async def _authorized_url(self) -> str:
        import httpx

        token = await _maybe_await(self._token())
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            response = await client.get(
                self._authorize_url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
        if response.status_code in {401, 403}:
            raise FeedTransportError(
                f"the feed authorize endpoint refused the credential (HTTP "
                f"{response.status_code}); the connection needs re-authorization"
            )
        if response.status_code >= 400:
            raise FeedTransportError(
                f"the feed authorize endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeedTransportError("the authorize response was not JSON") from exc

        url = None
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
        if not url:
            raise FeedTransportError(
                "the authorize response carried no redirect URI; the feed cannot be opened "
                "without one and this platform will not guess a socket address"
            )
        return str(url)

    async def connect(self) -> Any:
        url = await self._authorized_url()
        if self._open_socket is not None:
            return await _maybe_await(self._open_socket(url))
        import websockets

        return await websockets.connect(url)


def upstox_subscribe_message(keys: set[str], mode: str = "full") -> str:
    """The provider's subscribe frame.

    ``mode`` is passed through rather than defaulted silently: the provider's
    modes differ in which fields arrive, and a mode that omits depth would
    produce quotes with no bid or ask that look like a one-sided market.
    """
    return json.dumps(
        {
            "guid": uuid.uuid4().hex,
            "method": "sub",
            "data": {"mode": mode, "instrumentKeys": sorted(keys)},
        }
    )


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


async def drain(iterator: AsyncIterator[Any], limit: int) -> list[Any]:
    """Test helper: take at most ``limit`` items from an async iterator."""
    items: list[Any] = []
    async for item in iterator:
        items.append(item)
        if len(items) >= limit:
            break
    return items
