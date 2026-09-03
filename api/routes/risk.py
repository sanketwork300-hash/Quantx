"""Risk and scenario routes.

Every route is scoped to the portfolio's owner. VaR and stress are jobs because
both fully reprice the book — once per stored observation, or once per simulated
path — and an endpoint that pretended that was instant would be lying about it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    DerivativesServiceDep,
    JobServiceDep,
    MarketDataServiceDep,
    PortfolioServiceDep,
    RiskServiceDep,
    ScenarioServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.risk import (
    DeriveScenarioRequest,
    MarginModelOut,
    MarginRequest,
    MarginSummaryOut,
    RiskSnapshotOut,
    ScenarioCreateRequest,
    ScenarioOut,
    StressRequest,
    StressSummaryOut,
    VaRRequest,
    VaRSummaryOut,
)
from api.schemas.uploads import JobAcceptedOut
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.reports.composition import FactorHistoryComposer
from domains.risk.margin import MARGIN_MODELS
from domains.scenarios.models import ScenarioError
from domains.scenarios.service import ScenarioNotFound, ShockInput
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(tags=["risk"])


async def _owned_portfolio(portfolios, portfolio_id: uuid.UUID, user_id: uuid.UUID):
    portfolio = await portfolios.get(portfolio_id, user_id)
    if portfolio is None:
        raise NotFound("Portfolio")
    return portfolio


def _run_payload(payload: VaRRequest | StressRequest | MarginRequest) -> dict:
    return {
        "risk_free_rate": payload.risk_free_rate,
        "dividend_yield": payload.dividend_yield,
        "settlement_time_utc": (
            payload.settlement_time_utc.isoformat() if payload.settlement_time_utc else None
        ),
        "as_of": payload.as_of.isoformat() if payload.as_of else None,
        "lookback": payload.lookback,
        "include_volatility_factor": payload.include_volatility_factor,
    }


async def _submit(
    jobs, session, settings, user, job_type: JobType, payload: dict
) -> JobAcceptedOut:
    job = await jobs.create(user_id=user.id, job_type=job_type, input_reference=payload)
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(job_type),
        portfolio_id=payload.get("portfolio_id"),
    )
    await session.commit()
    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


# ------------------------------------------------------------------ scenarios
@router.get("/scenarios", response_model=list[ScenarioOut])
async def list_scenarios(
    user: CurrentUser,
    scenarios: ScenarioServiceDep,
    include_templates: bool = Query(default=True),
) -> list[ScenarioOut]:
    """Shipped templates plus this user's own.

    Templates are labelled `HYPOTHETICAL` and say so in their own descriptions:
    their numbers are round because they were chosen to be, not measured.
    """
    return [
        ScenarioOut(**scenario.to_dict())
        for scenario in await scenarios.list(user.id, include_templates=include_templates)
    ]


@router.post("/scenarios", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def create_scenario(
    payload: ScenarioCreateRequest,
    user: CurrentUser,
    scenarios: ScenarioServiceDep,
    session: SessionDep,
) -> ScenarioOut:
    """Define a scenario by hand. It is recorded as `USER_DEFINED`.

    There is deliberately no way to declare one historical here. That label is
    only earned by `POST /scenarios/derive`, which computes the shock from data.
    """
    try:
        scenario = await scenarios.create(
            user.id,
            payload.name,
            [
                ShockInput(
                    kind=str(shock.kind),
                    shock_type=str(shock.shock_type),
                    value=shock.value,
                    target=shock.target,
                )
                for shock in payload.shocks
            ],
            description=payload.description,
        )
    except ScenarioError as exc:
        raise UnprocessableEntity("INVALID_SCENARIO", str(exc)) from exc
    await session.commit()
    return ScenarioOut(**scenario.to_dict())


@router.post("/scenarios/derive", response_model=ScenarioOut, status_code=status.HTTP_201_CREATED)
async def derive_scenario(
    payload: DeriveScenarioRequest,
    user: CurrentUser,
    scenarios: ScenarioServiceDep,
    market_data: MarketDataServiceDep,
    derivatives: DerivativesServiceDep,
    session: SessionDep,
) -> ScenarioOut:
    """Derive a scenario from the underlying's own recorded history.

    The result carries the series, its date range and the date of the move it
    reproduces, so the number can be checked against the data that produced it.
    """
    composer = FactorHistoryComposer(market_data, derivatives)
    series = await composer.build(
        user.id,
        [payload.underlying_id],
        limit=payload.lookback or 500,
        include_volatility=payload.include_volatility,
    )
    spot = next((item for item in series if item.kind.value == "SPOT_RETURN"), None)
    if spot is None or len(spot.dates) < 2:
        # Count the raw history rather than the series, which is only built once
        # there are two points: reporting 0 when the user has one recorded price
        # would be a worse answer than the true one.
        recorded = await market_data.underlying_price_history(
            user.id, payload.underlying_id, limit=payload.lookback or 500
        )
        raise UnprocessableEntity(
            "INSUFFICIENT_HISTORY",
            f"This underlying has {len(recorded)} recorded observation(s), and a "
            "return needs two. History accumulates one point per ingested option "
            "chain, so ingest more of them rather than deriving a scenario from "
            "a move that was never observed.",
            observations=len(recorded),
        )
    volatility = next((item for item in series if item.kind.value == "VOLATILITY_CHANGE"), None)

    try:
        scenario = await scenarios.derive(
            user.id,
            payload.name,
            dates=list(spot.dates),
            prices=list(spot.levels),
            series_label=f"{spot.source} for underlying {payload.underlying_id}",
            window_days=payload.window_days,
            target=str(payload.underlying_id),
            percentile=payload.percentile,
            volatility_dates=list(volatility.dates) if volatility else None,
            volatility_levels=list(volatility.levels) if volatility else None,
        )
    except ScenarioError as exc:
        raise UnprocessableEntity("INVALID_SCENARIO", str(exc)) from exc
    await session.commit()
    return ScenarioOut(**scenario.to_dict())


@router.get("/scenarios/{scenario_id}", response_model=ScenarioOut)
async def get_scenario(
    scenario_id: str, user: CurrentUser, scenarios: ScenarioServiceDep
) -> ScenarioOut:
    try:
        scenario = await scenarios.resolve(user.id, scenario_id)
    except ScenarioNotFound as exc:
        raise NotFound("Scenario") from exc
    return ScenarioOut(**scenario.to_dict())


@router.delete("/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scenario(
    scenario_id: uuid.UUID,
    user: CurrentUser,
    scenarios: ScenarioServiceDep,
    session: SessionDep,
) -> None:
    try:
        await scenarios.delete(user.id, scenario_id)
    except ScenarioNotFound as exc:
        raise NotFound("Scenario") from exc
    await session.commit()


# ----------------------------------------------------------------------- risk
@router.post(
    "/portfolios/{portfolio_id}/var",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_var(
    portfolio_id: uuid.UUID,
    payload: VaRRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Value at Risk and Expected Shortfall by the requested method.

    Historical and Monte Carlo fully reprice the book under every scenario.
    Parametric does not, and its response says so.
    """
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    return await _submit(
        jobs,
        session,
        settings,
        user,
        JobType.RUN_VAR,
        {
            **_run_payload(payload),
            "portfolio_id": str(portfolio_id),
            "method": str(payload.method),
            "horizon_days": payload.horizon_days,
            "confidences": payload.confidences,
            "paths": payload.paths,
            "seed": payload.seed,
            "distribution": str(payload.distribution),
            "degrees_of_freedom": payload.degrees_of_freedom,
        },
    )


@router.post(
    "/portfolios/{portfolio_id}/stress",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_stress(
    portfolio_id: uuid.UUID,
    payload: StressRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    scenarios: ScenarioServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Apply a scenario and fully revalue.

    The response also carries the Greek approximation of the same move, labelled
    as an approximation, so the size of the linearisation error is visible
    rather than argued about.
    """
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    try:
        await scenarios.resolve(user.id, payload.scenario)
    except ScenarioNotFound as exc:
        raise NotFound("Scenario") from exc

    return await _submit(
        jobs,
        session,
        settings,
        user,
        JobType.RUN_STRESS,
        {
            **_run_payload(payload),
            "portfolio_id": str(portfolio_id),
            "scenario": payload.scenario,
            "time_decay_days": payload.time_decay_days,
        },
    )


@router.get("/portfolios/{portfolio_id}/var", response_model=list[VaRSummaryOut])
async def list_var(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    risk: RiskServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[VaRSummaryOut]:
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    rows = await risk.list_var(portfolio_id, user.id, limit=limit)
    return [VaRSummaryOut.model_validate(row) for row in rows]


@router.get("/portfolios/{portfolio_id}/stress", response_model=list[StressSummaryOut])
async def list_stress(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    risk: RiskServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[StressSummaryOut]:
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    rows = await risk.list_stress(portfolio_id, user.id, limit=limit)
    return [StressSummaryOut.model_validate(row) for row in rows]


@router.get("/portfolios/{portfolio_id}/risk-snapshot", response_model=RiskSnapshotOut)
async def latest_snapshot(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    risk: RiskServiceDep,
) -> RiskSnapshotOut:
    """The repriceable state the most recent risk run was measured on."""
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    row = await risk.repository.latest_snapshot(portfolio_id, user.id)
    if row is None:
        raise NotFound("Risk snapshot")
    return RiskSnapshotOut.model_validate(row)


# --------------------------------------------------------------------- margin
@router.get("/margin/models", response_model=list[MarginModelOut])
async def list_margin_models(_user: CurrentUser) -> list[MarginModelOut]:
    """The margin models this platform implements.

    None of them is any broker's or exchange's methodology. Those are
    proprietary, versioned and change without notice, so the platform does not
    have them and does not claim to; `is_broker_equivalent` is false for every
    entry and would only ever be true for a *published* methodology.
    """
    return [
        MarginModelOut(
            name=name,
            version=model.version,
            description=(model.__doc__ or "").strip().split("\n")[0],
            is_broker_equivalent=False,
        )
        for name, model in sorted(MARGIN_MODELS.items())
    ]


@router.post(
    "/portfolios/{portfolio_id}/margin",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_margin(
    portfolio_id: uuid.UUID,
    payload: MarginRequest,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Estimate margin, and scan where the estimated buffer goes negative.

    The output is a **region**, bounded by the move at which the buffer crosses
    zero under the named model with the stated capital. It is never a
    liquidation price: producing one would require broker rules this platform
    does not have.
    """
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    if payload.margin_model not in MARGIN_MODELS:
        raise UnprocessableEntity(
            "UNKNOWN_MARGIN_MODEL",
            f"No margin model named {payload.margin_model!r}. Available: "
            f"{', '.join(sorted(MARGIN_MODELS))}.",
            available=sorted(MARGIN_MODELS),
        )

    return await _submit(
        jobs,
        session,
        settings,
        user,
        JobType.RUN_MARGIN,
        {
            **_run_payload(payload),
            "portfolio_id": str(portfolio_id),
            "margin_model": payload.margin_model,
            "grid": (
                {
                    "spot_returns": payload.grid.spot_returns,
                    "vol_points": payload.grid.vol_points,
                }
                if payload.grid
                else None
            ),
            "short_option_minimum_rate": payload.short_option_minimum_rate,
            "concentration_add_on_rate": payload.concentration_add_on_rate,
            "concentration_threshold": payload.concentration_threshold,
            "eligible_capital": payload.eligible_capital,
            "ladder": payload.ladder,
            "vol_co_shock": payload.vol_co_shock,
        },
    )


@router.get("/portfolios/{portfolio_id}/margin", response_model=list[MarginSummaryOut])
async def list_margin(
    portfolio_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    risk: RiskServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MarginSummaryOut]:
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    rows = await risk.list_margin(portfolio_id, user.id, limit=limit)
    return [MarginSummaryOut.model_validate(row) for row in rows]


@router.get("/portfolios/{portfolio_id}/margin/{margin_id}", response_model=Envelope)
async def get_margin(
    portfolio_id: uuid.UUID,
    margin_id: uuid.UUID,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    risk: RiskServiceDep,
) -> Envelope:
    """One stored estimate with its full ladder, assumptions and provenance."""
    await _owned_portfolio(portfolios, portfolio_id, user.id)
    row = await risk.get_margin(margin_id, user.id)
    if row is None or row.portfolio_id != portfolio_id:
        raise NotFound("Margin result")

    return Envelope(
        status="PARTIAL" if row.excluded_positions else "OK",
        results={
            "margin_id": str(row.id),
            "method": row.method,
            "model_version": row.model_version,
            "currency": row.currency,
            "estimated_margin": row.estimated_margin,
            "confidence": row.confidence,
            "eligible_capital": row.eligible_capital,
            "buffer": row.buffer,
            "utilisation": row.utilisation,
            "in_shortfall_at_rest": row.in_shortfall_at_rest,
            "vol_co_shock": row.vol_co_shock,
            "worst_case": {
                "spot_return": row.worst_spot_return,
                "vol_points": row.worst_vol_points,
                "loss": row.worst_loss,
                "at_grid_edge": row.worst_at_grid_edge,
            },
            "summary": row.summary,
            "components": row.components,
            "assumptions": row.assumptions,
            "parameters": row.parameters,
            "shortfall_region": row.shortfall_region,
            "ladder": row.ladder,
            "warnings": row.warnings,
        },
        warnings=[],
        provenance=ProvenanceOut(**row.provenance),
    )
