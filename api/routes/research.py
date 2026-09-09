"""Research: strategies, backtests and the experiment record.

Note what this surface does **not** offer: an endpoint that evaluates a strategy
against today's market and returns its view. That would be a trading signal, and
the platform does not emit those. Everything here is historical simulation, and a
strategy's output is a *target weight for a simulated book* rather than an
instruction to anybody.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    InstrumentServiceDep,
    JobServiceDep,
    ResearchServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.research import (
    EquityCurveOut,
    ExperimentDetailOut,
    ExperimentSummaryOut,
    FillListOut,
    RunBacktestRequest,
    StrategyOut,
)
from api.schemas.uploads import JobAcceptedOut
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.research import strategies as strategy_module
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/research", tags=["research"])


def _summary(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "instrument_id": row.instrument_id,
        "dataset_id": row.dataset_id,
        "strategy_name": row.strategy_name,
        "strategy_version": row.strategy_version,
        "start_timestamp": row.start_timestamp,
        "end_timestamp": row.end_timestamp,
        "initial_equity": format(row.initial_equity, "f"),
        "final_equity": format(row.final_equity, "f"),
        "total_costs": format(row.total_costs, "f"),
        "total_slippage": format(row.total_slippage, "f"),
        "gross_of_costs": row.gross_of_costs,
        "total_return": row.total_return,
        "cagr": row.cagr,
        "sharpe": row.sharpe,
        "sortino": row.sortino,
        "max_drawdown": row.max_drawdown,
        "bars_in": row.bars_in,
        "bars_used": row.bars_used,
        "fill_count": row.fill_count,
        "created_at": row.created_at,
    }


@router.get("/strategies", response_model=list[StrategyOut])
async def list_strategies(_user: CurrentUser) -> list[StrategyOut]:
    """The strategies the platform ships, and the features each declares.

    They are benchmarks rather than recommendations: buy-and-hold exists so a
    backtest's accounting can be checked against an instrument's own return, and
    a run whose buy-and-hold does not match has a bug no Sharpe ratio would find.
    """
    items: list[StrategyOut] = []
    for name, strategy_type in sorted(strategy_module.REGISTRY.items()):
        instance = strategy_type()
        items.append(
            StrategyOut(
                name=name,
                version=instance.version,
                features=[spec.name for spec in instance.features()],
                parameters=instance.parameters(),
            )
        )
    return items


@router.post("/backtests", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(
    payload: RunBacktestRequest,
    user: CurrentUser,
    jobs: JobServiceDep,
    instruments: InstrumentServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Run a strategy over warehouse bars and record the experiment.

    Supply ``cost_components`` if you want a net return. Without them the run is
    **gross** and says so on every figure: the platform does not hold brokerage,
    exchange or statutory rates and will not invent them.
    """
    if payload.strategy_name not in strategy_module.REGISTRY:
        raise UnprocessableEntity(
            "UNKNOWN_STRATEGY",
            f"{payload.strategy_name!r} is not a known strategy.",
            available=sorted(strategy_module.REGISTRY),
        )
    if await instruments.get(payload.instrument_id) is None:
        raise NotFound("Instrument")
    for moment, name in ((payload.start, "start"), (payload.end, "end")):
        if moment is not None and moment.tzinfo is None:
            raise UnprocessableEntity(
                "TIMESTAMP_NOT_TIMEZONE_AWARE",
                f"{name} must carry a UTC offset; a naive timestamp does not name a moment.",
            )

    try:
        strategy_module.build(payload.strategy_name, payload.strategy_parameters)
    except (ValueError, TypeError) as exc:
        raise UnprocessableEntity("INVALID_STRATEGY_PARAMETERS", str(exc)) from exc

    job = await jobs.create(
        user.id,
        JobType.RUN_BACKTEST,
        {
            "name": payload.name,
            "instrument_id": str(payload.instrument_id),
            "strategy_name": payload.strategy_name,
            "strategy_parameters": payload.strategy_parameters,
            "start": payload.start.isoformat() if payload.start else None,
            "end": payload.end.isoformat() if payload.end else None,
            "exchange": payload.exchange,
            "dataset_id": str(payload.dataset_id) if payload.dataset_id else None,
            "initial_cash": payload.initial_cash,
            "timing": str(payload.timing),
            "cost_schedule_name": payload.cost_schedule_name,
            "cost_schedule_source": payload.cost_schedule_source,
            "cost_components": [item.model_dump() for item in payload.cost_components],
            "slippage_basis_points": payload.slippage_basis_points,
            "slippage_source": payload.slippage_source,
            "max_position_weight": payload.max_position_weight,
            "max_gross_exposure": payload.max_gross_exposure,
            "include_flagged_bars": payload.include_flagged_bars,
            "risk_free_rate": payload.risk_free_rate,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.RUN_BACKTEST),
    )
    await session.commit()
    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/experiments", response_model=list[ExperimentSummaryOut])
async def list_experiments(
    user: CurrentUser,
    research: ResearchServiceDep,
    strategy_name: str | None = None,
    instrument_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExperimentSummaryOut]:
    rows = await research.list_experiments(
        user.id,
        strategy_name=strategy_name,
        instrument_id=instrument_id,
        limit=limit,
        offset=offset,
    )
    return [ExperimentSummaryOut.model_validate(_summary(row)) for row in rows]


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetailOut)
async def get_experiment(
    experiment_id: uuid.UUID, user: CurrentUser, research: ResearchServiceDep
) -> ExperimentDetailOut:
    """A run, with everything needed to run it again.

    The cost schedule and slippage assumption are here verbatim, because a
    return figure means something different depending on them and a record that
    omitted them would be a number without a claim attached.
    """
    row = await research.get_experiment(experiment_id, user.id)
    if row is None:
        raise NotFound("Experiment")
    return ExperimentDetailOut.model_validate(
        {
            **_summary(row),
            "strategy_parameters": row.strategy_parameters or {},
            "features": row.features or [],
            "engine_config": row.engine_config or {},
            "cost_schedule": row.cost_schedule or {},
            "slippage_model": row.slippage_model or {},
            "code_commit": row.code_commit,
            "data_digest": row.data_digest,
            "traded_notional": format(row.traded_notional, "f"),
            "metrics": row.metrics or {},
            "attribution": row.attribution or {},
            "warnings": row.warnings or [],
            "provenance": row.provenance or {},
        }
    )


@router.get("/experiments/{experiment_id}/equity-curve", response_model=EquityCurveOut)
async def equity_curve(
    experiment_id: uuid.UUID, user: CurrentUser, research: ResearchServiceDep
) -> EquityCurveOut:
    row = await research.get_experiment(experiment_id, user.id)
    if row is None:
        raise NotFound("Experiment")
    points = await research.equity_curve(row)
    return EquityCurveOut(items=points, count=len(points))


@router.get("/experiments/{experiment_id}/fills", response_model=FillListOut)
async def experiment_fills(
    experiment_id: uuid.UUID, user: CurrentUser, research: ResearchServiceDep
) -> FillListOut:
    """Every simulated trade, with the reason the strategy gave for it."""
    row = await research.get_experiment(experiment_id, user.id)
    if row is None:
        raise NotFound("Experiment")
    fills = await research.fills(row)
    return FillListOut(items=fills, count=len(fills))
