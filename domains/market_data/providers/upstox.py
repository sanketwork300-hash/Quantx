"""Upstox market data, normalised into the platform's canonical schemas.

Nothing above this file knows that Upstox exists. Its identifiers, its response
shapes and its quirks stop here; what leaves is a ``Quote``, a ``Bar`` or an
``OptionChain`` that reads the same whether it came from Upstox, a CSV or the
synthetic market.

The design point worth stating: **the mapping from their payload to our schema
is data**, held in :data:`UPSTOX_FULL_QUOTE_SPEC` and friends, and reading it
produces an account of which fields were found, which were expected and absent,
and which the payload carried that we map nowhere. A provider that renames a
field therefore surfaces as a named warning on the first response rather than as
quotes that gradually become empty.

The other design point: **a response we cannot match to the instrument we asked
about is an error, not a best guess.** Upstox keys its quote responses by a
symbol string rather than by the instrument key the request used, so the entry
is matched on the ``instrument_token`` the payload itself carries; failing that,
on being the only entry for a single-instrument request. There is no third
fallback, because the third fallback is where the wrong instrument's price gets
attached to the right instrument's id.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from domains.instruments.models import Instrument
from domains.market_data.enums import BarInterval, ProviderCapability
from domains.market_data.models import (
    Bar,
    OptionChain,
    OptionQuote,
    OrderBookLevel,
    OrderBookSnapshot,
    Quote,
)
from domains.market_data.providers.base import (
    AuthenticationFailed,
    InstrumentNotMapped,
    InvalidMarketData,
    MarketDataProvider,
    ProviderUnavailable,
)
from domains.market_data.providers.normalisation import (
    NormalisationOutcome,
    NormalisationSpec,
    normalise,
    to_decimal,
    to_timestamp,
)

PROVIDER_NAME = "upstox"

#: The full market-quote payload for one instrument.
#:
#: Mapped from the published v2 response layout. Treat it as the thing to check
#: first when quotes look wrong: :attr:`NormalisationOutcome.missing` names any
#: path here that a live response did not carry.
UPSTOX_FULL_QUOTE_SPEC = NormalisationSpec(
    name="upstox.market-quote.full.v2",
    fields={
        "instrument_token": "instrument_token",
        "symbol": "symbol",
        "last_price": "last_price",
        "volume": "volume",
        "open_interest": "oi",
        "exchange_timestamp": "timestamp",
        "last_trade_time": "last_trade_time",
        "bid_price": "depth.buy.0.price",
        "bid_size": "depth.buy.0.quantity",
        "ask_price": "depth.sell.0.price",
        "ask_size": "depth.sell.0.quantity",
        "open": "ohlc.open",
        "high": "ohlc.high",
        "low": "ohlc.low",
        "close": "ohlc.close",
    },
    optional=frozenset({"last_trade_time", "open_interest", "volume", "symbol"}),
    #: Depth beyond the top of book is read separately by
    #: :meth:`UpstoxMarketDataProvider.get_order_book`; listing the prefix here
    #: keeps the unmapped-field alarm meaningful rather than permanently noisy.
    ignored_prefixes=("depth.",),
    provenance=(
        "mapped from the published Upstox v2 full market-quote layout; the "
        "fields_missing/fields_unmapped report on every read is what verifies it"
    ),
)

#: The LTP-only payload. A separate spec because it is a different response,
#: not a subset with things missing — reusing the full spec would report a dozen
#: missing fields on every healthy LTP call and train everyone to ignore them.
UPSTOX_LTP_SPEC = NormalisationSpec(
    name="upstox.market-quote.ltp.v2",
    fields={
        "instrument_token": "instrument_token",
        "last_price": "last_price",
    },
    provenance="mapped from the published Upstox v2 LTP layout",
)

UPSTOX_CANDLE_FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "open_interest")

#: Platform interval -> the provider's own interval naming. Absent entries are
#: refused rather than approximated: a caller asking for 5-minute bars must not
#: silently receive 1-minute ones.
BAR_INTERVALS: Mapping[BarInterval, tuple[str, str]] = {
    BarInterval.M1: ("minutes", "1"),
    BarInterval.M5: ("minutes", "5"),
    BarInterval.M15: ("minutes", "15"),
    BarInterval.H1: ("hours", "1"),
    BarInterval.D1: ("days", "1"),
}


class AccessTokenSource(Protocol):
    """Where the provider gets a credential.

    A protocol rather than an import of the credential vault: the provider must
    not depend on how a token is stored, and a test must be able to supply one
    without a database.
    """

    async def __call__(self) -> str: ...


class InstrumentDirectory(Protocol):
    """The join between platform ids and provider keys.

    Implemented over the instrument master in the application layer. The
    provider takes it as a collaborator so that it holds no persistence of its
    own and can be exercised entirely in memory.
    """

    async def provider_key(self, instrument_id: uuid.UUID) -> str | None: ...

    async def instrument(self, instrument_id: uuid.UUID) -> Instrument | None: ...

    async def by_provider_key(self, key: str) -> Instrument | None: ...

    async def option_contracts(
        self, underlying_id: uuid.UUID, expiry: date | None = None
    ) -> Sequence[Instrument]: ...


@dataclass(frozen=True, slots=True)
class UpstoxEndpoints:
    """Where the provider lives.

    Configuration rather than constants: a provider moving an endpoint should be
    a deployment change, and pinning the API version in one visible place is
    what makes an upgrade a decision rather than a surprise.
    """

    base_url: str = "https://api.upstox.com"
    full_quote_path: str = "/v2/market-quote/quotes"
    ltp_path: str = "/v2/market-quote/ltp"
    historical_path: str = "/v2/historical-candle"
    intraday_path: str = "/v2/historical-candle/intraday"
    instruments_url: str = (
        "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    )
    timeout_seconds: float = 15.0

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"


class UpstoxTransport(Protocol):
    """The one place that touches the network.

    Extracted so every test in this repository exercises the real normalisation
    against a recorded payload, and none of them can reach a live broker.
    """

    async def get_json(
        self, url: str, params: Mapping[str, str] | None, token: str
    ) -> tuple[int, Any]: ...


class HttpUpstoxTransport:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    async def get_json(
        self, url: str, params: Mapping[str, str] | None, token: str
    ) -> tuple[int, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    url,
                    params=dict(params or {}),
                    headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(PROVIDER_NAME, str(exc)) from exc

        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None


def _match_entry(
    data: Mapping[str, Any], instrument_key: str
) -> tuple[str, Mapping[str, Any]] | None:
    """Find the entry that is about ``instrument_key``.

    Upstox keys the response map by a display symbol rather than by the key the
    request used. Matching on the ``instrument_token`` inside each entry is
    exact; the single-entry fallback is safe only because a single-instrument
    request can have only one answer. Anything else returns ``None`` so the
    caller can refuse rather than attach someone else's price.
    """
    for key, entry in data.items():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("instrument_token") or "") == instrument_key:
            return key, entry
    if len(data) == 1:
        key, entry = next(iter(data.items()))
        if isinstance(entry, Mapping):
            return key, entry
    return None


class UpstoxMarketDataProvider(MarketDataProvider):
    """Snapshot market data over Upstox's REST API.

    Streaming lives in ``domains/market_data/streaming``; this class is the
    request/response half, and is also what the stream falls back to when a feed
    is unavailable.
    """

    name = PROVIDER_NAME
    #: INSTRUMENTS is deliberately absent. This provider serves market data; the
    #: instrument directory comes from ``UpstoxInstrumentMaster``. Declaring a
    #: capability the class does not serve would let a caller plan around it and
    #: then fail halfway through, which is exactly what the capability
    #: declaration exists to prevent.
    capabilities = frozenset(
        {
            ProviderCapability.QUOTES,
            ProviderCapability.OPTION_CHAINS,
            ProviderCapability.ORDER_BOOK,
            ProviderCapability.BARS,
        }
    )

    def __init__(
        self,
        directory: InstrumentDirectory,
        token_source: AccessTokenSource,
        transport: UpstoxTransport | None = None,
        endpoints: UpstoxEndpoints | None = None,
        quote_spec: NormalisationSpec | None = None,
    ) -> None:
        self._directory = directory
        self._token = token_source
        self._endpoints = endpoints or UpstoxEndpoints()
        self._transport = transport or HttpUpstoxTransport(self._endpoints.timeout_seconds)
        self._quote_spec = quote_spec or UPSTOX_FULL_QUOTE_SPEC
        #: The normalisation report from the most recent read, for provenance.
        self.last_outcome: NormalisationOutcome | None = None

    @property
    def dataset_version(self) -> str:
        return f"upstox:{self._quote_spec.name}"

    # ------------------------------------------------------------- requests
    async def _get(self, path: str, params: Mapping[str, str]) -> Any:
        token = await self._token()
        status, payload = await self._transport.get_json(self._endpoints.url(path), params, token)

        if status in {401, 403}:
            raise AuthenticationFailed(
                PROVIDER_NAME,
                f"the token was refused with HTTP {status}; the connection needs re-authorization",
            )
        if status >= 400:
            detail = ""
            if isinstance(payload, Mapping):
                detail = str(payload.get("message") or payload.get("errors") or "")[:200]
            raise ProviderUnavailable(PROVIDER_NAME, detail or f"HTTP {status}", status)
        if not isinstance(payload, Mapping):
            raise InvalidMarketData(PROVIDER_NAME, "the response body was not a JSON object")

        data = payload.get("data")
        if data is None:
            raise InvalidMarketData(
                PROVIDER_NAME,
                "the response carried no 'data'",
                evidence=tuple(sorted(str(key) for key in payload)),
            )
        return data

    # ------------------------------------------------------------ interface
    async def get_instrument(self, instrument_id: uuid.UUID) -> Instrument | None:
        return await self._directory.instrument(instrument_id)

    async def get_quote(self, instrument_id: uuid.UUID) -> Quote | None:
        instrument_key = await self._directory.provider_key(instrument_id)
        if instrument_key is None:
            raise InstrumentNotMapped(PROVIDER_NAME, instrument_id)

        data = await self._get(self._endpoints.full_quote_path, {"instrument_key": instrument_key})
        if not isinstance(data, Mapping) or not data:
            return None

        matched = _match_entry(data, instrument_key)
        if matched is None:
            raise InvalidMarketData(
                PROVIDER_NAME,
                f"no entry in the response is identifiable as {instrument_key!r}; refusing to "
                "attach an unidentified quote to this instrument",
                evidence=tuple(sorted(str(key) for key in data)),
            )
        _key, entry = matched
        return self.quote_from_entry(instrument_id, entry)

    def quote_from_entry(self, instrument_id: uuid.UUID, entry: Mapping[str, Any]) -> Quote:
        """Read one provider entry into a canonical quote.

        Public because the streaming normaliser reads the same shape, and two
        readings of one payload is two chances to disagree.
        """
        outcome = normalise(entry, self._quote_spec)
        self.last_outcome = outcome
        values = outcome.values

        received = datetime.now(UTC)
        exchange_timestamp = values.get("exchange_timestamp") or values.get("last_trade_time")
        if exchange_timestamp is None:
            # No event time means no staleness measurement, and a quote whose
            # age cannot be known must not be dated to the moment we read it.
            raise InvalidMarketData(
                PROVIDER_NAME,
                "the entry carried no readable exchange timestamp, so the quote's age "
                "could not be established",
                evidence=outcome.missing,
            )

        return Quote(
            instrument_id=instrument_id,
            exchange_timestamp=exchange_timestamp,
            receive_timestamp=received,
            source=PROVIDER_NAME,
            bid_price=to_decimal(values.get("bid_price")),
            bid_size=to_decimal(values.get("bid_size")),
            ask_price=to_decimal(values.get("ask_price")),
            ask_size=to_decimal(values.get("ask_size")),
            last_price=to_decimal(values.get("last_price")),
            volume=to_decimal(values.get("volume")),
            open_interest=to_decimal(values.get("open_interest")),
            metadata={
                "provider": PROVIDER_NAME,
                "provider_symbol": values.get("symbol"),
                "provider_instrument_key": values.get("instrument_token"),
                #: The full reading report rides along only when the reading was
                #: *not* clean. On a healthy feed it is identical for every
                #: quote from the same spec, and carrying it per tick would put
                #: a kilobyte of unchanging text into every stored quote and
                #: every cache write. When it is not clean, it is exactly what
                #: an operator needs and it is here.
                **(
                    outcome.to_provenance()
                    if (outcome.missing or outcome.unmapped)
                    else {"normalisation_spec": outcome.spec_name}
                ),
            },
        )

    async def raw_quotes(self, instrument_keys: Sequence[str]) -> Mapping[str, Mapping[str, Any]]:
        """Fetch several instruments at once, keyed by *our* request key.

        Returns the provider's own entries unnormalised, because the caller is
        the streaming path and it normalises through exactly the same spec this
        class uses. Re-keying to the requested identifier here is what makes the
        two paths agree about which instrument an entry is about — the response
        is keyed by a display symbol, which is not what anything upstream knows.
        """
        if not instrument_keys:
            return {}
        data = await self._get(
            self._endpoints.full_quote_path,
            {"instrument_key": ",".join(sorted(set(instrument_keys)))},
        )
        if not isinstance(data, Mapping):
            raise InvalidMarketData(PROVIDER_NAME, "the quote response was not a JSON object")

        wanted = set(instrument_keys)
        entries: dict[str, Mapping[str, Any]] = {}
        for entry in data.values():
            if not isinstance(entry, Mapping):
                continue
            token = str(entry.get("instrument_token") or "")
            if token in wanted:
                entries[token] = entry
        if not entries and len(data) == 1 and len(wanted) == 1:
            # A single-instrument request has one possible answer, so an entry
            # that carries no instrument_token is still unambiguous.
            only = next(iter(data.values()))
            if isinstance(only, Mapping):
                entries[next(iter(wanted))] = only
        return entries

    async def get_order_book(
        self, instrument_id: uuid.UUID, depth: int = 20
    ) -> OrderBookSnapshot | None:
        instrument_key = await self._directory.provider_key(instrument_id)
        if instrument_key is None:
            raise InstrumentNotMapped(PROVIDER_NAME, instrument_id)

        data = await self._get(self._endpoints.full_quote_path, {"instrument_key": instrument_key})
        if not isinstance(data, Mapping) or not data:
            return None
        matched = _match_entry(data, instrument_key)
        if matched is None:
            raise InvalidMarketData(PROVIDER_NAME, f"no entry identifiable as {instrument_key!r}")
        _key, entry = matched

        book = entry.get("depth")
        if not isinstance(book, Mapping):
            return None
        timestamp = to_timestamp(entry.get("timestamp"))
        if timestamp is None:
            raise InvalidMarketData(
                PROVIDER_NAME, "the depth entry carried no readable exchange timestamp"
            )

        return OrderBookSnapshot(
            instrument_id=instrument_id,
            exchange_timestamp=timestamp,
            receive_timestamp=datetime.now(UTC),
            bids=_levels(book.get("buy"), depth, descending=True),
            asks=_levels(book.get("sell"), depth, descending=False),
            source=PROVIDER_NAME,
        )

    async def get_bars(
        self,
        instrument_id: uuid.UUID,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        instrument_key = await self._directory.provider_key(instrument_id)
        if instrument_key is None:
            raise InstrumentNotMapped(PROVIDER_NAME, instrument_id)

        unit_and_size = BAR_INTERVALS.get(interval)
        if unit_and_size is None:
            raise InvalidMarketData(
                PROVIDER_NAME,
                f"interval {interval} has no recorded provider equivalent; a request for one "
                "bar size must not be answered with another",
            )
        unit, size = unit_and_size

        path = (
            f"{self._endpoints.historical_path}/{instrument_key}/{unit}/{size}"
            f"/{end.date().isoformat()}/{start.date().isoformat()}"
        )
        data = await self._get(path, {})
        candles = data.get("candles") if isinstance(data, Mapping) else None
        if not isinstance(candles, Sequence):
            raise InvalidMarketData(PROVIDER_NAME, "the historical response carried no candles")

        bars: list[Bar] = []
        for row in candles:
            bar = _bar_from_candle(row, instrument_id, interval)
            if bar is not None and start <= bar.start_timestamp <= end:
                bars.append(bar)
        return tuple(sorted(bars, key=lambda item: item.start_timestamp))

    async def get_option_chain(
        self, underlying_id: uuid.UUID, expiry: date | None = None
    ) -> OptionChain:
        """Quote every mapped contract on this underlying, in one request.

        The chain is assembled from the platform's own instrument master rather
        than from a provider chain endpoint, so a contract the platform does not
        know about cannot enter a chain through a side door and arrive without
        an identity.
        """
        contracts = await self._directory.option_contracts(underlying_id, expiry)
        if not contracts:
            return OptionChain(
                underlying_id=underlying_id,
                as_of=datetime.now(UTC),
                quotes=(),
                source=PROVIDER_NAME,
                metadata={"reason": "no mapped option contracts for this underlying"},
            )

        keys: dict[str, Instrument] = {}
        for contract in contracts:
            key = await self._directory.provider_key(contract.id)
            if key is not None:
                keys[key] = contract

        if not keys:
            return OptionChain(
                underlying_id=underlying_id,
                as_of=datetime.now(UTC),
                quotes=(),
                source=PROVIDER_NAME,
                metadata={"reason": "no provider identifiers recorded for these contracts"},
            )

        data = await self._get(
            self._endpoints.full_quote_path, {"instrument_key": ",".join(sorted(keys))}
        )
        if not isinstance(data, Mapping):
            raise InvalidMarketData(PROVIDER_NAME, "the chain response was not a JSON object")

        quotes: list[OptionQuote] = []
        unmatched: list[str] = []
        for entry in data.values():
            if not isinstance(entry, Mapping):
                continue
            token = str(entry.get("instrument_token") or "")
            contract = keys.get(token)
            if contract is None:
                unmatched.append(token or "<no instrument_token>")
                continue
            quote = self.quote_from_entry(contract.id, entry)
            quotes.append(
                OptionQuote(
                    quote=quote,
                    underlying_id=underlying_id,
                    expiry=contract.expiry,
                    strike=contract.strike,
                    option_type=contract.option_type,
                    expiry_timestamp=_expiry_timestamp(contract),
                )
            )

        return OptionChain(
            underlying_id=underlying_id,
            as_of=datetime.now(UTC),
            quotes=tuple(quotes),
            source=PROVIDER_NAME,
            metadata={
                "requested": len(keys),
                "returned": len(data),
                "matched": len(quotes),
                #: Entries the provider returned that no requested contract
                #: claims. Reported rather than dropped: it means the request
                #: and the response disagree about what was asked for.
                "unmatched_entries": sorted(unmatched),
            },
        )


def _levels(raw: Any, depth: int, descending: bool) -> tuple[OrderBookLevel, ...]:
    if not isinstance(raw, Sequence):
        return ()
    levels: list[OrderBookLevel] = []
    for item in raw[:depth]:
        if not isinstance(item, Mapping):
            continue
        price = to_decimal(item.get("price"))
        quantity = to_decimal(item.get("quantity"))
        if price is None or quantity is None or price <= 0:
            # A zero-price level is padding, not a resting order. Keeping it
            # would put a bid of nothing at the top of the book.
            continue
        orders = item.get("orders")
        levels.append(
            OrderBookLevel(
                price=price,
                quantity=quantity,
                order_count=int(orders) if isinstance(orders, int) else None,
            )
        )
    levels.sort(key=lambda level: level.price, reverse=descending)
    return tuple(levels)


def _bar_from_candle(row: Any, instrument_id: uuid.UUID, interval: BarInterval) -> Bar | None:
    """Read one candle. Positional, because that is how the provider sends them.

    A row that is short, or whose values do not read as numbers, is skipped
    rather than filled in: a bar with an invented close is worse than a gap.
    """
    if not isinstance(row, Sequence) or isinstance(row, str | bytes) or len(row) < 6:
        return None
    start = to_timestamp(row[0])
    values = [to_decimal(row[index]) for index in range(1, 6)]
    if start is None or any(value is None for value in values):
        return None
    open_, high, low, close, volume = values
    try:
        return Bar(
            instrument_id=instrument_id,
            interval=interval,
            start_timestamp=start,
            end_timestamp=start + timedelta(seconds=interval.seconds),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source=PROVIDER_NAME,
        )
    except ValueError:
        # Bar validates high >= low and open/close inside the range. A candle
        # that fails those is not a bar we can repair.
        return None


def _expiry_timestamp(contract: Instrument) -> datetime | None:
    raw = contract.metadata.get("expiry_timestamp")
    return to_timestamp(raw) if raw else None
