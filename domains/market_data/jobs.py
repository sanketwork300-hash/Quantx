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
from domains.market_data.ingestion.layout import LayoutDetection, TwoSidedLayout
from domains.market_data.ingestion.parser import DateOrder
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
        layout=(
            TwoSidedLayout.from_dict(payload["layout"])
            if payload.get("layout") is not None
            else None
        ),
        mapping_inferred=payload.get("column_mapping_inferred", False),
        layout_detection=(
            LayoutDetection.from_dict(payload["layout_detection"])
            if payload.get("layout_detection") is not None
            else None
        ),
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
        filename=upload.original_filename,
        date_order=DateOrder(payload["date_order"]) if payload.get("date_order") else None,
        upload_id=upload.id,
        dataset_digest=upload.sha256,
        provider="csv",
    )

    result = await service.ingest_option_chain(upload, request)
    return result.to_dict(serializer=lambda summary: summary.to_dict())


async def load_instrument_master(session: AsyncSession, job: Job) -> dict:
    """Load a provider's instrument file into instruments and alias mappings.

    A job rather than a request handler: the published files run to hundreds of
    thousands of rows, and the work belongs nowhere near an HTTP timeout.
    """
    from decimal import Decimal as _Decimal

    from domains.instruments.service import InstrumentService
    from domains.market_data.live import LiveMarketDataService
    from domains.market_data.providers.master_source import decode_rows, fetch
    from domains.market_data.providers.upstox_master import InstrumentMasterOptions
    from domains.market_data.streaming.live_state import LiveMarketStore
    from infrastructure.cache.client import get_cache

    payload = job.input_reference
    settings = get_settings()

    url = payload.get("url") or settings.upstox_instruments_url
    segments = tuple(payload.get("segments") or settings.upstox_segments)
    underlyings = tuple(payload.get("underlyings") or settings.upstox_underlyings)

    raw = await fetch(url)
    options = InstrumentMasterOptions(
        segments=segments,
        underlyings=underlyings,
        tick_size_scale=_Decimal(str(settings.upstox_tick_size_scale)),
    )

    service = LiveMarketDataService(
        InstrumentService(session),
        LiveMarketStore(get_cache(settings), settings.live_quote_ttl_seconds),
    )
    result = await service.load_instrument_master(decode_rows(raw), options)

    return {
        "url": url,
        "segments": list(segments),
        "underlyings": list(underlyings),
        **result.to_provenance(),
        #: A bounded sample rather than every rejection: a file with a hundred
        #: thousand rows filtered out would otherwise write a hundred thousand
        #: rows of explanation into the job result.
        "rejected_sample": [
            {
                "row_number": row.row_number,
                "instrument_key": row.instrument_key,
                "reason": row.reason,
                "detail": row.detail,
            }
            for row in result.rejected[:25]
        ],
    }


def register_handlers() -> None:
    register(JobType.INGEST_OPTION_CHAIN, ingest_option_chain)
    register(JobType.LOAD_INSTRUMENT_MASTER, load_instrument_master)
