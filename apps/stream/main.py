"""The market stream worker.

A separate process from the API on purpose. It holds one connection to the
provider, writes what arrives into the live store, and the API reads that store;
nothing in a request path ever opens a feed. That is what keeps two readers from
getting two different answers about the same instant.

Run it with::

    python -m apps.stream.main

It exits non-zero, with the reason, when it is configured for something it
cannot do — an unset provider credential, a websocket transport with no decoder
for the provider's frames. It does not fall back to generated prices.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from domains.broker_auth.enums import BrokerProvider
from domains.broker_auth.errors import BrokerAuthError
from domains.broker_auth.service import BrokerAuthService
from domains.instruments.models import Instrument
from domains.instruments.service import InstrumentService
from domains.market_data.live import LiveMarketDataService
from domains.market_data.providers.factory import ProviderKind, ProviderNotAvailable
from domains.market_data.providers.upstox import (
    UpstoxEndpoints,
    UpstoxMarketDataProvider,
)
from domains.market_data.streaming.decoders import (
    DecoderUnavailable,
    ProtobufFeedDecoder,
)
from domains.market_data.streaming.feed import (
    FeedEntry,
    FeedTransport,
    PollingFeedTransport,
    UpstoxWebSocketConnector,
    WebSocketFeedTransport,
    upstox_subscribe_message,
)
from domains.market_data.streaming.live_state import LiveMarketStore
from domains.market_data.streaming.manager import MarketStreamManager, StreamOptions
from domains.market_data.streaming.reconnect import BackoffPolicy
from domains.users.orm import UserORM
from infrastructure.cache.client import get_cache
from infrastructure.database.session import get_sessionmaker
from infrastructure.observability.logging import configure_logging, get_logger
from infrastructure.settings import Settings, get_settings

logger = get_logger(__name__)


class StreamNotRunnable(Exception):
    """The worker is configured for something it cannot do."""


@dataclass
class MarketDataAccount:
    """Whose broker connection the shared feed uses.

    Market data is not per-user — one entitlement serves the deployment — so the
    account whose connection is used is named explicitly rather than being
    whichever user happened to connect first. It is also the account whose
    licensing terms govern what may be shown to whom (see docs/credentials.md
    and the licensing note in docs/live-market-data.md).
    """

    user_id: uuid.UUID
    email: str


async def resolve_account(settings: Settings) -> MarketDataAccount:
    from sqlalchemy import select

    email = (settings.market_data_account_email or "").strip().lower()
    if not email:
        raise StreamNotRunnable(
            "QIP_MARKET_DATA_ACCOUNT_EMAIL is not set. The feed uses one account's broker "
            "connection; name it rather than letting the worker choose one."
        )

    maker = get_sessionmaker()
    async with maker() as session:
        row = (
            await session.execute(select(UserORM).where(UserORM.email == email))
        ).scalar_one_or_none()
    if row is None:
        raise StreamNotRunnable(f"no account exists for {email!r}")
    return MarketDataAccount(user_id=row.id, email=email)


class VaultTokenSource:
    """Supplies the feed's credential from the broker credential vault.

    Opens a short session per call rather than holding one: the vault may need
    to renew the token, which is a write, and a long-lived session held open
    across a day of streaming is a transaction nobody closed.
    """

    def __init__(self, settings: Settings, account: MarketDataAccount) -> None:
        self._settings = settings
        self._account = account

    async def __call__(self) -> str:
        maker = get_sessionmaker()
        async with maker() as session:
            service = BrokerAuthService(session, self._settings)
            try:
                credential = await service.access_token(
                    self._account.user_id, BrokerProvider.UPSTOX
                )
            except BrokerAuthError as exc:
                raise StreamNotRunnable(
                    f"the market-data account {self._account.email} has no usable broker "
                    f"connection: {exc}. Connect it at /connections and restart."
                ) from exc
            await session.commit()
            return credential.token


class SubscriptionSync:
    """Keeps the manager's subscriptions in step with registered interest.

    Interest is registered by the API into the live store; this loop turns it
    into instruments and provider keys. It resolves each instrument once and
    keeps it, so the message path never touches a database.
    """

    def __init__(
        self,
        manager: MarketStreamManager,
        live: LiveMarketDataService,
        feed: str,
        interval_seconds: float = 5.0,
    ) -> None:
        self._manager = manager
        self._live = live
        self._feed = feed
        self._interval = interval_seconds
        self._known: dict[uuid.UUID, tuple[Instrument, str]] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def instrument_for_key(self, provider_key: str) -> Instrument | None:
        for instrument, key in self._known.values():
            if key == provider_key:
                return instrument
        return None

    async def run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self._sync_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def _sync_once(self) -> None:
        wanted = await self._live.interest(self._feed)

        new = wanted - set(self._known)
        resolved: list[tuple[Instrument, str]] = []
        for instrument_id in new:
            instrument = await self._live.directory.instrument(instrument_id)
            key = await self._live.directory.provider_key(instrument_id)
            if instrument is None or key is None:
                # Interest in something the instrument master does not cover.
                # Left unresolved rather than guessed at; it will be retried
                # after the next master load.
                continue
            self._known[instrument_id] = (instrument, key)
            resolved.append((instrument, key))
        if resolved:
            self._manager.subscribe(resolved)

        gone = set(self._known) - wanted
        if gone:
            self._manager.unsubscribe(gone)
            for instrument_id in gone:
                self._known.pop(instrument_id, None)


def build_transport(
    settings: Settings,
    provider: UpstoxMarketDataProvider,
    token_source: VaultTokenSource,
) -> FeedTransport:
    if settings.market_stream_transport == "polling":

        async def fetch(keys: set[str]) -> Sequence[FeedEntry]:
            entries = await provider.raw_quotes(sorted(keys))
            return tuple(
                FeedEntry(provider_key=key, fields=fields) for key, fields in entries.items()
            )

        return PollingFeedTransport(
            fetch=fetch, interval_seconds=settings.market_stream_poll_interval_seconds
        )

    if settings.market_stream_transport != "websocket":
        raise StreamNotRunnable(
            f"QIP_MARKET_STREAM_TRANSPORT={settings.market_stream_transport!r} is not one of "
            "'polling' or 'websocket'"
        )

    if settings.upstox_feed_proto_module:
        decoder = ProtobufFeedDecoder(
            settings.upstox_feed_proto_module, settings.upstox_feed_proto_message
        )
        try:
            decoder._load()  # noqa: SLF001 - fail at startup, not on the first frame
        except DecoderUnavailable as exc:
            raise StreamNotRunnable(str(exc)) from exc
    else:
        # A JSON decoder against a binary feed will read nothing. Saying so here
        # beats a connection that stays up and delivers zero quotes.
        raise StreamNotRunnable(
            "the websocket transport needs a frame decoder for this provider. Generate the "
            "provider's protobuf module from their published .proto and set "
            "QIP_UPSTOX_FEED_PROTO_MODULE, or use QIP_MARKET_STREAM_TRANSPORT=polling."
        )

    connector = UpstoxWebSocketConnector(
        authorize_url=settings.upstox_feed_authorize_url, token_source=token_source
    )
    return WebSocketFeedTransport(
        connector=connector,
        decoder=decoder,
        subscribe_message=lambda keys: upstox_subscribe_message(keys, settings.upstox_feed_mode),
    )


async def run(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    if settings.market_data_provider != ProviderKind.UPSTOX:
        raise StreamNotRunnable(
            f"the stream worker serves the upstox provider; QIP_MARKET_DATA_PROVIDER is "
            f"{settings.market_data_provider!r}"
        )

    account = await resolve_account(settings)
    token_source = VaultTokenSource(settings, account)
    cache = get_cache(settings)
    store = LiveMarketStore(cache, settings.live_quote_ttl_seconds)

    maker = get_sessionmaker()
    async with maker() as session:
        live = LiveMarketDataService(
            InstrumentService(session),
            store,
            source=settings.market_data_provider,
            subscription_ttl_seconds=settings.live_subscription_ttl_seconds,
        )
        provider = UpstoxMarketDataProvider(
            directory=live.directory,
            token_source=token_source,
            endpoints=UpstoxEndpoints(base_url=settings.upstox_api_base_url),
        )
        transport = build_transport(settings, provider, token_source)

        manager = MarketStreamManager(
            transport=transport,
            store=store,
            read_quote=provider.quote_from_entry,
            instrument_for_key=lambda key: sync.instrument_for_key(key),
            options=StreamOptions(
                feed_name=settings.market_data_provider,
                stale_after_seconds=settings.market_stream_stale_after_seconds,
                backoff=BackoffPolicy(),
            ),
        )
        sync = SubscriptionSync(manager, live, settings.market_data_provider)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(getattr(signal, signal_name), stop.set)

        logger.info(
            "market_stream_starting",
            provider=settings.market_data_provider,
            transport=transport.name,
            account=account.email,
            delivers_every_update=transport.delivers_every_update,
        )

        tasks = [asyncio.create_task(manager.run()), asyncio.create_task(sync.run())]
        await stop.wait()

        logger.info("market_stream_stopping", **manager.counters.to_dict())
        sync.stop()
        manager.stop()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except (StreamNotRunnable, ProviderNotAvailable) as exc:
        print(f"market stream cannot start: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
