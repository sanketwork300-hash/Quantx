from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from api.dependencies.core import (
    CurrentUser,
    JobServiceDep,
    MarketDataServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import BadRequest, NotFound, UnprocessableEntity
from api.schemas.uploads import (
    IngestRequest,
    JobAcceptedOut,
    PreviewRequest,
    PreviewResponse,
    UploadOut,
)
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.market_data.enums import UploadKind
from domains.market_data.ingestion.column_mapping import (
    OPTION_CHAIN_FIELDS,
    ColumnMapping,
)
from domains.market_data.service import UploadRejected
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def create_upload(
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    session: SessionDep,
    settings: SettingsDep,
    file: UploadFile = File(...),
    kind: UploadKind = Form(default=UploadKind.OPTION_CHAIN),
) -> UploadOut:
    """Store an uploaded file. Parsing happens later, in a worker.

    The file lands in the object store before anything reads it, so a hostile
    or malformed file is never parsed inside the request thread.
    """
    data = await file.read(settings.max_upload_bytes + 1)
    try:
        upload = await market_data.create_upload(
            user_id=user.id,
            kind=kind,
            filename=file.filename or "upload.csv",
            content_type=file.content_type or "text/csv",
            data=data,
        )
    except UploadRejected as exc:
        raise UnprocessableEntity(exc.code, str(exc)) from exc

    await UserService(session).audit(
        AuditAction.UPLOAD_RECEIVED,
        user_id=user.id,
        resource_type="upload",
        resource_id=str(upload.id),
        kind=str(kind),
        byte_size=upload.byte_size,
        sha256=upload.sha256,
    )
    await session.commit()
    return UploadOut.model_validate(upload)


@router.get("", response_model=list[UploadOut])
async def list_uploads(
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[UploadOut]:
    rows = await market_data.repository.list_uploads(user.id, limit=limit, offset=offset)
    return [UploadOut.model_validate(row) for row in rows]


@router.get("/{upload_id}", response_model=UploadOut)
async def get_upload(
    upload_id: uuid.UUID, user: CurrentUser, market_data: MarketDataServiceDep
) -> UploadOut:
    upload = await market_data.get_upload(upload_id, user.id)
    if upload is None:
        raise NotFound("Upload")
    return UploadOut.model_validate(upload)


@router.post("/{upload_id}/preview", response_model=PreviewResponse)
async def preview_upload(
    upload_id: uuid.UUID,
    payload: PreviewRequest,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
) -> PreviewResponse:
    """Show how the file would be interpreted. Persists nothing.

    Mandatory before ingestion in the UI: a misread column produces a plausible,
    wrong chain and no error at all, so the user confirms the mapping first.
    """
    upload = await market_data.get_upload(upload_id, user.id)
    if upload is None:
        raise NotFound("Upload")

    mapping = (
        ColumnMapping(mapping=dict(payload.column_mapping))
        if payload.column_mapping is not None
        else None
    )
    preview = await market_data.preview_upload(upload, mapping, limit=payload.limit)
    return PreviewResponse(**preview.to_dict())


@router.post(
    "/{upload_id}/ingest",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_upload(
    upload_id: uuid.UUID,
    payload: IngestRequest,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    upload = await market_data.get_upload(upload_id, user.id)
    if upload is None:
        raise NotFound("Upload")
    if payload.kind is not UploadKind.OPTION_CHAIN:
        raise BadRequest(
            "UNSUPPORTED_INGESTION_KIND",
            f"Ingestion of {payload.kind} is not implemented yet; see docs/backlog.md.",
        )

    mapping = ColumnMapping(mapping=dict(payload.column_mapping))
    missing = mapping.missing_required(OPTION_CHAIN_FIELDS)
    if missing:
        raise UnprocessableEntity(
            "COLUMN_MAPPING_INCOMPLETE",
            f"Required field(s) not mapped to a column: {', '.join(missing)}.",
            missing_required=list(missing),
        )

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.INGEST_OPTION_CHAIN,
        input_reference={
            "upload_id": str(upload.id),
            "underlying": payload.underlying.model_dump(mode="json"),
            "as_of_timestamp": payload.as_of_timestamp.isoformat(),
            "column_mapping": mapping.to_dict(),
            "underlying_price": (
                format(payload.underlying_price, "f")
                if payload.underlying_price is not None
                else None
            ),
            "risk_free_rate": payload.risk_free_rate,
            "dividend_yield": payload.dividend_yield,
            "contract": payload.contract.model_dump(mode="json"),
            "options": payload.options.model_dump(mode="json"),
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.INGEST_OPTION_CHAIN),
    )
    # The job must be durable before it is dispatched: a worker that picks it up
    # first would not find it.
    await session.commit()

    await submit_job(job.id, settings)

    # 202 reports the state at submission. Even in eager mode, where the job has
    # already finished by this line, re-reading here would report whatever this
    # session's identity map happens to hold rather than the truth. The client
    # polls GET /jobs/{id}, which is the one place job state is authoritative.
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))
