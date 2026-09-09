"""Live market data as the rest of the platform sees it.

The application layer over the provider and the stream: it owns the instrument
master, the directory that joins provider identifiers to platform ones, and the
assembly of a ``MarketState`` from whatever the live store currently holds.

The rule this file exists to keep is the one from the architecture: **a
calculation never reads a feed.** It reads a snapshot. So the live path ends
here, at a ``MarketState`` built from live quotes, and everything downstream is
the same code that runs against a chain uploaded from a CSV.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from domains.instruments.enums import AssetClass
from domains.instruments.models import Instrument
from domains.instruments.service import InstrumentService
from domains.market_data.market_state import MarketState, MarketStateBuilder
from domains.market_data.providers.upstox import PROVIDER_NAME as UPSTOX
from domains.market_data.providers.upstox_master import (
    InstrumentMasterOptions,
    InstrumentMasterResult,
    UpstoxInstrumentMaster,
)
from domains.market_data.streaming.live_state import FeedHealth, LiveMarketStore, LiveQuote


@dataclass(frozen=True, slots=True)
class LiveQuoteView:
    """A live quote with the things a caller always needs beside it."""

    instrument: Instrument
    live: LiveQuote
    as_of: datetime

    @property
    def age_seconds(self) -> float:
        return self.live.age_seconds(self.as_of)


class PersistedInstrumentDirectory:
    """Joins platform instrument ids to a provider's identifiers.

    Reads the alias table, which is what the instrument master writes. The
    instrument's own metadata carries the same key, so the common case costs no
    extra query; the alias table remains the authority when they disagree,
    because it is the one with a uniqueness constraint on it.
    """

    def __init__(self, instruments: InstrumentService, source: str = UPSTOX) -> None:
        self._instruments = instruments
        self._source = source
        self._metadata_key = f"{source}_instrument_key"

    async def instrument(self, instrument_id: uuid.UUID) -> Instrument | None:
        return await self._instruments.get(instrument_id)

    async def provider_key(self, instrument_id: uuid.UUID) -> str | None:
        instrument = await self._instruments.get(instrument_id)
        if instrument is not None:
            from_metadata = instrument.metadata.get(self._metadata_key)
            if from_metadata:
                return str(from_metadata)
        for source, alias in await self._instruments.list_aliases(instrument_id):
            if source == self._source:
                return alias
        return None

    async def by_provider_key(self, key: str) -> Instrument | None:
        return await self._instruments.find_by_alias(self._source, key)

    async def option_contracts(
        self, underlying_id: uuid.UUID, expiry: date | None = None
    ) -> Sequence[Instrument]:
        return await self._instruments.search(
            asset_class=AssetClass.OPTION,
            underlying_id=underlying_id,
            expiry=expiry,
            limit=1000,
        )


class LiveMarketDataService:
    """Loading the instrument master, and reading live state back out."""

    def __init__(
        self,
        instruments: InstrumentService,
        store: LiveMarketStore,
        source: str = UPSTOX,
        subscription_ttl_seconds: int = 900,
    ) -> None:
        self._instruments = instruments
        self._store = store
        self._source = source
        self._subscription_ttl = subscription_ttl_seconds
        self.directory = PersistedInstrumentDirectory(instruments, source)

    # ---------------------------------------------------- instrument master
    async def load_instrument_master(
        self,
        rows: Iterable[Mapping[str, object]],
        options: InstrumentMasterOptions | None = None,
    ) -> InstrumentMasterResult:
        """Turn a provider instrument file into platform instruments and aliases.

        Underlyings are written before the contracts that reference them,
        because the foreign key from a contract to its underlying is real and a
        contract whose underlying is missing is not an instrument the platform
        will accept.
        """
        result = UpstoxInstrumentMaster(options).load(rows)

        underlyings = [
            instrument
            for instrument in result.instruments
            if instrument.asset_class in {AssetClass.EQUITY, AssetClass.INDEX}
        ]
        contracts = [
            instrument
            for instrument in result.instruments
            if instrument.asset_class in {AssetClass.OPTION, AssetClass.FUTURE}
        ]
        for batch in (underlyings, contracts):
            if batch:
                await self._instruments.upsert_many(batch)

        # In one batch: an instrument file runs to tens of thousands of rows,
        # and a select-then-insert per alias would dominate the whole load.
        await self._instruments.add_aliases(
            self._source,
            {
                instrument.id: result.provider_keys[instrument.id]
                for instrument in result.instruments
                if result.provider_keys.get(instrument.id)
            },
        )
        return result

    # ------------------------------------------------------------ live read
    async def live_quote(self, instrument_id: uuid.UUID) -> LiveQuoteView | None:
        instrument = await self._instruments.get(instrument_id)
        if instrument is None:
            return None
        live = await self._store.get_quote(instrument_id)
        if live is None:
            return None
        return LiveQuoteView(instrument=instrument, live=live, as_of=datetime.now(UTC))

    async def live_quotes(
        self, instrument_ids: Sequence[uuid.UUID]
    ) -> tuple[list[LiveQuoteView], list[uuid.UUID]]:
        """Returns ``(found, missing)``.

        The missing list is returned rather than silently omitted: "we hold no
        live price for this instrument" is information the caller needs, and an
        endpoint that answers with a shorter list than it was asked about leaves
        them to work out which one is absent.
        """
        as_of = datetime.now(UTC)
        found: list[LiveQuoteView] = []
        missing: list[uuid.UUID] = []
        for instrument_id in instrument_ids:
            instrument = await self._instruments.get(instrument_id)
            live = await self._store.get_quote(instrument_id) if instrument else None
            if instrument is None or live is None:
                missing.append(instrument_id)
                continue
            found.append(LiveQuoteView(instrument=instrument, live=live, as_of=as_of))
        return found, missing

    async def register_interest(
        self, feed: str, instrument_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        return await self._store.register_interest(feed, instrument_ids, self._subscription_ttl)

    async def drop_interest(self, feed: str, instrument_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        return await self._store.drop_interest(feed, instrument_ids, self._subscription_ttl)

    async def interest(self, feed: str) -> set[uuid.UUID]:
        return await self._store.interest(feed)

    async def feed_health(self, feed: str | None = None) -> FeedHealth | None:
        return await self._store.get_health(feed or self._source)

    # ---------------------------------------------------------- market state
    async def live_market_state(
        self,
        instrument_ids: Sequence[uuid.UUID],
        as_of: datetime | None = None,
    ) -> tuple[MarketState, list[uuid.UUID]]:
        """One snapshot of the live market, and what was not in it.

        ``as_of`` defaults to now. Quotes stamped *after* it are refused by the
        builder rather than admitted, which is what stops a state that claims to
        be a moment from containing something that had not happened yet.
        """
        moment = as_of or datetime.now(UTC)
        builder = MarketStateBuilder(moment)
        builder.add_source(self._source)

        views, missing = await self.live_quotes(instrument_ids)
        for view in views:
            builder.add_quote(view.live.quote, view.live.quality)
            price = _spot_price(view)
            if price is not None and view.instrument.asset_class in {
                AssetClass.INDEX,
                AssetClass.EQUITY,
            }:
                builder.add_spot(view.instrument.id, price)

        state = builder.build()
        # A quote stamped after the snapshot's own moment is refused by the
        # builder. It is reported here beside the ones we never had, because
        # from the caller's side both mean the same thing: this instrument is
        # not in the snapshot.
        rejected = [instrument_id for instrument_id, _reason in builder.rejected]
        return state, missing + rejected


def _spot_price(view: LiveQuoteView) -> Decimal | None:
    """The price to treat as spot: a real mid if there is one, else the print.

    The substitution is deliberate and narrow. An index publishes a level and no
    two-sided market, so requiring a mid would leave every index without a spot.
    The quote itself is untouched — ``Quote.mid_price`` still returns ``None`` —
    and the substitution happens here, in the assembly of a derived value, where
    it is visible.
    """
    quote = view.live.quote
    mid = quote.mid_price
    if mid is not None:
        return mid
    return quote.last_price
