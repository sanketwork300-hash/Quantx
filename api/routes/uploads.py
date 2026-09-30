from __future__ import annotations

import uuid
from dataclasses import replace

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
    infer_mapping,
)
from domains.market_data.ingestion.layout import (
    ChainLayout,
    LayoutError,
    TwoSidedLayout,
)
from domains.market_data.ingestion.parser import DateOrder
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
    """Report how the file was read. Persists nothing.

    The file is read first and the reading is reported: which column each field
    came from, the first rows as they were read with the failures kept in, and
    whether the reading worked at all. Correcting a column is the exception
    rather than the entry price, because the ordinary user downloaded a chain
    from an exchange and has nothing to say about its columns.

    Supplying a ``column_mapping`` or ``layout`` re-reads the file that way, so
    a correction can be seen taking effect before anything is committed.
    """
    upload = await market_data.get_upload(upload_id, user.id)
    if upload is None:
        raise NotFound("Upload")

    mapping = (
        ColumnMapping(mapping=dict(payload.column_mapping))
        if payload.column_mapping is not None
        else None
    )
    try:
        layout = _layout(payload.layout)
    except LayoutError as exc:
        raise UnprocessableEntity("INVALID_LAYOUT", str(exc)) from exc
    preview = await market_data.preview_upload(
        upload,
        mapping,
        limit=payload.limit,
        layout=layout,
        date_order=DateOrder(payload.date_order) if payload.date_order else None,
    )
    return PreviewResponse(**preview.to_dict())


def _layout(payload) -> TwoSidedLayout | None:
    """Build the layout the caller confirmed, or ``None`` for a long-form file."""
    if payload is None:
        return None
    return TwoSidedLayout(
        header_row=payload.header_row,
        strike_column=payload.strike_column,
        call_columns=dict(payload.call_columns),
        put_columns=dict(payload.put_columns),
        shared_columns=dict(payload.shared_columns),
        expiry=payload.expiry,
    )


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
    if payload.kind is UploadKind.TRADES:
        raise BadRequest(
            "WRONG_INGESTION_ROUTE",
            "Trade logs are imported through the execution engine. Preview with "
            "POST /execution/trades/preview, then commit with "
            "POST /execution/trades/import.",
        )
    if payload.kind is UploadKind.POSITIONS:
        raise BadRequest(
            "WRONG_INGESTION_ROUTE",
            "Position files are imported into a specific portfolio. Preview with "
            "POST /portfolios/{portfolio_id}/import/preview, then commit with "
            "POST /portfolios/{portfolio_id}/import.",
        )
    if payload.kind in {UploadKind.BOOK_SNAPSHOTS, UploadKind.BOOK_EVENTS}:
        raise BadRequest(
            "WRONG_INGESTION_ROUTE",
            "Depth snapshots and event tapes are imported as a microstructure "
            "dataset, because the two halves are assessed together for what they "
            "can support. Preview with POST /microstructure/datasets/preview, "
            "then commit with POST /microstructure/datasets.",
        )
    if payload.kind is not UploadKind.OPTION_CHAIN:
        raise BadRequest(
            "UNSUPPORTED_INGESTION_KIND",
            f"Ingestion of {payload.kind} is not implemented yet; see docs/backlog.md.",
        )

    try:
        layout = _layout(payload.layout)
    except LayoutError as exc:
        raise UnprocessableEntity("INVALID_LAYOUT", str(exc)) from exc

    supplied = ColumnMapping(mapping=dict(payload.column_mapping))
    detection = None
    mapping_inferred = False
    if layout is None and not payload.column_mapping:
        # Nothing at all was said about the file -- the ordinary case for a user
        # who downloaded a chain from an exchange and uploaded it. Rather than
        # reject it field by field, read how it is actually arranged: a
        # two-sided export names its columns once per side and so can never be
        # described by a mapping at all. What was worked out is reported in the
        # result's warnings, with its evidence, because a misread file is
        # plausible rather than loud. A *partial* mapping is still an
        # instruction and is answered with the fields it is missing.
        found = await market_data.detect_upload_layout(upload)
        if found.layout is ChainLayout.TWO_SIDED and found.two_sided is not None:
            if found.suggested_expiry is None:
                raise UnprocessableEntity(
                    "LAYOUT_EXPIRY_REQUIRED",
                    "This file is a two-sided chain export: calls to the left of the "
                    "strike column, puts to the right. It names no expiry in any "
                    "column and none could be read from its filename, so the expiry "
                    "cannot be established from the upload. Preview the file and "
                    "supply layout.expiry.",
                    evidence=list(found.evidence),
                )
            detection = found
            layout = replace(found.two_sided, expiry=found.suggested_expiry)
        else:
            # A long-form file whose columns were never named: match each field
            # to a column by header name, the same step the preview shows. A
            # mapping that still cannot read the file falls through to the
            # error below, which names the fields that are missing.
            inferred = infer_mapping(list(found.headers), OPTION_CHAIN_FIELDS)
            if not inferred.missing_required(OPTION_CHAIN_FIELDS):
                supplied = inferred
                mapping_inferred = True

    # A two-sided file is resolved by column index, so the layout *is* the
    # mapping for it and the two must not both be asserted.
    mapping = layout.identity_mapping() if layout is not None else supplied
    missing = mapping.missing_required(OPTION_CHAIN_FIELDS)
    if missing:
        raise UnprocessableEntity(
            "COLUMN_MAPPING_INCOMPLETE",
            f"Required field(s) not mapped to a column: {', '.join(missing)}.",
            missing_required=list(missing),
        )

    # Read a sample before accepting the file. The mapping being *complete* only
    # says every required field points at some column; it does not say the
    # column holds that field. A file read with the wrong columns produced a
    # snapshot with almost nothing in it and no error anywhere, which downstream
    # is indistinguishable from a market with almost nothing in it. The same
    # rule runs again in the worker over the whole file; refusing here means the
    # user is told now rather than by a job that fails a minute later.
    date_order = DateOrder(payload.date_order) if payload.date_order else None
    verdict = (
        await market_data.preview_upload(upload, mapping, layout=layout, date_order=date_order)
    ).verdict
    if not verdict.readable:
        raise UnprocessableEntity(
            str(verdict.problem),
            verdict.message or "The file could not be read.",
            **verdict.to_dict(),
        )

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.INGEST_OPTION_CHAIN,
        input_reference={
            "upload_id": str(upload.id),
            "underlying": payload.underlying.model_dump(mode="json"),
            "as_of_timestamp": payload.as_of_timestamp.isoformat(),
            "column_mapping": mapping.to_dict(),
            "layout": layout.to_dict() if layout is not None else None,
            # Present only when the layout was read from the file rather than
            # named by the caller. It carries the evidence, which the result
            # reports: a misread layout is plausible rather than loud.
            "layout_detection": detection.to_dict() if detection is not None else None,
            "column_mapping_inferred": mapping_inferred,
            "date_order": payload.date_order,
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
