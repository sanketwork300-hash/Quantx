"""Unified order analysis.

Answered inline rather than as a job. It values one portfolio once and one extra
contract once; the expensive parts — a surface calibration, an intensity fit —
were done in earlier phases and are read here, not redone.

The route is thin on purpose. It checks ownership, maps a request model onto the
domain's own request object, and returns the envelope. There is no assembly of
the five branches here: `domains.reports` is the only place permitted to fan out
across the engines, and doing it in a controller would put cross-engine policy
in the HTTP layer where no test looks for it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from api.dependencies.core import (
    CurrentUser,
    OrderAnalysisServiceDep,
    PortfolioServiceDep,
    SessionDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut, WarningOut
from api.schemas.orders import (
    OrderAnalysisOut,
    OrderAnalysisRequestIn,
    OrderAnalysisSummaryOut,
)
from domains.derivatives.anomaly import AnomalyPolicy
from domains.execution.impact import IMPACT_MODELS
from domains.portfolio.service import PortfolioNotFound
from domains.reports.order_analysis import (
    ExecutionAssumptions,
    OrderAnalysisError,
    OrderAnalysisRequest,
)
from domains.risk.application import MarginRunParams
from domains.risk.margin import MARGIN_MODELS
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(tags=["order-analysis"])


def _to_domain(payload: OrderAnalysisRequestIn) -> OrderAnalysisRequest:
    grid = payload.margin.grid or None
    return OrderAnalysisRequest(
        portfolio_id=payload.portfolio_id,
        instrument_id=payload.instrument_id,
        side=payload.side,
        quantity=payload.quantity,
        order_type=payload.order_type,
        limit_price=payload.limit_price,
        risk_free_rate=payload.risk_free_rate,
        dividend_yield=payload.dividend_yield,
        settlement_time_utc=payload.settlement_time_utc,
        as_of=payload.as_of,
        var_method=payload.var_method,
        scenario=payload.scenario,
        lookback=payload.lookback,
        horizon_days=payload.horizon_days,
        seed=payload.seed,
        execution=ExecutionAssumptions(
            horizon_seconds=payload.execution.horizon_seconds,
            intervals=payload.execution.intervals,
            strategies=tuple(payload.execution.strategies),
            impact_model=payload.execution.impact_model,
            permanent_coefficient=payload.execution.permanent_coefficient,
            temporary_coefficient=payload.execution.temporary_coefficient,
            volatility=payload.execution.volatility,
            average_daily_volume=payload.execution.average_daily_volume,
            participation_rate=payload.execution.participation_rate,
            expected_volumes=(
                tuple(payload.execution.expected_volumes)
                if payload.execution.expected_volumes
                else None
            ),
        ),
        margin=MarginRunParams(
            model=payload.margin.margin_model,
            spot_returns=tuple(grid.spot_returns) if grid and grid.spot_returns else None,
            vol_points=tuple(grid.vol_points) if grid and grid.vol_points else None,
            short_option_minimum_rate=payload.margin.short_option_minimum_rate,
            concentration_add_on_rate=payload.margin.concentration_add_on_rate,
            concentration_threshold=payload.margin.concentration_threshold,
            eligible_capital=payload.margin.eligible_capital,
            vol_co_shock=payload.margin.vol_co_shock,
        ),
        anomaly_policy=AnomalyPolicy(min_z_score=payload.min_deviation_z_score),
    )


@router.post("/order-analysis", response_model=Envelope)
async def analyse_order(
    payload: OrderAnalysisRequestIn,
    user: CurrentUser,
    portfolios: PortfolioServiceDep,
    orders: OrderAnalysisServiceDep,
    session: SessionDep,
) -> Envelope:
    """Valuation, surface deviation, execution cost and incremental risk and margin.

    All five read the one `MarketState` named in every provenance block, so the
    current-to-proposed differences are attributable to the order rather than to
    five calculations catching the market at five moments.

    A branch that cannot compute is `FAILED` with its reason and the analysis is
    `PARTIAL`; the others still answer. **The response contains no field that
    could be read as advice** — no action, signal, rating or score, and the
    execution schedules are listed side by side rather than ranked.
    """
    if await portfolios.get(payload.portfolio_id, user.id) is None:
        raise NotFound("Portfolio")

    # Both catalogues are checked here rather than left to fail inside a branch:
    # a model name nobody ships is a bad request, and answering it with a
    # degraded branch would bury the typo among four working ones.
    if payload.margin.margin_model not in MARGIN_MODELS:
        raise UnprocessableEntity(
            "UNKNOWN_MARGIN_MODEL",
            f"No margin model named {payload.margin.margin_model!r}. Available: "
            f"{', '.join(sorted(MARGIN_MODELS))}.",
            available=sorted(MARGIN_MODELS),
        )
    if payload.execution.impact_model not in IMPACT_MODELS:
        raise UnprocessableEntity(
            "UNKNOWN_IMPACT_MODEL",
            f"No impact model named {payload.execution.impact_model!r}. Available: "
            f"{', '.join(sorted(IMPACT_MODELS))}.",
            available=sorted(IMPACT_MODELS),
        )

    try:
        request = _to_domain(payload)
    except OrderAnalysisError as exc:
        raise UnprocessableEntity("INVALID_ORDER", str(exc)) from exc

    try:
        result, analysis_id = await orders.analyse(user.id, request)
    except PortfolioNotFound as exc:
        raise NotFound("Portfolio") from exc
    except (OrderAnalysisError, ValueError) as exc:
        raise UnprocessableEntity("ORDER_ANALYSIS_REFUSED", str(exc)) from exc

    await UserService(session).audit(
        AuditAction.ORDER_ANALYSED,
        user_id=user.id,
        resource_type="order_analysis",
        resource_id=str(analysis_id) if analysis_id else None,
        portfolio_id=str(payload.portfolio_id),
        instrument_id=str(payload.instrument_id),
    )
    await session.commit()

    payload_out = result.to_dict()
    return Envelope(
        status=payload_out["status"],
        results=payload_out["results"],
        warnings=[WarningOut(**item) for item in payload_out["warnings"]],
        provenance=ProvenanceOut(**payload_out["provenance"]),
    )


@router.get("/order-analysis", response_model=list[OrderAnalysisSummaryOut])
async def list_order_analyses(
    user: CurrentUser,
    orders: OrderAnalysisServiceDep,
    portfolio_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[OrderAnalysisSummaryOut]:
    rows = await orders.list(user.id, portfolio_id=portfolio_id, limit=limit, offset=offset)
    return [OrderAnalysisSummaryOut.model_validate(row) for row in rows]


@router.get("/order-analysis/{analysis_id}", response_model=OrderAnalysisOut)
async def get_order_analysis(
    analysis_id: uuid.UUID,
    user: CurrentUser,
    orders: OrderAnalysisServiceDep,
) -> OrderAnalysisOut:
    row = await orders.get(analysis_id, user.id)
    if row is None:
        raise NotFound("Order analysis")
    return OrderAnalysisOut(
        id=row.id,
        status=row.status,
        results=row.results,
        warnings=[WarningOut(**item) for item in row.warnings],
        provenance=row.provenance,
    )
