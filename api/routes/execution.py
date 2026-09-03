"""Execution and transaction-cost routes.

Every route is scoped to the owner of the fills. Import and analysis are both
jobs: one resolves every row against the instrument master, the other assembles
a market window and six benchmarks per parent order.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    ExecutionServiceDep,
    InstrumentServiceDep,
    JobServiceDep,
    MarketDataServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import BadRequest, NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.execution import (
    AnalyseExecutionsRequest,
    ExecutionOut,
    ExecutionReportOut,
    ImpactModelOut,
    SimulateRequest,
    SimulationOut,
    StrategyOut,
    TradeImportCommitRequest,
    TradeImportPreviewOut,
    TradeImportPreviewRequest,
)
from api.schemas.uploads import JobAcceptedOut
from domains.execution.impact import IMPACT_MODELS
from domains.execution.importer import TRADE_FIELDS, TradeImportDefaults
from domains.execution.simulation import COUNTERFACTUAL_CAVEAT
from domains.execution.strategies import STRATEGIES
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.market_data.enums import UploadKind
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/execution", tags=["execution"])


async def _trades_upload(market_data, upload_id: uuid.UUID, user_id: uuid.UUID):
    upload = await market_data.get_upload(upload_id, user_id)
    if upload is None:
        raise NotFound("Upload")
    if upload.kind != str(UploadKind.TRADES):
        raise UnprocessableEntity(
            "WRONG_UPLOAD_KIND",
            f"Upload {upload_id} was received as {upload.kind}, not TRADES. "
            "Re-upload it with kind=TRADES rather than importing a file of "
            "another shape.",
        )
    return upload


def _defaults(payload) -> TradeImportDefaults:
    return TradeImportDefaults(
        currency=payload.currency.upper(),
        exchange=payload.exchange,
        asset_class=payload.asset_class,
        multiplier=payload.multiplier,
        tick_size=payload.tick_size,
        lot_size=payload.lot_size,
        exercise_style=payload.exercise_style,
        settlement_type=payload.settlement_type,
        create_missing_instruments=payload.create_missing_instruments,
        broker=payload.broker,
        parent_gap_seconds=payload.parent_gap_seconds,
    )


# ---------------------------------------------------------------------- import
@router.post("/trades/preview", response_model=TradeImportPreviewOut)
async def preview_trades(
    payload: TradeImportPreviewRequest,
    user: CurrentUser,
    execution: ExecutionServiceDep,
    market_data: MarketDataServiceDep,
    settings: SettingsDep,
) -> TradeImportPreviewOut:
    """Resolve every fill against the instrument master without writing anything.

    Preview is mandatory for the same reason it is on the portfolio import, only
    more so: a fill attributed to the wrong contract lands in the wrong parent
    order and drags a benchmark window with it, so every cost computed afterwards
    is wrong with nothing to show for it.
    """
    upload = await _trades_upload(market_data, payload.upload_id, user.id)
    data = await market_data.read_upload(upload)

    mapping = (
        ColumnMapping(mapping=dict(payload.column_mapping)) if payload.column_mapping else None
    )
    result = await execution.preview_import(
        user.id,
        data,
        mapping,
        _defaults(payload.defaults),
        limit=payload.limit or settings.upload_preview_rows,
    )
    preview = result.preview.to_dict()
    return TradeImportPreviewOut(
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


@router.post("/trades/import", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def import_trades(
    payload: TradeImportCommitRequest,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Commit a previewed trade log. Refuses while any row is ambiguous."""
    upload = await _trades_upload(market_data, payload.upload_id, user.id)
    if not payload.column_mapping:
        raise BadRequest(
            "MISSING_COLUMN_MAPPING",
            "A commit must state the column mapping it was previewed with, so "
            "that what is committed is what was reviewed.",
        )
    missing = ColumnMapping(mapping=dict(payload.column_mapping)).missing_required(TRADE_FIELDS)
    if missing:
        raise UnprocessableEntity(
            "COLUMN_MAPPING_INCOMPLETE",
            f"Required field(s) not mapped to a column: {', '.join(missing)}.",
            missing_required=list(missing),
        )

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.IMPORT_TRADES,
        input_reference={
            "upload_id": str(upload.id),
            "column_mapping": dict(payload.column_mapping),
            "defaults": payload.defaults.to_payload(),
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.IMPORT_TRADES),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


# -------------------------------------------------------------------- analysis
@router.post("/analyze", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def analyse_executions(
    payload: AnalyseExecutionsRequest,
    user: CurrentUser,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Group stored fills into parent orders and benchmark each one.

    Every benchmark reports the window it covered, where the observations came
    from and how they were combined. A benchmark the available data cannot
    support is returned unavailable with a reason rather than computed from a
    handful of ticks.
    """
    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.ANALYZE_EXECUTIONS,
        input_reference={
            "start": payload.start.isoformat() if payload.start else None,
            "end": payload.end.isoformat() if payload.end else None,
            "instrument_id": str(payload.instrument_id) if payload.instrument_id else None,
            "parent_order_key": payload.parent_order_key,
            "primary_benchmark": str(payload.primary_benchmark),
            "parent_gap_seconds": payload.parent_gap_seconds,
            "staleness_tolerance_seconds": payload.staleness_tolerance_seconds,
            "window_padding_seconds": payload.window_padding_seconds,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.ANALYZE_EXECUTIONS),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


# ----------------------------------------------------------------------- reads
@router.get("/executions", response_model=list[ExecutionOut])
async def list_executions(
    user: CurrentUser,
    execution: ExecutionServiceDep,
    instrument_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    parent_order_key: str | None = None,
    limit: int = Query(default=200, ge=1, le=5_000),
) -> list[ExecutionOut]:
    rows = await execution.list_executions(
        user.id,
        instrument_id=instrument_id,
        start=start,
        end=end,
        parent_order_key=parent_order_key,
        limit=limit,
    )
    return [ExecutionOut.model_validate(row) for row in rows]


@router.get("/reports", response_model=list[ExecutionReportOut])
async def list_reports(
    user: CurrentUser,
    execution: ExecutionServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExecutionReportOut]:
    rows = await execution.list_reports(user.id, limit=limit, offset=offset)
    return [ExecutionReportOut.model_validate(row) for row in rows]


@router.get("/reports/{report_id}", response_model=Envelope)
async def get_report(
    report_id: uuid.UUID, user: CurrentUser, execution: ExecutionServiceDep
) -> Envelope:
    """One parent order's analysis: every benchmark with its window, source and
    method, the shortfalls that could be computed, the ones that could not and
    why, and the model-based decomposition."""
    row = await execution.get_report(report_id, user.id)
    if row is None:
        raise NotFound("Execution report")

    return Envelope(
        status="OK" if row.shortfall_currency is not None else "PARTIAL",
        results={
            "report_id": str(row.id),
            "parent_order_key": row.parent_order_key,
            "grouping_method": row.grouping_method,
            "grouping_is_inferred": row.grouping_is_inferred,
            "instrument_id": str(row.instrument_id),
            "canonical_key": row.canonical_key,
            "side": row.side,
            "currency": row.currency,
            "multiplier": format(row.multiplier, "f"),
            "fills": row.fills,
            "filled_quantity": format(row.filled_quantity, "f"),
            "order_quantity": (
                format(row.order_quantity, "f") if row.order_quantity is not None else None
            ),
            "average_price": format(row.average_price, "f"),
            "fees": format(row.fees, "f"),
            "window": {
                "start": row.window_start.isoformat(),
                "end": row.window_end.isoformat(),
            },
            "primary_benchmark": row.primary_benchmark,
            "shortfall": (
                {
                    "benchmark_price": format(row.primary_benchmark_price, "f"),
                    "currency_amount": row.shortfall_currency,
                    "basis_points": row.shortfall_bps,
                    "percent": row.shortfall_percent,
                }
                if row.shortfall_currency is not None
                else None
            ),
            "benchmarks": row.benchmarks,
            "shortfalls": row.shortfalls,
            "unavailable_shortfalls": row.unavailable_shortfalls,
            "decomposition": row.decomposition,
            "market_window": row.market_window,
            "warnings": row.warnings,
        },
        warnings=[],
        provenance=ProvenanceOut(**row.provenance),
    )


# ------------------------------------------------------------------ simulation
#: What each strategy needs from the caller. The platform supplies none of it.
STRATEGY_REQUIREMENTS = {
    "TWAP": [],
    "VWAP": ["expected_volumes (must vary)"],
    "POV": ["expected_volumes", "participation_rate", "enough capacity in the window"],
    "LiquidityAdaptive": ["expected_volumes", "spreads or volatilities (must vary)"],
}


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(_user: CurrentUser) -> list[StrategyOut]:
    """The execution strategies this platform can schedule, and what each needs.

    A strategy whose inputs are missing reports itself unavailable with a reason
    rather than degrading into another strategy under its own name.
    """
    return [
        StrategyOut(
            name=name,
            version=strategy.version,
            description=(strategy.__doc__ or "").strip().split("\n")[0],
            requires=STRATEGY_REQUIREMENTS.get(name, []),
        )
        for name, strategy in sorted(STRATEGIES.items())
    ]


@router.get("/impact-models", response_model=list[ImpactModelOut])
async def list_impact_models(_user: CurrentUser) -> list[ImpactModelOut]:
    """The impact models, none of which ships a calibrated coefficient.

    The functional forms are from the literature; the coefficients are
    regime-, venue- and period-dependent and were not measured here. They
    default to the identity, and every result computed that way is flagged.
    """
    return [
        ImpactModelOut(
            name=name,
            version=model.version,
            description=(model.__doc__ or "").strip().split("\n")[0],
            ships_calibrated_coefficients=False,
        )
        for name, model in sorted(IMPACT_MODELS.items())
    ]


@router.post("/simulate", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def simulate_execution(
    payload: SimulateRequest,
    user: CurrentUser,
    instruments: InstrumentServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Price one or more schedules against a past path.

    **Every number in the result is a counterfactual estimate.** These schedules
    were never executed; they are priced against a path the market printed while
    something else was happening, and executing one would itself have moved that
    path. No strategy is recommended and the result contains no ranking.
    """
    unknown = [name for name in payload.strategies if name not in STRATEGIES]
    if unknown:
        raise UnprocessableEntity(
            "UNKNOWN_STRATEGY",
            f"No execution strategy named {', '.join(unknown)}. Available: "
            f"{', '.join(sorted(STRATEGIES))}.",
            available=sorted(STRATEGIES),
        )
    if payload.impact_model not in IMPACT_MODELS:
        raise UnprocessableEntity(
            "UNKNOWN_IMPACT_MODEL",
            f"No impact model named {payload.impact_model!r}. Available: "
            f"{', '.join(sorted(IMPACT_MODELS))}.",
            available=sorted(IMPACT_MODELS),
        )
    # Checked here rather than left to the job, so an unknown contract is a
    # stated refusal at the boundary instead of a failed job with a traceback.
    if await instruments.get(payload.instrument_id) is None:
        raise NotFound("Instrument")

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.SIMULATE_EXECUTION,
        input_reference={
            "instrument_id": str(payload.instrument_id),
            "side": str(payload.side),
            "quantity": format(payload.quantity, "f"),
            "start": payload.start.isoformat(),
            "end": payload.end.isoformat(),
            "intervals": payload.intervals,
            "strategies": payload.strategies,
            "impact_model": payload.impact_model,
            "permanent_coefficient": payload.permanent_coefficient,
            "temporary_coefficient": payload.temporary_coefficient,
            "volatility": payload.volatility,
            "average_daily_volume": payload.average_daily_volume,
            "lot_size": format(payload.lot_size, "f"),
            "expected_volumes": payload.expected_volumes,
            "spreads": payload.spreads,
            "volatilities": payload.volatilities,
            "participation_rate": payload.participation_rate,
            "latency_seconds": payload.latency_seconds,
            "max_price_age_seconds": payload.max_price_age_seconds,
            "window_padding_seconds": payload.window_padding_seconds,
            "staleness_tolerance_seconds": payload.staleness_tolerance_seconds,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.SIMULATE_EXECUTION),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/simulations", response_model=list[SimulationOut])
async def list_simulations(
    user: CurrentUser,
    execution: ExecutionServiceDep,
    comparison_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[SimulationOut]:
    rows = await execution.list_simulations(
        user.id, comparison_id=comparison_id, limit=limit, offset=offset
    )
    return [SimulationOut.model_validate(row) for row in rows]


@router.get("/simulations/{simulation_id}", response_model=Envelope)
async def get_simulation(
    simulation_id: uuid.UUID, user: CurrentUser, execution: ExecutionServiceDep
) -> Envelope:
    """One stored counterfactual run, with its schedule, fills and benchmarks."""
    row = await execution.get_simulation(simulation_id, user.id)
    if row is None:
        raise NotFound("Execution simulation")

    return Envelope(
        status="OK" if row.completion_rate >= 1.0 else "PARTIAL",
        results={
            "simulation_id": str(row.id),
            "comparison_id": str(row.comparison_id),
            "counterfactual": row.counterfactual,
            "caveat": COUNTERFACTUAL_CAVEAT,
            "strategy": row.strategy,
            "impact_model": row.impact_model,
            "impact_is_calibrated": row.impact_is_calibrated,
            "side": row.side,
            "ordered_quantity": format(row.ordered_quantity, "f"),
            "filled_quantity": format(row.filled_quantity, "f"),
            "completion_rate": row.completion_rate,
            "average_price": (
                format(row.average_price, "f") if row.average_price is not None else None
            ),
            "window": {
                "start": row.window_start.isoformat(),
                "end": row.window_end.isoformat(),
            },
            "latency_seconds": row.latency_seconds,
            "max_price_age_seconds": row.max_price_age_seconds,
            "modelled_impact_cost": format(row.modelled_impact_cost, "f"),
            "modelled_spread_cost": format(row.modelled_spread_cost, "f"),
            "shortfall": (
                {
                    "benchmark": row.primary_benchmark,
                    "currency_amount": row.shortfall_currency,
                    "basis_points": row.shortfall_bps,
                }
                if row.shortfall_currency is not None
                else None
            ),
            "schedule": row.schedule,
            "context": row.context,
            "fills": row.fills,
            "unfilled": row.unfilled,
            "benchmarks": row.benchmarks,
            "warnings": row.warnings,
        },
        warnings=[],
        provenance=ProvenanceOut(**row.provenance),
    )
