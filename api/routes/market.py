from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Query

from api.dependencies.core import (
    CurrentUser,
    DerivativesServiceDep,
    InstrumentServiceDep,
    MarketDataServiceDep,
    SettingsDep,
)
from api.errors import NotFound
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.market import (
    ChainCountsOut,
    ChainSnapshotOut,
    ChainSnapshotSummaryOut,
    MarketQuoteOut,
    OptionQuoteOut,
    QualityOut,
)
from domains.market_data.orm import OptionChainSnapshotORM, OptionQuoteORM

router = APIRouter(prefix="/market", tags=["market-data"])


def _quality(row: OptionQuoteORM) -> QualityOut:
    return QualityOut(
        stale_score=row.stale_score,
        spread_score=row.spread_score,
        liquidity_score=row.liquidity_score,
        consistency_score=row.consistency_score,
        completeness_score=row.completeness_score,
        overall_score=row.overall_score,
        flags=row.quality_flags or [],
    )


def _quote_out(row: OptionQuoteORM) -> OptionQuoteOut:
    mid = None
    relative_spread = None
    # Derived on read from the stored observations, never stored as truth.
    two_sided = (
        row.bid_price is not None
        and row.ask_price is not None
        and row.bid_price > 0
        and row.ask_price > 0
    )
    if two_sided:
        mid = (row.bid_price + row.ask_price) / 2
        if mid > 0:
            relative_spread = float((row.ask_price - row.bid_price) / mid)
    return OptionQuoteOut(
        instrument_id=row.instrument_id,
        expiry=row.expiry,
        strike=row.strike,
        option_type=row.option_type,
        exchange_timestamp=row.exchange_timestamp,
        receive_timestamp=row.receive_timestamp,
        bid_price=row.bid_price,
        bid_size=row.bid_size,
        ask_price=row.ask_price,
        ask_size=row.ask_size,
        last_price=row.last_price,
        mid_price=mid,
        relative_spread=relative_spread,
        volume=row.volume,
        open_interest=row.open_interest,
        underlying_price=row.underlying_price,
        source_row_number=row.source_row_number,
        excluded=row.excluded,
        exclusion_reason=row.exclusion_reason,
        quality=_quality(row),
    )


def _counts(snapshot: OptionChainSnapshotORM) -> ChainCountsOut:
    return ChainCountsOut(
        input=snapshot.rows_input,
        kept=snapshot.rows_kept,
        excluded=snapshot.rows_excluded,
        rejected=snapshot.rows_rejected,
    )


def _envelope(snapshot: OptionChainSnapshotORM, results) -> Envelope:
    return Envelope(
        status="OK" if snapshot.rows_kept > 0 else "PARTIAL",
        results=results,
        warnings=[],
        provenance=ProvenanceOut(**(snapshot.provenance or {})),
    )


@router.get("/chains", response_model=list[ChainSnapshotSummaryOut])
async def list_chain_snapshots(
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    underlying_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ChainSnapshotSummaryOut]:
    rows = await market_data.repository.list_chain_snapshots(
        user.id, underlying_id=underlying_id, limit=limit, offset=offset
    )
    return [
        ChainSnapshotSummaryOut(
            snapshot_id=row.id,
            underlying_id=row.underlying_id,
            as_of_timestamp=row.as_of_timestamp,
            source=row.source,
            counts=_counts(row),
            overall_score=(row.quality_summary or {}).get("aggregate", {}).get("overall_score"),
        )
        for row in rows
    ]


@router.get("/chains/{snapshot_id}", response_model=Envelope)
async def get_chain_snapshot(
    snapshot_id: uuid.UUID,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    expiry: date | None = None,
    include_excluded: bool = True,
    limit: int = Query(default=5000, ge=1, le=20000),
    offset: int = Query(default=0, ge=0),
) -> Envelope:
    snapshot = await market_data.repository.get_chain_snapshot(snapshot_id, user.id)
    if snapshot is None:
        raise NotFound("Chain snapshot")

    rows = await market_data.repository.get_option_quotes(
        snapshot.id,
        expiry=expiry,
        include_excluded=include_excluded,
        limit=limit,
        offset=offset,
    )
    results = ChainSnapshotOut(
        snapshot_id=snapshot.id,
        underlying_id=snapshot.underlying_id,
        as_of_timestamp=snapshot.as_of_timestamp,
        source=snapshot.source,
        provider=snapshot.provider,
        dataset_digest=snapshot.dataset_digest,
        underlying_price=snapshot.underlying_price,
        counts=_counts(snapshot),
        quality_summary=snapshot.quality_summary or {},
        expiries=sorted({row.expiry for row in rows}),
        quotes=[_quote_out(row) for row in rows],
    )
    return _envelope(snapshot, results)


@router.get("/options/{underlying_id}", response_model=Envelope)
async def get_latest_chain(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    instruments: InstrumentServiceDep,
    expiry: date | None = None,
    include_excluded: bool = True,
    limit: int = Query(default=5000, ge=1, le=20000),
) -> Envelope:
    if await instruments.get(underlying_id) is None:
        raise NotFound("Instrument")

    snapshot = await market_data.repository.latest_chain_snapshot(user.id, underlying_id)
    if snapshot is None:
        raise NotFound("Chain snapshot")

    rows = await market_data.repository.get_option_quotes(
        snapshot.id, expiry=expiry, include_excluded=include_excluded, limit=limit
    )
    results = ChainSnapshotOut(
        snapshot_id=snapshot.id,
        underlying_id=snapshot.underlying_id,
        as_of_timestamp=snapshot.as_of_timestamp,
        source=snapshot.source,
        provider=snapshot.provider,
        dataset_digest=snapshot.dataset_digest,
        underlying_price=snapshot.underlying_price,
        counts=_counts(snapshot),
        quality_summary=snapshot.quality_summary or {},
        expiries=sorted({row.expiry for row in rows}),
        quotes=[_quote_out(row) for row in rows],
    )
    return _envelope(snapshot, results)


@router.get("/quotes/{instrument_id}", response_model=MarketQuoteOut)
async def get_quote(
    instrument_id: uuid.UUID,
    _user: CurrentUser,
    market_data: MarketDataServiceDep,
    instruments: InstrumentServiceDep,
) -> MarketQuoteOut:
    if await instruments.get(instrument_id) is None:
        raise NotFound("Instrument")
    row = await market_data.repository.latest_market_quote(instrument_id)
    if row is None:
        raise NotFound("Quote")

    mid = None
    two_sided = (
        row.bid_price is not None
        and row.ask_price is not None
        and row.bid_price > 0
        and row.ask_price > 0
    )
    if two_sided:
        mid = (row.bid_price + row.ask_price) / 2
    return MarketQuoteOut(
        instrument_id=row.instrument_id,
        exchange_timestamp=row.exchange_timestamp,
        receive_timestamp=row.receive_timestamp,
        bid_price=row.bid_price,
        ask_price=row.ask_price,
        last_price=row.last_price,
        mid_price=mid,
        volume=row.volume,
        open_interest=row.open_interest,
        source=row.source,
    )


@router.get("/state", response_model=Envelope)
async def get_market_state(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    derivatives: DerivativesServiceDep,
    instruments: InstrumentServiceDep,
    settings: SettingsDep,
    risk_free_rate: float | None = None,
    include_quotes: bool = False,
) -> Envelope:
    """A timestamp-consistent snapshot: quotes, spot, curve and surface.

    The ``state_id`` is content-addressed, so two calculations reporting the
    same id provably saw the same inputs. Everything downstream of Phase 2 takes
    one of these rather than a live provider handle, which is what stops a risk
    report mixing a 09:15 delta with a 09:47 vega.
    """
    from domains.reports.composition import MarketStateComposer
    from domains.reports.provenance import Provenance

    if await instruments.get(underlying_id) is None:
        raise NotFound("Instrument")

    state = await MarketStateComposer(market_data, derivatives).build(
        user.id, underlying_id, risk_free_rate=risk_free_rate
    )
    if state is None:
        raise NotFound("Market state")

    warnings = []
    if not state.yield_curves:
        warnings.append(
            {
                "code": "MARKET_STATE_NO_CURVE",
                "severity": "WARNING",
                "message": (
                    "No discount curve is attached. Supply risk_free_rate to add a "
                    "flat curve, recorded as an assumption."
                ),
                "context": {},
            }
        )
    if not state.volatility_surfaces:
        warnings.append(
            {
                "code": "MARKET_STATE_NO_SURFACE",
                "severity": "INFO",
                "message": "No calibrated surface exists for this underlying yet.",
                "context": {},
            }
        )

    provenance = Provenance.now(
        code_commit=settings.code_commit,
        market_state_id=state.state_id,
        market_state_timestamp=state.as_of,
        market_data_sources=state.sources,
        dataset_versions=dict(state.data_versions),
    )
    return Envelope(
        status="OK",
        results=state.to_dict(include_quotes=include_quotes),
        warnings=warnings,
        provenance=ProvenanceOut(**provenance.to_dict()),
    )
