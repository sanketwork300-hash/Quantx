from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Query, status

from api.dependencies.core import CurrentUser, InstrumentServiceDep, SessionDep
from api.errors import NotFound, UnprocessableEntity
from api.schemas.common import PageMeta
from api.schemas.instruments import (
    AliasIn,
    AliasOut,
    InstrumentCreate,
    InstrumentListOut,
    InstrumentOut,
    ResolutionResultOut,
    ResolveRequest,
    ResolveResponse,
)
from domains.instruments.enums import AssetClass, OptionType
from domains.instruments.errors import InstrumentError
from domains.instruments.models import make_instrument
from domains.instruments.resolver import ResolutionRequest, ResolutionStatus

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("", response_model=InstrumentListOut)
async def list_instruments(
    _user: CurrentUser,
    instruments: InstrumentServiceDep,
    asset_class: AssetClass | None = None,
    exchange: str | None = None,
    symbol: str | None = None,
    underlying_id: uuid.UUID | None = None,
    expiry: date | None = None,
    option_type: OptionType | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> InstrumentListOut:
    items = await instruments.search(
        asset_class=asset_class,
        exchange=exchange,
        symbol=symbol,
        underlying_id=underlying_id,
        expiry=expiry,
        option_type=option_type,
        limit=limit,
        offset=offset,
    )
    return InstrumentListOut(
        items=[InstrumentOut.model_validate(item) for item in items],
        meta=PageMeta(limit=limit, offset=offset, count=len(items)),
    )


@router.get("/{instrument_id}", response_model=InstrumentOut)
async def get_instrument(
    instrument_id: uuid.UUID, _user: CurrentUser, instruments: InstrumentServiceDep
) -> InstrumentOut:
    instrument = await instruments.get(instrument_id)
    if instrument is None:
        raise NotFound("Instrument")
    return InstrumentOut.model_validate(instrument)


@router.post("", response_model=InstrumentOut, status_code=status.HTTP_201_CREATED)
async def create_instrument(
    payload: InstrumentCreate,
    _user: CurrentUser,
    instruments: InstrumentServiceDep,
    session: SessionDep,
) -> InstrumentOut:
    """Create or update by canonical key.

    Idempotent: ids are the uuid5 of the canonical key, so re-posting the same
    contract updates it rather than creating a second identity for it.
    """
    try:
        instrument = make_instrument(
            asset_class=payload.asset_class,
            exchange=payload.exchange,
            symbol=payload.symbol,
            currency=payload.currency,
            multiplier=Decimal(payload.multiplier),
            tick_size=Decimal(payload.tick_size),
            lot_size=Decimal(payload.lot_size),
            expiry=payload.expiry,
            strike=Decimal(payload.strike) if payload.strike is not None else None,
            option_type=payload.option_type,
            exercise_style=payload.exercise_style,
            settlement_type=payload.settlement_type,
            underlying_id=payload.underlying_id,
            venue=payload.venue,
            metadata=payload.metadata,
        )
    except InstrumentError as exc:
        raise UnprocessableEntity("INVALID_INSTRUMENT", str(exc)) from exc

    saved = await instruments.upsert(instrument)
    await session.commit()
    return InstrumentOut.model_validate(saved)


@router.post("/resolve", response_model=ResolveResponse)
async def resolve(
    payload: ResolveRequest, _user: CurrentUser, instruments: InstrumentServiceDep
) -> ResolveResponse:
    requests = [
        ResolutionRequest(
            instrument_id=item.instrument_id,
            canonical_key=item.canonical_key,
            symbol=item.symbol,
            exchange=item.exchange,
            asset_class=item.asset_class,
            expiry=item.expiry,
            strike=Decimal(item.strike) if item.strike is not None else None,
            option_type=item.option_type,
            source=item.source,
            row_index=index,
        )
        for index, item in enumerate(payload.requests)
    ]
    results = await instruments.resolve_many(requests)
    return ResolveResponse(
        results=[
            ResolutionResultOut(
                status=str(result.status),
                instrument_id=result.instrument.id if result.instrument else None,
                method=str(result.method) if result.method else None,
                confidence=result.confidence,
                reason=str(result.reason) if result.reason else None,
                candidates=(
                    [InstrumentOut.model_validate(c) for c in result.candidates]
                    if result.status is ResolutionStatus.AMBIGUOUS
                    else []
                ),
            )
            for result in results
        ]
    )


@router.get("/{instrument_id}/aliases", response_model=list[AliasOut])
async def list_aliases(
    instrument_id: uuid.UUID, _user: CurrentUser, instruments: InstrumentServiceDep
) -> list[AliasOut]:
    if await instruments.get(instrument_id) is None:
        raise NotFound("Instrument")
    aliases = await instruments.list_aliases(instrument_id)
    return [AliasOut(source=source, alias_symbol=symbol) for source, symbol in aliases]


@router.post(
    "/{instrument_id}/aliases", response_model=AliasOut, status_code=status.HTTP_201_CREATED
)
async def add_alias(
    instrument_id: uuid.UUID,
    payload: AliasIn,
    _user: CurrentUser,
    instruments: InstrumentServiceDep,
    session: SessionDep,
) -> AliasOut:
    if await instruments.get(instrument_id) is None:
        raise NotFound("Instrument")
    await instruments.add_alias(instrument_id, payload.source, payload.alias_symbol)
    await session.commit()
    return AliasOut(source=payload.source, alias_symbol=payload.alias_symbol)
