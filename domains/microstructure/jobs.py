"""Microstructure job handlers.

All three are jobs for the same reason: each reads a parquet file that can run
to millions of rows. An import parses and rewrites the whole tape, an analytics
run measures every snapshot in it, and an intensity fit evaluates a likelihood
over every event several hundred times. None of those belongs inside a request
that has to answer in a second.

A queue estimate is deliberately *not* a job: it reads one level from one
snapshot and counts the events at that price, which is cheap enough to answer
inline, and making it asynchronous would put a polling loop in front of a
question the user asks repeatedly while moving a price around.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.enums import BookEventType, BookSide
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.market_data.service import MarketDataService
from domains.microstructure.analytics import BookAnalyticsParams
from domains.microstructure.application import (
    ImportParams,
    MicrostructureApplicationService,
)
from domains.microstructure.importer import LevelColumns
from domains.microstructure.intensity import IntensityParams
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


def _level_columns(payload: dict | None) -> LevelColumns | None:
    """Rehydrate a confirmed wide-CSV mapping from the job's input reference.

    A commit carries the mapping it was previewed with, so what is imported is
    what the user reviewed — the same rule as the option-chain and trade-log
    imports.
    """
    if not payload:
        return None
    levels = {
        side: {int(index): dict(columns) for index, columns in (indices or {}).items()}
        for side, indices in (payload.get("levels") or {}).items()
    }
    return LevelColumns(
        timestamp=payload.get("timestamp"),
        receive_timestamp=payload.get("receive_timestamp"),
        sequence=payload.get("sequence"),
        levels={"bid": levels.get("bid", {}), "ask": levels.get("ask", {})},
        unrecognised=tuple(payload.get("unrecognised_columns") or ()),
    )


async def import_book_data(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()
    store = get_object_store(settings)
    market_data = MarketDataService(session, settings, store)

    async def read(key: str) -> bytes | None:
        upload_id = payload.get(key)
        if not upload_id:
            return None
        upload = await market_data.get_upload(uuid.UUID(upload_id), job.user_id)
        if upload is None:
            raise LookupError(f"upload {upload_id} not found for this job")
        return await market_data.read_upload(upload)

    snapshot_data = await read("snapshot_upload_id")
    event_data = await read("event_upload_id")

    service = MicrostructureApplicationService(session, settings, store)
    params = ImportParams(
        instrument_id=uuid.UUID(payload["instrument_id"]),
        name=payload["name"],
        source=payload.get("source") or "user-upload",
        snapshot_columns=_level_columns(payload.get("snapshot_columns")),
        event_mapping=(
            ColumnMapping(mapping=dict(payload["event_mapping"]))
            if payload.get("event_mapping")
            else None
        ),
        snapshot_upload_id=(
            uuid.UUID(payload["snapshot_upload_id"]) if payload.get("snapshot_upload_id") else None
        ),
        event_upload_id=(
            uuid.UUID(payload["event_upload_id"]) if payload.get("event_upload_id") else None
        ),
    )
    result, dataset_id = await service.commit_import(job.user_id, params, snapshot_data, event_data)
    out = result.to_dict()
    if out.get("results") is not None and dataset_id is not None:
        out["results"]["dataset_id"] = str(dataset_id)
    return out


async def analyse_microstructure(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()
    service = MicrostructureApplicationService(session, settings, get_object_store(settings))

    dataset = await service.get_dataset(uuid.UUID(payload["dataset_id"]), job.user_id)
    if dataset is None:
        raise LookupError("microstructure dataset not found for this job")

    params = BookAnalyticsParams(
        levels=int(payload.get("levels") or 5),
        weighted_decay=float(payload.get("weighted_decay") or 0.5),
        trade_sizes=tuple(float(size) for size in (payload.get("trade_sizes") or ())),
        preview_points=int(payload.get("preview_points") or 500),
    )
    result, report_id = await service.analyse(job.user_id, dataset, params)
    out = result.to_dict()
    if out.get("results") is not None and report_id is not None:
        out["results"]["report_id"] = str(report_id)
    return out


async def fit_intensity(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()
    service = MicrostructureApplicationService(session, settings, get_object_store(settings))

    dataset = await service.get_dataset(uuid.UUID(payload["dataset_id"]), job.user_id)
    if dataset is None:
        raise LookupError("microstructure dataset not found for this job")

    params = IntensityParams(
        event_types=tuple(BookEventType(item) for item in (payload.get("event_types") or ())),
        side=BookSide(payload["side"]) if payload.get("side") else None,
        price=Decimal(str(payload["price"])) if payload.get("price") is not None else None,
        train_fraction=float(payload.get("train_fraction") or 0.7),
        critical_value=float(payload.get("critical_value") or 1.645),
    )
    result, model_id = await service.fit_intensity(job.user_id, dataset, params)
    out = result.to_dict()
    if out.get("results") is not None and model_id is not None:
        out["results"]["intensity_model_id"] = str(model_id)
    return out


def register_handlers() -> None:
    register(JobType.IMPORT_BOOK_DATA, import_book_data)
    register(JobType.ANALYZE_MICROSTRUCTURE, analyse_microstructure)
    register(JobType.FIT_INTENSITY, fit_intensity)


__all__ = [
    "analyse_microstructure",
    "fit_intensity",
    "import_book_data",
    "register_handlers",
]
