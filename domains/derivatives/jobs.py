"""Derivatives job handlers.

Chain analysis is a job because it scales with the chain: a full option-market
scan solves hundreds of thousands of implied volatilities, and that must never
run inside a request thread.
"""

from __future__ import annotations

import uuid
from datetime import time

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.application import AnalyzeChainParams, DerivativesService
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.service import MarketDataService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store
from quant.daycount import DEFAULT_DAY_COUNT, DayCount


async def analyze_option_chain(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()
    market_data = MarketDataService(session, settings, get_object_store(settings))
    derivatives = DerivativesService(session, settings)

    params = AnalyzeChainParams(
        risk_free_rate=float(payload.get("risk_free_rate") or 0.0),
        dividend_yield=float(payload.get("dividend_yield") or 0.0),
        dividend_yield_assumed=bool(payload.get("dividend_yield_assumed", True)),
        settlement_time_utc=(
            time.fromisoformat(payload["settlement_time_utc"])
            if payload.get("settlement_time_utc")
            else None
        ),
        day_count=DayCount(payload.get("day_count") or DEFAULT_DAY_COUNT),
        include_excluded_quotes=bool(payload.get("include_excluded_quotes", False)),
    )

    result, analysis_id = await derivatives.analyze_chain(
        job.user_id, uuid.UUID(payload["snapshot_id"]), params, market_data
    )
    # The full point set lives in option_implied_vols; the job result carries
    # the summary and a pointer, so a 100k-quote analysis does not land in a
    # JSON column.
    payload_out = result.to_dict(serializer=lambda a: a.to_dict(include_points=False))
    payload_out["analysis_id"] = str(analysis_id)
    return payload_out


def register_handlers() -> None:
    register(JobType.ANALYZE_OPTION_CHAIN, analyze_option_chain)
