"""Market-data provider interface.

The rule this interface exists to enforce: **no quant engine ever calls NSE,
Binance, IBKR, a broker API, Yahoo or a CSV parser.** Everything enters through
a provider, is normalised to the canonical schemas, is scored by the quality
engine, and is assembled into a ``MarketState`` before a model sees it.

Providers declare :class:`~domains.market_data.enums.ProviderCapability` up
front instead of raising ``NotImplementedError`` at call time, so a caller can
plan around a missing capability rather than failing halfway through a
calculation.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date, datetime

from domains.instruments.models import Instrument
from domains.market_data.enums import BarInterval, ProviderCapability
from domains.market_data.models import (
    Bar,
    OptionChain,
    OrderBookSnapshot,
    Quote,
    Trade,
)


class ProviderError(Exception):
    """A provider could not satisfy a request."""


class CapabilityNotSupported(ProviderError):
    """The provider does not offer this kind of data.

    The service layer converts this into a ``PARTIAL`` result with a named
    warning; it is never allowed to surface as a 500.
    """

    def __init__(self, provider: str, capability: ProviderCapability) -> None:
        super().__init__(f"provider {provider!r} does not support {capability}")
        self.provider = provider
        self.capability = capability


class ProviderUnavailable(ProviderError):
    """The provider could not be reached, or refused to serve the request.

    Distinct from a provider that answered and had nothing: "we could not ask"
    and "there is no data" lead to different decisions, and collapsing them is
    how an outage comes to look like a quiet market.
    """

    def __init__(self, provider: str, detail: str, status_code: int | None = None) -> None:
        super().__init__(f"{provider} unavailable: {detail}")
        self.provider = provider
        self.detail = detail
        self.status_code = status_code


class AuthenticationFailed(ProviderError):
    """The provider rejected the credential in use.

    Carried separately because it is the one provider failure a *user* can fix,
    by reconnecting; everything else is an operator's problem.
    """

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"{provider} rejected the credential: {detail}")
        self.provider = provider
        self.detail = detail


class InvalidMarketData(ProviderError):
    """A response arrived and could not be read as market data.

    Raised rather than returning a quote of ``None``s: a malformed payload is a
    broken integration, and a calculation must not be handed something that
    looks like a market with nothing in it.
    """

    def __init__(self, provider: str, detail: str, evidence: tuple[str, ...] = ()) -> None:
        super().__init__(f"{provider} returned unreadable market data: {detail}")
        self.provider = provider
        self.detail = detail
        self.evidence = evidence


class InstrumentNotMapped(ProviderError):
    """The platform holds no provider identifier for this instrument.

    Almost always means the instrument master has not been loaded, or was loaded
    with a segment selection that excluded this contract.
    """

    def __init__(self, provider: str, instrument_id) -> None:
        super().__init__(f"{provider} has no identifier recorded for instrument {instrument_id}")
        self.provider = provider
        self.instrument_id = instrument_id


class MarketDataProvider(ABC):
    #: Stable identifier recorded in ``Quote.source`` and in provenance.
    name: str = "abstract"
    capabilities: frozenset[ProviderCapability] = frozenset()

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities

    def require(self, capability: ProviderCapability) -> None:
        if not self.supports(capability):
            raise CapabilityNotSupported(self.name, capability)

    @property
    def dataset_version(self) -> str:
        """Version/digest of the underlying dataset, recorded in provenance.

        Two analyses that report the same dataset version saw the same bytes.
        """
        return "unversioned"

    # ------------------------------------------------------------ interface
    @abstractmethod
    async def get_instrument(self, instrument_id: uuid.UUID) -> Instrument | None: ...

    @abstractmethod
    async def get_quote(self, instrument_id: uuid.UUID) -> Quote | None: ...

    @abstractmethod
    async def get_option_chain(
        self, underlying_id: uuid.UUID, expiry: date | None = None
    ) -> OptionChain: ...

    async def get_order_book(
        self, instrument_id: uuid.UUID, depth: int = 20
    ) -> OrderBookSnapshot | None:
        self.require(ProviderCapability.ORDER_BOOK)
        raise NotImplementedError  # pragma: no cover - guarded by require()

    async def get_trades(
        self, instrument_id: uuid.UUID, start: datetime, end: datetime
    ) -> Sequence[Trade]:
        self.require(ProviderCapability.TRADES)
        raise NotImplementedError  # pragma: no cover - guarded by require()

    async def get_bars(
        self,
        instrument_id: uuid.UUID,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        self.require(ProviderCapability.BARS)
        raise NotImplementedError  # pragma: no cover - guarded by require()

    # ------------------------------------------------------------- helpers
    async def list_instruments(self) -> Sequence[Instrument]:
        self.require(ProviderCapability.INSTRUMENTS)
        raise NotImplementedError  # pragma: no cover - guarded by require()

    async def health(self) -> bool:
        return True
