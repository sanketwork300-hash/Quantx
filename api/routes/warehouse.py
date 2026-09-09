"""The historical data warehouse.

Register a file as a dataset, see what validation made of it, and query the
partitions it wrote. The four verbs the acceptance criterion names — registered,
validated, queryable, usable — are four things a caller can see rather than one
opaque success.

The rule the query endpoint enforces: a dataset validation **quarantined** is
not served. Serving it with a warning attached and hoping the warning is read is
how a series with an unadjusted split reaches a backtest.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    JobServiceDep,
    SessionDep,
    SettingsDep,
    WarehouseServiceDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.uploads import JobAcceptedOut
from api.schemas.warehouse import (
    DatasetDetailOut,
    DatasetQualityOut,
    DatasetSummaryOut,
    FindingsOut,
    IngestDatasetRequest,
    PartitionListOut,
    PartitionOut,
    QueryRowsOut,
)
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.market_data.service import MarketDataService
from domains.users.models import AuditAction
from domains.users.service import UserService
from domains.warehouse.enums import DatasetKind, DatasetLayer
from domains.warehouse.query import QueryRequest, QueryTooLarge
from domains.warehouse.service import DatasetQuarantined
from infrastructure.storage.factory import get_object_store

router = APIRouter(prefix="/warehouse", tags=["warehouse"])


def _quality(row) -> DatasetQualityOut:
    return DatasetQualityOut(
        completeness_score=row.completeness_score,
        consistency_score=row.consistency_score,
        outlier_score=row.outlier_score,
        source_score=row.source_score,
        freshness_score=row.freshness_score,
        overall_score=row.overall_score,
    )


def _summary(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "layer": row.layer,
        "kind": row.kind,
        "exchange": row.exchange,
        "status": row.status,
        "source": row.source,
        "corporate_action_treatment": row.corporate_action_treatment,
        "continuous": row.continuous,
        "rows_in": row.rows_in,
        "rows_written": row.rows_written,
        "rows_excluded": row.rows_excluded,
        "rows_rejected": row.rows_rejected,
        "rows_flagged": row.rows_flagged,
        "instrument_count": row.instrument_count,
        "partition_count": row.partition_count,
        "bytes_written": row.bytes_written,
        "first_observation": row.first_observation,
        "last_observation": row.last_observation,
        "quality": _quality(row),
        "created_at": row.created_at,
    }


@router.post("/datasets", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def ingest_dataset(
    payload: IngestDatasetRequest,
    user: CurrentUser,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Read an uploaded historical file into validated warehouse partitions.

    A job because a year of minute bars is millions of rows. Poll
    ``GET /jobs/{id}/result``: the result carries the reader's accounting and
    the warehouse's, and between them every row in the file is written,
    refused, unparseable or unresolvable.
    """
    market_data = MarketDataService(session, settings, get_object_store(settings))
    if await market_data.get_upload(payload.upload_id, user.id) is None:
        raise NotFound("Upload")

    job = await jobs.create(
        user.id,
        JobType.INGEST_HISTORICAL_DATASET,
        {
            "upload_id": str(payload.upload_id),
            "name": payload.name,
            "exchange": payload.exchange,
            "kind": str(payload.kind),
            "layer": str(payload.layer),
            "instrument_id": str(payload.instrument_id) if payload.instrument_id else None,
            "interval": payload.interval,
            "source": payload.source,
            "corporate_action_treatment": str(payload.corporate_action_treatment),
            "continuous": payload.continuous,
            "column_mapping": dict(payload.column_mapping),
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.INGEST_HISTORICAL_DATASET),
    )
    await session.commit()
    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/datasets", response_model=list[DatasetSummaryOut])
async def list_datasets(
    user: CurrentUser,
    warehouse: WarehouseServiceDep,
    layer: DatasetLayer | None = None,
    kind: DatasetKind | None = None,
    exchange: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DatasetSummaryOut]:
    rows = await warehouse.list_datasets(
        user.id,
        layer=str(layer) if layer else None,
        kind=str(kind) if kind else None,
        exchange=exchange,
        limit=limit,
        offset=offset,
    )
    return [DatasetSummaryOut.model_validate(_summary(row)) for row in rows]


@router.get("/datasets/{dataset_id}", response_model=DatasetDetailOut)
async def get_dataset(
    dataset_id: uuid.UUID, user: CurrentUser, warehouse: WarehouseServiceDep
) -> DatasetDetailOut:
    row = await warehouse.get_dataset(dataset_id, user.id)
    if row is None:
        raise NotFound("Dataset")
    return DatasetDetailOut.model_validate(
        {
            **_summary(row),
            "dataset_digest": row.dataset_digest,
            "quality_evidence": row.quality_evidence or {},
            "validation_summary": row.validation_summary or {},
            "provenance": row.provenance or {},
        }
    )


@router.get("/datasets/{dataset_id}/partitions", response_model=PartitionListOut)
async def list_partitions(
    dataset_id: uuid.UUID,
    user: CurrentUser,
    warehouse: WarehouseServiceDep,
    limit: int = Query(default=500, ge=1, le=2000),
) -> PartitionListOut:
    """Where the files are, and what is in each of them."""
    if await warehouse.get_dataset(dataset_id, user.id) is None:
        raise NotFound("Dataset")
    rows = await warehouse.list_partitions(dataset_id, limit)
    return PartitionListOut(
        items=[PartitionOut.model_validate(row, from_attributes=True) for row in rows],
        total_rows=sum(row.rows for row in rows),
        total_bytes=sum(row.bytes_written for row in rows),
    )


@router.get("/datasets/{dataset_id}/findings", response_model=FindingsOut)
async def dataset_findings(
    dataset_id: uuid.UUID, user: CurrentUser, warehouse: WarehouseServiceDep
) -> FindingsOut:
    """Everything the validator said, in full. Nothing here was repaired.

    The dataset row carries counts and a capped sample; this is the complete
    list, which is what makes "every rejected row reports its position and
    reason" true for every row rather than for the first twenty-five.
    """
    row = await warehouse.get_dataset(dataset_id, user.id)
    if row is None:
        raise NotFound("Dataset")
    return FindingsOut.model_validate(await warehouse.findings(row))


@router.get("/query", response_model=QueryRowsOut)
async def query_warehouse(
    user: CurrentUser,
    warehouse: WarehouseServiceDep,
    kind: DatasetKind = DatasetKind.BARS,
    layer: DatasetLayer = DatasetLayer.NORMALIZED,
    exchange: str | None = None,
    instrument_ids: list[uuid.UUID] = Query(default_factory=list),
    start: datetime | None = None,
    end: datetime | None = None,
    columns: list[str] = Query(default_factory=list),
    limit: int = Query(default=5000, ge=1, le=200_000),
    exclude_flagged: bool = False,
    dataset_id: uuid.UUID | None = None,
) -> QueryRowsOut:
    """Read the warehouse.

    ``exclude_flagged`` defaults to False on purpose. Rows the validator was
    unhappy about are in the partitions with their flags attached, and whether
    to use them is the caller's judgement — an outlier is often the most
    important observation in a sample.
    """
    for moment, name in ((start, "start"), (end, "end")):
        if moment is not None and moment.tzinfo is None:
            raise UnprocessableEntity(
                "TIMESTAMP_NOT_TIMEZONE_AWARE",
                f"{name} must carry a UTC offset; a naive timestamp does not name a moment.",
            )

    request = QueryRequest(
        layer=layer,
        kind=kind,
        exchange=exchange,
        instrument_ids=tuple(instrument_ids),
        start=start,
        end=end,
        columns=tuple(columns),
        limit=limit,
        exclude_flagged=exclude_flagged,
    )
    try:
        result = await warehouse.run_query(user.id, request, dataset_id)
    except DatasetQuarantined as exc:
        raise UnprocessableEntity(
            "DATASET_QUARANTINED", str(exc), dataset_id=str(exc.dataset_id)
        ) from exc
    except QueryTooLarge as exc:
        raise UnprocessableEntity(
            "WAREHOUSE_QUERY_TOO_LARGE",
            str(exc),
            partitions_requested=exc.requested,
            partitions_allowed=exc.allowed,
        ) from exc
    except LookupError as exc:
        raise NotFound("Dataset") from exc

    payload = result.table.to_pylist() if result.rows else []
    return QueryRowsOut(
        columns=list(result.table.column_names),
        rows=[{key: _jsonable(value) for key, value in row.items()} for row in payload],
        row_count=result.rows,
        read_path=str(result.read_path),
        partitions_read=result.partitions_read,
        truncated=result.truncated,
        warnings=list(result.warnings),
    )


def _jsonable(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        # Decimals are stored exact and serialised as strings, so a price never
        # round-trips through a float on the way to a client.
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
