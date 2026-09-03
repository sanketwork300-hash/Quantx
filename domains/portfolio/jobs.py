"""Portfolio job handlers.

Valuation is a job because it is fan-out work: every position resolves an
instrument, reads a quote, and may price against a fitted surface. Import is a
job because a broker export can be tens of thousands of rows, each of which
resolves against the instrument master.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.application import DerivativesService
from domains.instruments.enums import AssetClass, ExerciseStyle, SettlementType
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.market_data.service import MarketDataService
from domains.portfolio.application import (
    PortfolioApplicationService,
    ValuePortfolioParams,
)
from domains.portfolio.importer import ImportDefaults
from domains.reports.composition import ValuationContextComposer
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


def _defaults_from_payload(payload: dict) -> ImportDefaults:
    raw = payload.get("defaults") or {}
    multiplier = raw.get("multiplier")
    return ImportDefaults(
        currency=raw.get("currency", "INR"),
        exchange=raw.get("exchange"),
        asset_class=AssetClass(raw["asset_class"]) if raw.get("asset_class") else None,
        multiplier=Decimal(str(multiplier)) if multiplier is not None else None,
        tick_size=Decimal(str(raw.get("tick_size", "0.05"))),
        lot_size=Decimal(str(raw.get("lot_size", "1"))),
        exercise_style=ExerciseStyle(raw.get("exercise_style", "EUROPEAN")),
        settlement_type=SettlementType(raw.get("settlement_type", "CASH")),
        create_missing_instruments=bool(raw.get("create_missing_instruments", True)),
    )


async def import_positions(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()

    market_data = MarketDataService(session, settings, get_object_store(settings))
    upload = await market_data.get_upload(uuid.UUID(payload["upload_id"]), job.user_id)
    if upload is None:
        raise LookupError("upload not found for this job")
    data = await market_data.read_upload(upload)

    application = PortfolioApplicationService(session, settings)
    result = await application.commit_import(
        user_id=job.user_id,
        portfolio_id=uuid.UUID(payload["portfolio_id"]),
        data=data,
        mapping=ColumnMapping(mapping=dict(payload["column_mapping"])),
        defaults=_defaults_from_payload(payload),
        replace_existing=bool(payload.get("replace_existing", False)),
    )
    out = result.to_dict()
    if out.get("results") is not None:
        out["results"]["upload_id"] = str(upload.id)
        out["results"]["dataset_digest"] = upload.sha256
    return out


async def value_portfolio(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()

    composer = ValuationContextComposer(
        MarketDataService(session, settings, get_object_store(settings)),
        DerivativesService(session, settings),
    )
    application = PortfolioApplicationService(session, settings)

    params = ValuePortfolioParams(
        risk_free_rate=float(payload.get("risk_free_rate") or 0.0),
        dividend_yield=float(payload.get("dividend_yield") or 0.0),
        settlement_time_utc=(
            time.fromisoformat(payload["settlement_time_utc"])
            if payload.get("settlement_time_utc")
            else None
        ),
        as_of=(datetime.fromisoformat(payload["as_of"]) if payload.get("as_of") else None),
    )
    result, valuation_id = await application.value_portfolio(
        job.user_id, uuid.UUID(payload["portfolio_id"]), params, composer
    )
    out = result.to_dict()
    if out.get("results") is not None and valuation_id is not None:
        out["results"]["valuation_id"] = str(valuation_id)
    return out


def register_handlers() -> None:
    register(JobType.IMPORT_POSITIONS, import_positions)
    register(JobType.VALUE_PORTFOLIO, value_portfolio)
