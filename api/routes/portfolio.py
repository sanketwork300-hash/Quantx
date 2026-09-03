"""Portfolio routes.

Every route resolves the portfolio through an ownership-scoped read. A
``portfolio_id`` is never trusted because it is a well-formed UUID: a foreign id
returns 404, which is also the correct answer to "does this exist?".
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    JobServiceDep,
    MarketDataServiceDep,
    PortfolioApplicationDep,
    PortfolioServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import BadRequest, NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.portfolio import (
    ImportCommitRequest,
    ImportPreviewOut,
    ImportPreviewRequest,
    PortfolioCreateRequest,
    PortfolioOut,
    PortfolioUpdateRequest,
    PortfolioValuationOut,
    PositionCreateRequest,
    PositionOut,
    PositionUpdateRequest,
    ValuePortfolioRequest,
)
from api.schemas.uploads import JobAcceptedOut
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.market_data.enums import UploadKind
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.portfolio.enums import PositionSide
from domains.portfolio.importer import POSITION_FIELDS, ImportDefaults
from domains.portfolio.models import PortfolioError
from domains.portfolio.service import PortfolioNotFound
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/portfolios", tags=["portfolio"])


async def _owned(portfolios, portfolio_id: uuid.UUID, user_id: uuid.UUID):
    portfolio = await portfolios.get(portfolio_id, user_id)
    if portfolio is None:
        raise NotFound("Portfolio")
    return portfolio


async def _positions_upload(market_data, upload_id: uuid.UUID, user_id: uuid.UUID):
    upload = await market_data.get_upload(upload_id, user_id)
    if upload is None:
        raise NotFound("Upload")
    if upload.kind != str(UploadKind.POSITIONS):
        raise UnprocessableEntity(
            "WRONG_UPLOAD_KIND",
            f"Upload {upload_id} was received as {upload.kind}, not POSITIONS. "
            "Re-upload it with kind=POSITIONS rather than importing a file of "
            "another shape.",
        )
    return upload


# ------------------------------------------------------------------ portfolios
@router.post("", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    payload: PortfolioCreateRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    session: SessionDep,
) -> PortfolioOut:
    try:
        portfolio = await portfolios.create(
            user_id=user.id,
            name=payload.name,
            base_currency=payload.base_currency,
            description=payload.description,
        )
    except PortfolioError as exc:
        raise UnprocessableEntity("INVALID_PORTFOLIO", str(exc)) from exc
    await session.commit()
    return PortfolioOut.model_validate(portfolio)


@router.get("", response_model=list[PortfolioOut])
async def list_portfolios(
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PortfolioOut]:
    rows = await portfolios.list(user.id, limit=limit, offset=offset)
    return [PortfolioOut.model_validate(row) for row in rows]


@router.get("/{portfolio_id}", response_model=PortfolioOut)
async def get_portfolio(
    portfolio_id: uuid.UUID, user: CurrentUser, portfolios: PortfolioServiceDep
) -> PortfolioOut:
    portfolio = await _owned(portfolios, portfolio_id, user.id)
    return PortfolioOut.model_validate(portfolio)


@router.patch("/{portfolio_id}", response_model=PortfolioOut)
async def update_portfolio(
    portfolio_id: uuid.UUID,
    payload: PortfolioUpdateRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    session: SessionDep,
) -> PortfolioOut:
    try:
        portfolio = await portfolios.update(
            portfolio_id, user.id, name=payload.name, description=payload.description
        )
    except PortfolioNotFound as exc:
        raise NotFound("Portfolio") from exc
    except PortfolioError as exc:
        raise UnprocessableEntity("INVALID_PORTFOLIO", str(exc)) from exc
    await session.commit()
    return PortfolioOut.model_validate(portfolio)


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    session: SessionDep,
) -> None:
    try:
        await portfolios.delete(portfolio_id, user.id)
    except PortfolioNotFound as exc:
        raise NotFound("Portfolio") from exc
    await session.commit()


# ------------------------------------------------------------------- positions
@router.get("/{portfolio_id}/positions", response_model=list[PositionOut])
async def list_positions(
    portfolio_id: uuid.UUID, user: CurrentUser, portfolios: PortfolioServiceDep
) -> list[PositionOut]:
    await _owned(portfolios, portfolio_id, user.id)
    rows = await portfolios.positions(portfolio_id)
    return [PositionOut.model_validate(row) for row in rows]


@router.post(
    "/{portfolio_id}/positions",
    response_model=PositionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_position(
    portfolio_id: uuid.UUID,
    payload: PositionCreateRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    session: SessionDep,
) -> PositionOut:
    """Add one position.

    ``side`` is checked against the sign of ``quantity`` rather than used to
    infer it; a disagreement is a validation error, because guessing which of
    the two the user meant is how a portfolio ends up short what it is long.
    """
    side = None
    if payload.side is not None:
        try:
            side = PositionSide.parse(payload.side)
        except ValueError as exc:
            raise UnprocessableEntity("INVALID_SIDE", str(exc)) from exc
    try:
        position = await portfolios.add_position(
            portfolio_id=portfolio_id,
            user_id=user.id,
            instrument_id=payload.instrument_id,
            quantity=payload.quantity,
            average_price=payload.average_price,
            side=side,
            strategy_tag=payload.strategy_tag,
        )
    except PortfolioNotFound as exc:
        raise NotFound("Portfolio") from exc
    except PortfolioError as exc:
        raise UnprocessableEntity("INVALID_POSITION", str(exc)) from exc
    await session.commit()
    return PositionOut.model_validate(position)


@router.patch("/{portfolio_id}/positions/{position_id}", response_model=PositionOut)
async def update_position(
    portfolio_id: uuid.UUID,
    position_id: uuid.UUID,
    payload: PositionUpdateRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    session: SessionDep,
) -> PositionOut:
    try:
        position = await portfolios.update_position(
            portfolio_id,
            user.id,
            position_id,
            quantity=payload.quantity,
            average_price=payload.average_price,
            strategy_tag=payload.strategy_tag,
        )
    except PortfolioNotFound as exc:
        raise NotFound("Position") from exc
    except PortfolioError as exc:
        raise UnprocessableEntity("INVALID_POSITION", str(exc)) from exc
    await session.commit()
    return PositionOut.model_validate(position)


@router.delete("/{portfolio_id}/positions/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_position(
    portfolio_id: uuid.UUID,
    position_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    session: SessionDep,
) -> None:
    try:
        await portfolios.delete_position(portfolio_id, user.id, position_id)
    except PortfolioNotFound as exc:
        raise NotFound("Position") from exc
    await session.commit()


# ---------------------------------------------------------------------- import
@router.post("/{portfolio_id}/import/preview", response_model=ImportPreviewOut)
async def preview_import(
    portfolio_id: uuid.UUID,
    payload: ImportPreviewRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    application: PortfolioApplicationDep,
    market_data: MarketDataServiceDep,
    settings: SettingsDep,
) -> ImportPreviewOut:
    """Resolve every row against the instrument master without writing anything.

    Preview is mandatory: it is the only place an ambiguous row can be shown to
    the person who knows which contract they meant.
    """
    await _owned(portfolios, portfolio_id, user.id)
    upload = await _positions_upload(market_data, payload.upload_id, user.id)
    data = await market_data.read_upload(upload)

    mapping = (
        ColumnMapping(mapping=dict(payload.column_mapping)) if payload.column_mapping else None
    )
    result = await application.preview_import(
        data,
        mapping,
        _defaults(payload.defaults),
        limit=payload.limit or settings.upload_preview_rows,
    )
    preview = result.preview.to_dict()
    return ImportPreviewOut(
        upload_id=upload.id,
        headers=result.headers,
        inferred_mapping=result.inferred_mapping,
        applied_mapping=result.applied_mapping,
        rows_in=preview["counts"]["input"],
        committable=preview["committable"],
        resolved=preview["resolved"],
        ambiguous=preview["ambiguous"],
        invalid=preview["invalid"],
    )


@router.post(
    "/{portfolio_id}/import", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED
)
async def commit_import(
    portfolio_id: uuid.UUID,
    payload: ImportCommitRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    market_data: MarketDataServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Commit a previewed import. Refuses while any row is still ambiguous."""
    await _owned(portfolios, portfolio_id, user.id)
    upload = await _positions_upload(market_data, payload.upload_id, user.id)
    if not payload.column_mapping:
        raise BadRequest(
            "MISSING_COLUMN_MAPPING",
            "A commit must state the column mapping it was previewed with, so "
            "that what is committed is what was reviewed.",
        )
    missing = ColumnMapping(mapping=dict(payload.column_mapping)).missing_required(POSITION_FIELDS)
    if missing:
        raise UnprocessableEntity(
            "COLUMN_MAPPING_INCOMPLETE",
            f"Required field(s) not mapped to a column: {', '.join(missing)}.",
            missing_required=list(missing),
        )

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.IMPORT_POSITIONS,
        input_reference={
            "portfolio_id": str(portfolio_id),
            "upload_id": str(upload.id),
            "column_mapping": dict(payload.column_mapping),
            "defaults": payload.defaults.to_payload(),
            "replace_existing": payload.replace_existing,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.IMPORT_POSITIONS),
        portfolio_id=str(portfolio_id),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


# ------------------------------------------------------------------- valuation
@router.post(
    "/{portfolio_id}/valuation",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def value_portfolio(
    portfolio_id: uuid.UUID,
    payload: ValuePortfolioRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Value every position against one market snapshot.

    A job rather than a synchronous call: each position resolves an instrument,
    reads a quote and may price against a fitted surface.
    """
    await _owned(portfolios, portfolio_id, user.id)
    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.VALUE_PORTFOLIO,
        input_reference={
            "portfolio_id": str(portfolio_id),
            "risk_free_rate": payload.risk_free_rate,
            "dividend_yield": payload.dividend_yield,
            "settlement_time_utc": (
                payload.settlement_time_utc.isoformat() if payload.settlement_time_utc else None
            ),
            "as_of": payload.as_of.isoformat() if payload.as_of else None,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.VALUE_PORTFOLIO),
        portfolio_id=str(portfolio_id),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/{portfolio_id}/valuation", response_model=PortfolioValuationOut)
async def latest_valuation(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    application: PortfolioApplicationDep,
) -> PortfolioValuationOut:
    await _owned(portfolios, portfolio_id, user.id)
    row = await application.latest_valuation(portfolio_id, user.id)
    if row is None:
        raise NotFound("Portfolio valuation")
    return _valuation_out(row)


@router.get("/{portfolio_id}/valuation/{valuation_id}", response_model=Envelope)
async def valuation_detail(
    portfolio_id: uuid.UUID,
    valuation_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    application: PortfolioApplicationDep,
) -> Envelope:
    """One stored valuation with its positions and the provenance it was
    computed under, so the run can be reproduced rather than trusted."""
    await _owned(portfolios, portfolio_id, user.id)
    row = await application.get_valuation(valuation_id, user.id)
    if row is None or row.portfolio_id != portfolio_id:
        raise NotFound("Portfolio valuation")

    positions = await application.repository.get_position_valuations(valuation_id)
    payload = _valuation_out(row).model_dump(mode="json")
    payload["positions_detail"] = [_position_out(p) for p in positions]
    return Envelope(
        status="OK" if row.valued == row.positions else "PARTIAL",
        results=payload,
        warnings=[],
        provenance=ProvenanceOut(**row.provenance),
    )


@router.get("/{portfolio_id}/greeks", response_model=PortfolioValuationOut)
async def portfolio_greeks(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    application: PortfolioApplicationDep,
    dimension: str | None = Query(
        default=None,
        description="Restrict aggregates to one of UNDERLYING, EXPIRY, ASSET_CLASS, "
        "STRATEGY_TAG, CURRENCY.",
    ),
) -> PortfolioValuationOut:
    """Aggregated Greeks from the latest stored valuation.

    Read from the stored valuation rather than recomputed, so the Greeks on this
    response and the values on the valuation response came from one snapshot.
    """
    await _owned(portfolios, portfolio_id, user.id)
    row = await application.latest_valuation(portfolio_id, user.id)
    if row is None:
        raise NotFound("Portfolio valuation")

    out = _valuation_out(row)
    if dimension is not None:
        wanted = dimension.upper()
        out.aggregates = [b for b in out.aggregates if b.dimension == wanted]
    return out


# ------------------------------------------------------------------- helpers
def _defaults(payload) -> ImportDefaults:
    return ImportDefaults(
        currency=payload.currency.upper(),
        exchange=payload.exchange,
        asset_class=payload.asset_class,
        multiplier=payload.multiplier,
        tick_size=payload.tick_size,
        lot_size=payload.lot_size,
        exercise_style=payload.exercise_style,
        settlement_type=payload.settlement_type,
        create_missing_instruments=payload.create_missing_instruments,
    )


def _greeks(row) -> dict:
    from domains.portfolio.models import GREEK_UNITS

    return {
        "delta": row.delta,
        "gamma": row.gamma,
        "vega_per_vol_point": row.vega_per_vol_point,
        "theta_per_day": row.theta_per_day,
        "rho_per_bp": row.rho_per_bp,
        "units": GREEK_UNITS,
    }


def _valuation_out(row) -> PortfolioValuationOut:
    return PortfolioValuationOut(
        valuation_id=row.id,
        portfolio_id=row.portfolio_id,
        as_of_timestamp=row.as_of_timestamp,
        base_currency=row.base_currency,
        market_state_id=row.market_state_id,
        positions=row.positions,
        valued=row.valued,
        base_market_value=row.base_market_value,
        unrealized_pnl=row.unrealized_pnl,
        gross_exposure=row.gross_exposure,
        net_exposure=row.net_exposure,
        greeks=_greeks(row),
        valuation_methods=row.valuation_methods or {},
        aggregates=row.aggregates or [],
        created_at=row.created_at,
    )


def _position_out(row) -> dict:
    return {
        "position_id": str(row.position_id),
        "instrument_id": str(row.instrument_id),
        "canonical_key": row.canonical_key,
        "asset_class": row.asset_class,
        "underlying_id": str(row.underlying_id) if row.underlying_id else None,
        "expiry": row.expiry.isoformat() if row.expiry else None,
        "strike": format(row.strike, "f") if row.strike is not None else None,
        "option_type": row.option_type,
        "quantity": format(row.quantity, "f"),
        "multiplier": format(row.multiplier, "f"),
        "currency": row.currency,
        "market_price": format(row.market_price, "f") if row.market_price is not None else None,
        "model_price": format(row.model_price, "f") if row.model_price is not None else None,
        "price_used": format(row.price_used, "f") if row.price_used is not None else None,
        "valuation_method": row.valuation_method,
        "market_value": format(row.market_value, "f") if row.market_value is not None else None,
        "base_market_value": (
            format(row.base_market_value, "f") if row.base_market_value is not None else None
        ),
        "fx_rate": format(row.fx_rate, "f") if row.fx_rate is not None else None,
        "unrealized_pnl": (
            format(row.unrealized_pnl, "f") if row.unrealized_pnl is not None else None
        ),
        "greeks": _greeks(row),
        "greek_source": row.greek_source,
        "implied_volatility": row.implied_volatility,
        "time_to_expiry": row.time_to_expiry,
        "quote_age_seconds": row.quote_age_seconds,
        "warnings": list(row.warnings or []),
    }
