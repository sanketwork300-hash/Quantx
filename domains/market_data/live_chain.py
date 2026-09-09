"""Capturing a live option chain as a stored snapshot.

The whole of Phase 2 rests on one decision made here: a live chain is written
into the **same** ``option_chain_snapshots`` row a CSV upload produces, and then
the existing analysis, calibration and anomaly machinery runs on it unchanged.

The alternative — computing implied volatilities and surfaces straight off the
live cache — would have been less code and much worse. A surface calibrated from
memory cannot be refitted six months later to check what it said, the arbitrage
reports and characteristics would need a second home, and there would be two
paths through the IV solver that could disagree. Persisting first means a live
analysis is reproducible on exactly the terms a historical one is, which is the
platform's central promise.

Conservation holds here as everywhere: every contract the capture considered is
kept, excluded or rejected, and a contract the feed had no price for is
*rejected with that reason* rather than quietly left out of the chain.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from domains.instruments.enums import AssetClass
from domains.instruments.models import Instrument
from domains.instruments.service import InstrumentService
from domains.market_data.models import OptionQuote
from domains.market_data.providers.normalisation import to_timestamp
from domains.market_data.quality.engine import MarketDataQualityEngine, QuoteContext
from domains.market_data.quality.flags import MarketDataQuality, Severity
from domains.market_data.repository import MarketDataRepository, PersistableOptionQuote
from domains.market_data.streaming.live_state import LiveMarketStore
from domains.reports.envelope import AnalyticalResult, AnalyticalWarning
from domains.reports.provenance import Provenance
from infrastructure.settings import Settings


class LiveChainWarningCode:
    #: The feed held no price for some of the underlying's contracts. Ordinary
    #: on an illiquid wing; the count says how ordinary.
    CONTRACTS_WITHOUT_QUOTES = "LIVE_CHAIN_CONTRACTS_WITHOUT_QUOTES"
    #: No live price for the underlying itself, so the chain has no spot.
    #: Everything downstream that needs one will say so rather than assume one.
    NO_UNDERLYING_PRICE = "LIVE_CHAIN_NO_UNDERLYING_PRICE"
    #: The underlying has no mapped option contracts at all.
    NO_CONTRACTS = "LIVE_CHAIN_NO_CONTRACTS"
    #: Quotes in the capture span a wide range of exchange timestamps, so the
    #: "snapshot" is less of an instant than it looks.
    QUOTES_NOT_SIMULTANEOUS = "LIVE_CHAIN_QUOTES_NOT_SIMULTANEOUS"


class RejectionReason:
    NO_LIVE_QUOTE = "NO_LIVE_QUOTE"


#: Above this spread of exchange timestamps within one capture, the snapshot is
#: warned about. A chain assembled from quotes minutes apart is not a snapshot
#: of one moment, and every calibration downstream assumes it is.
DEFAULT_SIMULTANEITY_TOLERANCE_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class LiveChainCaptureSummary:
    """What one capture produced."""

    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of: datetime
    contracts_considered: int
    quotes_kept: int
    quotes_excluded: int
    contracts_without_quotes: int
    underlying_price: Decimal | None
    #: Widest gap between the exchange timestamps in the capture. The honest
    #: measure of how much of an instant this "snapshot" really is.
    timestamp_spread_seconds: float
    oldest_quote_age_seconds: float

    @property
    def conserved(self) -> bool:
        return self.contracts_considered == (
            self.quotes_kept + self.quotes_excluded + self.contracts_without_quotes
        )

    def to_dict(self) -> dict:
        return {
            "snapshot_id": str(self.snapshot_id),
            "underlying_id": str(self.underlying_id),
            "as_of": self.as_of.isoformat(),
            "contracts_considered": self.contracts_considered,
            "quotes_kept": self.quotes_kept,
            "quotes_excluded": self.quotes_excluded,
            "contracts_without_quotes": self.contracts_without_quotes,
            "conserved": self.conserved,
            "underlying_price": (
                format(self.underlying_price, "f") if self.underlying_price is not None else None
            ),
            "timestamp_spread_seconds": self.timestamp_spread_seconds,
            "oldest_quote_age_seconds": self.oldest_quote_age_seconds,
        }


class LiveChainCaptureService:
    """Turns the live cache into a chain snapshot the rest of the platform reads."""

    def __init__(
        self,
        instruments: InstrumentService,
        repository: MarketDataRepository,
        store: LiveMarketStore,
        settings: Settings,
        quality: MarketDataQualityEngine | None = None,
    ) -> None:
        self._instruments = instruments
        self._repository = repository
        self._store = store
        self._settings = settings
        self._quality = quality or MarketDataQualityEngine()

    async def capture(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        expiry=None,
        provider: str | None = None,
        exclusion_threshold: Severity = Severity.ERROR,
        as_of: datetime | None = None,
    ) -> AnalyticalResult[LiveChainCaptureSummary]:
        moment = as_of or datetime.now(UTC)
        provider = provider or self._settings.market_data_provider
        warnings: list[AnalyticalWarning] = []

        underlying = await self._instruments.get(underlying_id)
        if underlying is None:
            raise LookupError(f"underlying {underlying_id} not found")

        contracts = await self._instruments.search(
            asset_class=AssetClass.OPTION,
            underlying_id=underlying_id,
            expiry=expiry,
            limit=100_000,
        )
        if not contracts:
            warnings.append(
                AnalyticalWarning.error(
                    LiveChainWarningCode.NO_CONTRACTS,
                    "No option contracts are mapped to this underlying. Load the "
                    "provider's instrument master before capturing a chain.",
                    underlying_id=str(underlying_id),
                )
            )

        underlying_price = await self._underlying_price(underlying_id)
        if underlying_price is None:
            warnings.append(
                AnalyticalWarning.warn(
                    LiveChainWarningCode.NO_UNDERLYING_PRICE,
                    "The feed holds no price for the underlying, so the snapshot carries "
                    "no spot. Nothing downstream will substitute one.",
                    underlying_id=str(underlying_id),
                )
            )

        persistable, kept_quality, without_quotes, event_times = await self._build(
            contracts, underlying, underlying_price, moment, provider, exclusion_threshold
        )

        if without_quotes:
            warnings.append(
                AnalyticalWarning.info(
                    LiveChainWarningCode.CONTRACTS_WITHOUT_QUOTES,
                    f"{len(without_quotes)} of {len(contracts)} contracts had no live price "
                    "and are recorded as rejected rather than omitted.",
                    contracts_without_quotes=len(without_quotes),
                    contracts_considered=len(contracts),
                )
            )

        spread = _timestamp_spread(event_times)
        if spread > DEFAULT_SIMULTANEITY_TOLERANCE_SECONDS:
            warnings.append(
                AnalyticalWarning.warn(
                    LiveChainWarningCode.QUOTES_NOT_SIMULTANEOUS,
                    f"The quotes in this capture span {spread:.0f} seconds. A surface "
                    "calibrated from them treats them as one instant, which they are not.",
                    timestamp_spread_seconds=spread,
                )
            )

        excluded = sum(1 for item in persistable if item.excluded)
        kept = len(persistable) - excluded
        aggregate = _aggregate(kept_quality)

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=moment,
            market_data_sources=(provider,),
            parameters={
                "capture": "live",
                "expiry": expiry.isoformat() if expiry is not None else None,
                "exclusion_severity_threshold": str(exclusion_threshold),
                "timestamp_spread_seconds": spread,
            },
        )

        snapshot = await self._repository.create_chain_snapshot(
            user_id=user_id,
            underlying_id=underlying_id,
            as_of_timestamp=moment,
            source=f"{provider}:live",
            provider=provider,
            underlying_price=underlying_price,
            rows_input=len(contracts),
            rows_kept=kept,
            rows_excluded=excluded,
            rows_rejected=len(without_quotes),
            quality_summary={
                "aggregate": aggregate.to_dict(),
                "rejection_counts": {RejectionReason.NO_LIVE_QUOTE: len(without_quotes)},
                "rejected_contracts": [str(item) for item in without_quotes[:200]],
                "timestamp_spread_seconds": spread,
            },
            provenance=provenance.to_dict(),
        )
        await self._repository.add_option_quotes(snapshot.id, persistable)

        summary = LiveChainCaptureSummary(
            snapshot_id=snapshot.id,
            underlying_id=underlying_id,
            as_of=moment,
            contracts_considered=len(contracts),
            quotes_kept=kept,
            quotes_excluded=excluded,
            contracts_without_quotes=len(without_quotes),
            underlying_price=underlying_price,
            timestamp_spread_seconds=spread,
            oldest_quote_age_seconds=_oldest_age(event_times, moment),
        )
        return AnalyticalResult.ok(summary, provenance, tuple(warnings))

    # ------------------------------------------------------------- internals
    async def _underlying_price(self, underlying_id: uuid.UUID) -> Decimal | None:
        """The underlying's live level.

        A real mid where there is a two-sided market, else the published level.
        The substitution is confined to this derived value; the stored quote
        itself is untouched and ``Quote.mid_price`` still returns ``None``.
        """
        live = await self._store.get_quote(underlying_id)
        if live is None:
            return None
        return live.quote.mid_price or live.quote.last_price

    async def _build(
        self,
        contracts: Sequence[Instrument],
        underlying: Instrument,
        underlying_price: Decimal | None,
        moment: datetime,
        provider: str,
        threshold: Severity,
    ) -> tuple[
        list[PersistableOptionQuote], list[MarketDataQuality], list[uuid.UUID], list[datetime]
    ]:
        persistable: list[PersistableOptionQuote] = []
        kept_quality: list[MarketDataQuality] = []
        without_quotes: list[uuid.UUID] = []
        event_times: list[datetime] = []

        for contract in contracts:
            live = await self._store.get_quote(contract.id)
            if live is None:
                without_quotes.append(contract.id)
                continue

            event_times.append(live.quote.exchange_timestamp)
            option_quote = OptionQuote(
                quote=live.quote,
                underlying_id=underlying.id,
                expiry=contract.expiry,
                strike=contract.strike,
                option_type=contract.option_type,
                expiry_timestamp=to_timestamp(contract.metadata.get("expiry_timestamp")),
                underlying_price=underlying_price,
            )
            # Scored here rather than reused from the feed: the feed scored this
            # quote as a standalone observation, and an option in a chain is
            # additionally checked against its own bounds and its underlying.
            quality = self._quality.score_option_quote(
                option_quote,
                QuoteContext(
                    asset_class=AssetClass.OPTION,
                    as_of=moment,
                    tick_size=contract.tick_size,
                    multiplier_assumed=contract.multiplier_is_assumed,
                ),
            )
            primary = quality.primary_flag(threshold)
            excluded = primary is not None
            if not excluded:
                kept_quality.append(quality)

            persistable.append(
                PersistableOptionQuote(
                    instrument_id=contract.id,
                    underlying_id=underlying.id,
                    source_row_number=None,
                    expiry=contract.expiry,
                    strike=contract.strike,
                    option_type=str(contract.option_type),
                    exchange_timestamp=live.quote.exchange_timestamp,
                    receive_timestamp=live.quote.receive_timestamp,
                    bid_price=live.quote.bid_price,
                    bid_size=live.quote.bid_size,
                    ask_price=live.quote.ask_price,
                    ask_size=live.quote.ask_size,
                    last_price=live.quote.last_price,
                    volume=live.quote.volume,
                    open_interest=live.quote.open_interest,
                    sequence_number=live.quote.sequence_number,
                    underlying_price=underlying_price,
                    quality=quality,
                    excluded=excluded,
                    exclusion_reason=str(primary.code) if primary else None,
                )
            )

        return persistable, kept_quality, without_quotes, event_times


def _timestamp_spread(event_times: Sequence[datetime]) -> float:
    if len(event_times) < 2:
        return 0.0
    return (max(event_times) - min(event_times)).total_seconds()


def _oldest_age(event_times: Sequence[datetime], moment: datetime) -> float:
    if not event_times:
        return 0.0
    return (moment - min(event_times)).total_seconds()


def _aggregate(qualities: Sequence[MarketDataQuality]) -> MarketDataQuality:
    if not qualities:
        return MarketDataQuality(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ())

    def mean(attribute: str) -> float:
        return sum(getattr(item, attribute) for item in qualities) / len(qualities)

    return MarketDataQuality(
        stale_score=mean("stale_score"),
        spread_score=mean("spread_score"),
        liquidity_score=mean("liquidity_score"),
        consistency_score=mean("consistency_score"),
        completeness_score=mean("completeness_score"),
        overall_score=mean("overall_score"),
        flags=(),
    )
