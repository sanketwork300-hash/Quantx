"""Risk job handlers.

Both are jobs because both fully reprice the book: historical VaR reprices it
once per stored observation, Monte Carlo once per path. That is the cost the
method exists to pay, and paying it inside a request would make the endpoint
lie about how long it takes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.application import DerivativesService
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.service import MarketDataService
from domains.reports.composition import FactorHistoryComposer, ValuationContextComposer
from domains.risk.application import (
    MarginRunParams,
    RiskApplicationService,
    RiskRunParams,
)
from domains.risk.var import VaRMethod
from domains.scenarios.service import ScenarioService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store
from quant.simulation.paths import Distribution


def _params(payload: dict) -> RiskRunParams:
    confidences = payload.get("confidences") or [0.95, 0.99]
    return RiskRunParams(
        risk_free_rate=float(payload.get("risk_free_rate") or 0.0),
        dividend_yield=float(payload.get("dividend_yield") or 0.0),
        settlement_time_utc=(
            time.fromisoformat(payload["settlement_time_utc"])
            if payload.get("settlement_time_utc")
            else None
        ),
        as_of=datetime.fromisoformat(payload["as_of"]) if payload.get("as_of") else None,
        lookback=payload.get("lookback"),
        horizon_days=int(payload.get("horizon_days") or 1),
        confidences=tuple(float(level) for level in confidences),
        paths=int(payload.get("paths") or 10_000),
        seed=int(payload.get("seed") or 20_260_924),
        distribution=Distribution(payload.get("distribution", "NORMAL")),
        degrees_of_freedom=float(payload.get("degrees_of_freedom") or 5.0),
        include_volatility_factor=bool(payload.get("include_volatility_factor", True)),
    )


def _composers(session: AsyncSession):
    settings = get_settings()
    market_data = MarketDataService(session, settings, get_object_store(settings))
    derivatives = DerivativesService(session, settings)
    return (
        ValuationContextComposer(market_data, derivatives),
        FactorHistoryComposer(market_data, derivatives),
    )


async def run_var(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    valuation_composer, history_composer = _composers(session)
    service = RiskApplicationService(session, get_settings())

    result, var_id = await service.run_var(
        job.user_id,
        uuid.UUID(payload["portfolio_id"]),
        VaRMethod(payload.get("method", "HISTORICAL")),
        _params(payload),
        valuation_composer,
        history_composer,
    )
    out = result.to_dict()
    if out.get("results") is not None and var_id is not None:
        out["results"]["var_id"] = str(var_id)
    return out


def _margin_params(payload: dict) -> MarginRunParams:
    from domains.risk.vulnerability import DEFAULT_LADDER

    grid = payload.get("grid") or {}
    return MarginRunParams(
        model=payload.get("margin_model") or "SimpleRiskMarginModel",
        spot_returns=tuple(grid["spot_returns"]) if grid.get("spot_returns") else None,
        vol_points=tuple(grid["vol_points"]) if grid.get("vol_points") else None,
        short_option_minimum_rate=float(payload.get("short_option_minimum_rate") or 0.0),
        concentration_add_on_rate=float(payload.get("concentration_add_on_rate") or 0.0),
        concentration_threshold=float(payload.get("concentration_threshold") or 0.5),
        eligible_capital=(
            float(payload["eligible_capital"])
            if payload.get("eligible_capital") is not None
            else None
        ),
        ladder=tuple(payload.get("ladder") or DEFAULT_LADDER),
        vol_co_shock=float(payload.get("vol_co_shock") or 0.0),
    )


async def run_margin(session: AsyncSession, job: Job) -> dict:
    """Estimate margin and scan the buffer ladder.

    A job because the ladder runs the margin model once per rung, and each of
    those reprices the whole book across the model's own shock grid.
    """
    payload = job.input_reference
    valuation_composer, _history = _composers(session)
    service = RiskApplicationService(session, get_settings())

    result, margin_id = await service.run_margin(
        job.user_id,
        uuid.UUID(payload["portfolio_id"]),
        _params(payload),
        _margin_params(payload),
        valuation_composer,
    )
    out = result.to_dict()
    if out.get("results") is not None and margin_id is not None:
        out["results"]["margin_id"] = str(margin_id)
    return out


async def run_stress(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    valuation_composer, _history = _composers(session)
    scenarios = ScenarioService(session)
    scenario = await scenarios.resolve(job.user_id, payload["scenario"])

    stored_id = None
    if scenario.source.value != "HYPOTHETICAL":
        row = await scenarios.repository.get(scenario.id, job.user_id)
        stored_id = row.id if row is not None else None

    service = RiskApplicationService(session, get_settings())
    result, stress_id = await service.run_stress(
        job.user_id,
        uuid.UUID(payload["portfolio_id"]),
        scenario,
        _params(payload),
        valuation_composer,
        time_decay_days=float(payload.get("time_decay_days") or 0.0),
        scenario_row_id=stored_id,
    )
    out = result.to_dict()
    if out.get("results") is not None and stress_id is not None:
        out["results"]["stress_id"] = str(stress_id)
    return out


def register_handlers() -> None:
    register(JobType.RUN_VAR, run_var)
    register(JobType.RUN_STRESS, run_stress)
    register(JobType.RUN_MARGIN, run_margin)
