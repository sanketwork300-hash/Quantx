"""Execution job handlers.

Both are jobs for the same reason as everywhere else: an import resolves every
row against the instrument master, and an analysis assembles a market window and
six benchmarks per parent order. Doing either inside a request would make the
endpoint lie about how long it takes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.execution.application import (
    AnalyseParams,
    ExecutionApplicationService,
    SimulateParams,
)
from domains.execution.benchmarks import BenchmarkKind
from domains.execution.importer import TradeImportDefaults
from domains.execution.models import Side
from domains.instruments.enums import AssetClass, ExerciseStyle, SettlementType
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.market_data.service import MarketDataService
from domains.reports.composition import ExecutionWindowComposer
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


def _defaults(payload: dict) -> TradeImportDefaults:
    raw = payload.get("defaults") or {}
    multiplier = raw.get("multiplier")
    return TradeImportDefaults(
        currency=raw.get("currency", "INR"),
        exchange=raw.get("exchange"),
        asset_class=AssetClass(raw["asset_class"]) if raw.get("asset_class") else None,
        multiplier=Decimal(str(multiplier)) if multiplier is not None else None,
        tick_size=Decimal(str(raw.get("tick_size", "0.05"))),
        lot_size=Decimal(str(raw.get("lot_size", "1"))),
        exercise_style=ExerciseStyle(raw.get("exercise_style", "EUROPEAN")),
        settlement_type=SettlementType(raw.get("settlement_type", "CASH")),
        create_missing_instruments=bool(raw.get("create_missing_instruments", True)),
        broker=raw.get("broker"),
        parent_gap_seconds=float(raw.get("parent_gap_seconds") or 300.0),
    )


async def import_trades(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()

    market_data = MarketDataService(session, settings, get_object_store(settings))
    upload = await market_data.get_upload(uuid.UUID(payload["upload_id"]), job.user_id)
    if upload is None:
        raise LookupError("upload not found for this job")
    data = await market_data.read_upload(upload)

    service = ExecutionApplicationService(session, settings)
    result = await service.commit_import(
        user_id=job.user_id,
        data=data,
        mapping=ColumnMapping(mapping=dict(payload["column_mapping"])),
        defaults=_defaults(payload),
        upload_id=upload.id,
    )
    out = result.to_dict()
    if out.get("results") is not None:
        out["results"]["upload_id"] = str(upload.id)
        out["results"]["dataset_digest"] = upload.sha256
    return out


async def analyse_executions(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()

    composer = ExecutionWindowComposer(
        MarketDataService(session, settings, get_object_store(settings))
    )
    service = ExecutionApplicationService(session, settings)

    params = AnalyseParams(
        start=datetime.fromisoformat(payload["start"]) if payload.get("start") else None,
        end=datetime.fromisoformat(payload["end"]) if payload.get("end") else None,
        instrument_id=(
            uuid.UUID(payload["instrument_id"]) if payload.get("instrument_id") else None
        ),
        parent_order_key=payload.get("parent_order_key"),
        primary_benchmark=BenchmarkKind(payload.get("primary_benchmark", "ARRIVAL")),
        parent_gap_seconds=float(payload.get("parent_gap_seconds") or 300.0),
        staleness_tolerance_seconds=float(payload.get("staleness_tolerance_seconds") or 300.0),
        window_padding_seconds=float(payload.get("window_padding_seconds") or 3600.0),
    )
    result, report_ids = await service.analyse(job.user_id, params, composer)
    out = result.to_dict()
    if out.get("results") is not None:
        out["results"]["report_ids"] = [str(item) for item in report_ids]
    return out


async def simulate_execution(session: AsyncSession, job: Job) -> dict:
    """Run one or more schedules against a past path.

    A job because each strategy walks every slice, asks the impact model, and
    then runs the simulated fills through the whole Phase 7 benchmark set.
    """
    payload = job.input_reference
    settings = get_settings()

    composer = ExecutionWindowComposer(
        MarketDataService(session, settings, get_object_store(settings))
    )
    service = ExecutionApplicationService(session, settings)

    def sequence(name: str) -> tuple[float, ...] | None:
        values = payload.get(name)
        return tuple(float(item) for item in values) if values else None

    params = SimulateParams(
        instrument_id=uuid.UUID(payload["instrument_id"]),
        side=Side(payload["side"]),
        quantity=Decimal(str(payload["quantity"])),
        start=datetime.fromisoformat(payload["start"]),
        end=datetime.fromisoformat(payload["end"]),
        intervals=int(payload.get("intervals") or 6),
        strategies=tuple(payload.get("strategies") or ("TWAP",)),
        impact_model=payload.get("impact_model") or "SquareRootImpactModel",
        permanent_coefficient=float(payload.get("permanent_coefficient") or 1.0),
        temporary_coefficient=float(payload.get("temporary_coefficient") or 1.0),
        volatility=float(payload.get("volatility") or 0.2),
        average_daily_volume=float(payload.get("average_daily_volume") or 0.0),
        lot_size=Decimal(str(payload.get("lot_size") or 1)),
        expected_volumes=sequence("expected_volumes"),
        spreads=sequence("spreads"),
        volatilities=sequence("volatilities"),
        participation_rate=float(payload.get("participation_rate") or 0.10),
        latency_seconds=float(payload.get("latency_seconds") or 0.0),
        max_price_age_seconds=(
            float(payload["max_price_age_seconds"])
            if payload.get("max_price_age_seconds") is not None
            else None
        ),
        window_padding_seconds=float(payload.get("window_padding_seconds") or 3600.0),
        staleness_tolerance_seconds=float(payload.get("staleness_tolerance_seconds") or 300.0),
    )
    result, comparison_id = await service.simulate(job.user_id, params, composer)
    out = result.to_dict()
    if out.get("results") is not None and comparison_id is not None:
        out["results"]["comparison_id"] = str(comparison_id)
    return out


def register_handlers() -> None:
    register(JobType.IMPORT_TRADES, import_trades)
    register(JobType.ANALYZE_EXECUTIONS, analyse_executions)
    register(JobType.SIMULATE_EXECUTION, simulate_execution)
