"""Warehouse job handlers."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.service import InstrumentService
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.market_data.service import MarketDataService
from domains.warehouse.enums import CorporateActionTreatment, DatasetKind, DatasetLayer
from domains.warehouse.readers import BarFileReader, ReaderError
from domains.warehouse.service import WarehouseService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


async def ingest_historical_dataset(session: AsyncSession, job: Job) -> dict:
    """Read an uploaded historical file into validated warehouse partitions.

    The four stages the acceptance criterion names are four visible steps here:
    read, validate, write, register. A failure in the first is reported as a
    failure to read rather than as an empty dataset, because an empty dataset
    that registered cleanly is the worst possible outcome — it looks like the
    file had no rows.
    """
    payload = job.input_reference
    settings = get_settings()
    store = get_object_store(settings)

    market_data = MarketDataService(session, settings, store)
    upload = await market_data.get_upload(uuid.UUID(payload["upload_id"]), job.user_id)
    if upload is None:
        raise LookupError("upload not found for this job")

    data = await market_data.read_upload(upload)
    reader = BarFileReader(InstrumentService(session), settings.max_upload_rows)

    try:
        read = await reader.read(
            data,
            exchange=payload["exchange"],
            mapping=ColumnMapping(mapping=dict(payload.get("column_mapping") or {})),
            instrument_id=(
                uuid.UUID(payload["instrument_id"]) if payload.get("instrument_id") else None
            ),
            interval=payload.get("interval", "1d"),
        )
    except ReaderError as exc:
        # Reported as a read failure. Registering an empty dataset here would
        # look exactly like a file that genuinely had no rows in it.
        raise ValueError(str(exc)) from exc

    warehouse = WarehouseService(session, settings, store)
    result = await warehouse.ingest(
        user_id=job.user_id,
        name=payload["name"],
        kind=DatasetKind(payload.get("kind", DatasetKind.BARS)),
        exchange=payload["exchange"],
        rows=read.rows,
        layer=DatasetLayer(payload.get("layer", DatasetLayer.NORMALIZED)),
        source=payload.get("source") or upload.original_filename,
        treatment=CorporateActionTreatment(
            payload.get("corporate_action_treatment", CorporateActionTreatment.UNKNOWN)
        ),
        continuous=bool(payload.get("continuous", False)),
    )

    return {
        "upload_id": str(upload.id),
        # The reader's own accounting, kept beside the warehouse's. Between them
        # every row in the file is a row written, a row the validator refused, a
        # row that would not parse, or a symbol nobody could resolve.
        "read": read.to_dict(),
        **result.to_dict(serializer=lambda summary: summary.to_dict()),
    }


def register_handlers() -> None:
    register(JobType.INGEST_HISTORICAL_DATASET, ingest_historical_dataset)
