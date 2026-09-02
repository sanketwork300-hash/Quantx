"""Market-data job handlers."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    SettlementType,
)
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.market_data.ingestion.pipeline import (
    ContractSpec,
    IngestionOptions,
    OptionChainIngestionRequest,
    UnderlyingSpec,
)
from domains.market_data.quality.flags import Severity
from domains.market_data.service import MarketDataService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


async def ingest_option_chain(session: AsyncSession, job: Job) -> dict:
    """Run the option-chain ingestion pipeline for an uploaded file."""
    payload = job.input_reference
    settings = get_settings()
    service = MarketDataService(session, settings, get_object_store(settings))

    upload = await service.get_upload(uuid.UUID(payload["upload_id"]), job.user_id)
    if upload is None:
        raise LookupError("upload not found for this job")

    underlying = payload["underlying"]
    contract = payload.get("contract", {})
    options = payload.get("options", {})

    request = OptionChainIngestionRequest(
        user_id=job.user_id,
        underlying=UnderlyingSpec(
            symbol=underlying["symbol"],
            exchange=underlying["exchange"],
            asset_class=AssetClass(underlying.get("asset_class", "INDEX")),
            currency=underlying.get("currency", "INR"),
        ),
        as_of=datetime.fromisoformat(payload["as_of_timestamp"]),
        column_mapping=ColumnMapping(mapping=dict(payload["column_mapping"])),
        contract=ContractSpec(
            multiplier=(
                Decimal(contract["multiplier"]) if contract.get("multiplier") is not None else None
            ),
            tick_size=Decimal(contract.get("tick_size", "0.05")),
            lot_size=Decimal(contract.get("lot_size", "1")),
            exercise_style=ExerciseStyle(contract.get("exercise_style", "EUROPEAN")),
            settlement_type=SettlementType(contract.get("settlement_type", "CASH")),
            expiry_time_utc=(
                time.fromisoformat(contract["expiry_time_utc"])
                if contract.get("expiry_time_utc")
                else None
            ),
        ),
        options=IngestionOptions(
            exclusion_severity_threshold=Severity[
                options.get("exclusion_severity_threshold", "ERROR")
            ],
            create_missing_instruments=options.get("create_missing_instruments", True),
            source_label=options.get("source_label", "user-upload"),
        ),
        underlying_price=(
            Decimal(payload["underlying_price"])
            if payload.get("underlying_price") is not None
            else None
        ),
        risk_free_rate=payload.get("risk_free_rate"),
        dividend_yield=payload.get("dividend_yield"),
        upload_id=upload.id,
        dataset_digest=upload.sha256,
        provider="csv",
    )

    result = await service.ingest_option_chain(upload, request)
    return result.to_dict(serializer=lambda summary: summary.to_dict())


def register_handlers() -> None:
    register(JobType.INGEST_OPTION_CHAIN, ingest_option_chain)
