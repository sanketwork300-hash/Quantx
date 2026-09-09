"""Research job handlers."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.research.costs import (
    NO_COST_MODEL,
    NO_SLIPPAGE_MODEL,
    SlippageModel,
    schedule_from_components,
)
from domains.research.models import ExecutionTiming
from domains.research.service import BacktestRequest, ResearchService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


def _request_from(payload: dict) -> BacktestRequest:
    """Rebuild the request from what was recorded, so a re-run is the same run."""
    components = payload.get("cost_components") or []
    costs = (
        schedule_from_components(
            payload.get("cost_schedule_name") or "supplied",
            components,
            source=payload.get("cost_schedule_source") or "supplied with the request",
        )
        if components
        else NO_COST_MODEL
    )
    slippage_bps = payload.get("slippage_basis_points")
    slippage = (
        SlippageModel(
            basis_points=Decimal(str(slippage_bps)),
            source=payload.get("slippage_source") or "supplied with the request",
        )
        if slippage_bps
        else NO_SLIPPAGE_MODEL
    )

    return BacktestRequest(
        name=payload["name"],
        instrument_id=uuid.UUID(payload["instrument_id"]),
        strategy_name=payload["strategy_name"],
        strategy_parameters=dict(payload.get("strategy_parameters") or {}),
        start=datetime.fromisoformat(payload["start"]) if payload.get("start") else None,
        end=datetime.fromisoformat(payload["end"]) if payload.get("end") else None,
        exchange=payload.get("exchange"),
        dataset_id=uuid.UUID(payload["dataset_id"]) if payload.get("dataset_id") else None,
        initial_cash=Decimal(str(payload.get("initial_cash", "1000000"))),
        timing=ExecutionTiming(payload.get("timing", ExecutionTiming.NEXT_OPEN)),
        costs=costs,
        slippage=slippage,
        max_position_weight=Decimal(str(payload.get("max_position_weight", "1"))),
        max_gross_exposure=Decimal(str(payload.get("max_gross_exposure", "1"))),
        include_flagged_bars=bool(payload.get("include_flagged_bars", False)),
        risk_free_rate=float(payload.get("risk_free_rate") or 0.0),
    )


async def run_backtest(session: AsyncSession, job: Job) -> dict:
    """Run one strategy over one instrument's warehouse history."""
    settings = get_settings()
    service = ResearchService(session, settings, get_object_store(settings))
    result = await service.run_backtest(job.user_id, _request_from(job.input_reference))
    return result.to_dict(
        serializer=lambda summary: summary.to_dict(include_curve=False, include_fills=False)
    )


def register_handlers() -> None:
    register(JobType.RUN_BACKTEST, run_backtest)
