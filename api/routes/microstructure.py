"""Microstructure routes.

Every analytical route on this router does the same thing first: it looks up
the dataset's stored availability report and refuses if the capability it needs
was not granted. The refusal is a 422 carrying the closed-vocabulary reason and
the evidence it was decided on, because "this data cannot support that" is a
statement about the request, not a failed calculation.

There is deliberately no ``force`` parameter anywhere here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    InstrumentServiceDep,
    JobServiceDep,
    MarketDataServiceDep,
    MicrostructureServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.microstructure import (
    AnalyseBookRequest,
    BookReportOut,
    CapabilityOut,
    DatasetImportRequest,
    DatasetOut,
    DatasetPreviewOut,
    DatasetPreviewRequest,
    FitIntensityRequest,
    IntensityOut,
    QueueEstimateOut,
    QueueEstimateRequest,
    RejectionsOut,
)
from api.schemas.uploads import JobAcceptedOut
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.market_data.enums import BookEventType, UploadKind
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.microstructure.application import ImportParams
from domains.microstructure.availability import (
    MAX_TIED_EVENT_FRACTION,
    MIN_CANCEL_EVENTS,
    MIN_DEPTH_LEVELS,
    MIN_INTENSITY_EVENTS,
    MIN_WINDOW_SECONDS,
    CapabilityRefused,
    MicrostructureCapability,
)
from domains.microstructure.importer import LevelColumns
from domains.microstructure.queue import QueueParams
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/microstructure", tags=["microstructure"])


async def _upload(market_data, upload_id: uuid.UUID | None, user_id: uuid.UUID, kind: UploadKind):
    if upload_id is None:
        return None
    upload = await market_data.get_upload(upload_id, user_id)
    if upload is None:
        raise NotFound("Upload")
    if upload.kind != str(kind):
        raise UnprocessableEntity(
            "WRONG_UPLOAD_KIND",
            f"Upload {upload_id} was received as {upload.kind}, not {kind}. "
            "Depth snapshots and event tapes are different shapes and are read "
            "by different parsers; re-upload it with the right kind rather than "
            "letting one be read as the other.",
        )
    return upload


def _level_columns(payload) -> LevelColumns | None:
    if payload is None:
        return None
    return LevelColumns(
        timestamp=payload.timestamp,
        receive_timestamp=payload.receive_timestamp,
        sequence=payload.sequence,
        levels={
            side: {int(index): dict(columns) for index, columns in (indices or {}).items()}
            for side, indices in (payload.levels or {}).items()
        },
        unrecognised=tuple(payload.unrecognised_columns),
    )


def _refusal(exc: CapabilityRefused) -> UnprocessableEntity:
    """A refused capability is a 422 with the reason and the evidence."""
    return UnprocessableEntity(
        "MICROSTRUCTURE_CAPABILITY_REFUSED",
        exc.assessment.message,
        capability=str(exc.capability),
        reason=str(exc.reason) if exc.reason else None,
        evidence=exc.assessment.evidence,
    )


async def _dataset(service, dataset_id: uuid.UUID, user_id: uuid.UUID):
    dataset = await service.get_dataset(dataset_id, user_id)
    if dataset is None:
        raise NotFound("Microstructure dataset")
    return dataset


# ----------------------------------------------------------------- reference
@router.get("/capabilities", response_model=list[dict])
async def list_capabilities(_user: CurrentUser) -> list[dict]:
    """What a dataset can be asked for, and what each needs to be answerable.

    Published as reference so a user can tell, before uploading anything,
    whether the feed they have can support the analytic they want. A
    snapshot-only export cannot support a queue model however it is post-
    processed, and finding that out after the upload is a worse experience than
    reading it here.
    """
    return [
        {
            "capability": str(MicrostructureCapability.TOP_OF_BOOK),
            "measures": ["spread", "mid", "microprice", "top-of-book imbalance"],
            "requires": "at least one depth snapshot with a price on both sides",
        },
        {
            "capability": str(MicrostructureCapability.DEPTH_ANALYTICS),
            "measures": [
                "multi-level depth",
                "weighted imbalance",
                "book slope",
                "depth concentration",
                "cost to trade",
            ],
            "requires": f"snapshots carrying at least {MIN_DEPTH_LEVELS} levels per side",
        },
        {
            "capability": str(MicrostructureCapability.EVENT_INTENSITY),
            "measures": ["arrival rate for a chosen event type"],
            "requires": (
                f"an event tape of at least {MIN_INTENSITY_EVENTS} events spanning "
                f"at least {MIN_WINDOW_SECONDS:.0f} seconds"
            ),
        },
        {
            "capability": str(MicrostructureCapability.CANCELLATION_INTENSITY),
            "measures": ["cancellation rate, separately from the trade rate"],
            "requires": (
                f"at least {MIN_CANCEL_EVENTS} events labelled as cancellations; "
                "they are never inferred from a size decrease"
            ),
        },
        {
            "capability": str(MicrostructureCapability.SELF_EXCITATION),
            "measures": [
                "Hawkes branching ratio and excitation half-life, "
                "reported only if they beat a constant rate out of sample"
            ],
            "requires": (
                "an event tape as above, with fewer than "
                f"{MAX_TIED_EVENT_FRACTION:.0%} of consecutive events sharing a "
                "timestamp"
            ),
        },
        {
            "capability": str(MicrostructureCapability.QUEUE_POSITION),
            "measures": ["bracketed queue position, wait time and fill probability"],
            "requires": (
                "priced, sided events with a complete monotone sequence, plus "
                "snapshots to read the resting size from"
            ),
        },
    ]


# -------------------------------------------------------------------- import
@router.post("/datasets/preview", response_model=DatasetPreviewOut)
async def preview_dataset(
    payload: DatasetPreviewRequest,
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    market_data: MarketDataServiceDep,
    instruments: InstrumentServiceDep,
    settings: SettingsDep,
) -> DatasetPreviewOut:
    """Parse both halves, assess what they support, and write nothing.

    The preview exists for one reason above the others: the wide-CSV level
    columns are *detected*, and a file whose price and size columns are read the
    wrong way round parses cleanly and produces analytics that are wrong in
    every number. The detected mapping shown here is what the commit must send
    back, so what is imported is what was reviewed.
    """
    snapshot_upload = await _upload(
        market_data, payload.snapshot_upload_id, user.id, UploadKind.BOOK_SNAPSHOTS
    )
    event_upload = await _upload(
        market_data, payload.event_upload_id, user.id, UploadKind.BOOK_EVENTS
    )
    if await instruments.get(payload.instrument_id) is None:
        raise NotFound("Instrument")

    preview = microstructure.parse(
        ImportParams(
            instrument_id=payload.instrument_id,
            name="preview",
            snapshot_columns=_level_columns(payload.snapshot_columns),
            event_mapping=(
                ColumnMapping(mapping=dict(payload.event_mapping))
                if payload.event_mapping
                else None
            ),
        ),
        await market_data.read_upload(snapshot_upload) if snapshot_upload else None,
        await market_data.read_upload(event_upload) if event_upload else None,
        limit=payload.limit or settings.upload_preview_rows,
    )
    return DatasetPreviewOut(**preview.to_dict())


@router.post("/datasets", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def import_dataset(
    payload: DatasetImportRequest,
    user: CurrentUser,
    instruments: InstrumentServiceDep,
    market_data: MarketDataServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Import depth snapshots, an event tape, or both, as one dataset.

    A job because the whole file is parsed, normalised and rewritten to parquet;
    an L2 session is millions of rows and does not belong inside a request.
    """
    if await instruments.get(payload.instrument_id) is None:
        raise NotFound("Instrument")
    snapshot_upload = await _upload(
        market_data, payload.snapshot_upload_id, user.id, UploadKind.BOOK_SNAPSHOTS
    )
    event_upload = await _upload(
        market_data, payload.event_upload_id, user.id, UploadKind.BOOK_EVENTS
    )

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.IMPORT_BOOK_DATA,
        input_reference={
            "instrument_id": str(payload.instrument_id),
            "name": payload.name,
            "source": payload.source,
            "snapshot_upload_id": str(snapshot_upload.id) if snapshot_upload else None,
            "event_upload_id": str(event_upload.id) if event_upload else None,
            "snapshot_columns": (
                payload.snapshot_columns.to_payload() if payload.snapshot_columns else None
            ),
            "event_mapping": payload.event_mapping,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.IMPORT_BOOK_DATA),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


# ------------------------------------------------------------------ datasets
@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets(
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    instrument_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DatasetOut]:
    rows = await microstructure.list_datasets(
        user.id, instrument_id=instrument_id, limit=limit, offset=offset
    )
    return [DatasetOut.model_validate(row) for row in rows]


@router.get("/datasets/{dataset_id}", response_model=Envelope)
async def get_dataset(
    dataset_id: uuid.UUID, user: CurrentUser, microstructure: MicrostructureServiceDep
) -> Envelope:
    """One dataset, with the full availability report it was assessed against.

    The status is ``PARTIAL`` whenever any capability was refused, because a
    dataset that cannot support half of what this phase offers is not a complete
    answer to "what can I do with this?" even though the import succeeded.
    """
    dataset = await _dataset(microstructure, dataset_id, user.id)
    availability = dict(dataset.availability or {})
    refused = availability.get("refused", [])

    return Envelope(
        status="OK" if not refused else "PARTIAL",
        results={
            "dataset_id": str(dataset.id),
            "instrument_id": str(dataset.instrument_id),
            "name": dataset.name,
            "kind": dataset.kind,
            "source": dataset.source,
            "rows": {
                "snapshots": {
                    "input": dataset.snapshot_rows_in,
                    "kept": dataset.snapshot_rows_kept,
                    "rejected": dataset.snapshot_rows_rejected,
                },
                "events": {
                    "input": dataset.event_rows_in,
                    "kept": dataset.event_rows_kept,
                    "rejected": dataset.event_rows_rejected,
                },
                "rejection_counts": dataset.rejection_counts,
            },
            "window": {
                "start": (dataset.first_timestamp.isoformat() if dataset.first_timestamp else None),
                "end": dataset.last_timestamp.isoformat() if dataset.last_timestamp else None,
                "span_seconds": dataset.span_seconds,
            },
            "max_depth_levels": dataset.max_depth_levels,
            "availability": availability,
        },
        warnings=[],
        provenance=ProvenanceOut(**dataset.provenance),
    )


@router.get("/datasets/{dataset_id}/capabilities", response_model=list[CapabilityOut])
async def dataset_capabilities(
    dataset_id: uuid.UUID, user: CurrentUser, microstructure: MicrostructureServiceDep
) -> list[CapabilityOut]:
    """Per-capability verdicts for this dataset, each with its evidence."""
    dataset = await _dataset(microstructure, dataset_id, user.id)
    entries = (dataset.availability or {}).get("capabilities", [])
    return [CapabilityOut(**entry) for entry in entries]


@router.get("/datasets/{dataset_id}/rejections", response_model=RejectionsOut)
async def dataset_rejections(
    dataset_id: uuid.UUID, user: CurrentUser, microstructure: MicrostructureServiceDep
) -> RejectionsOut:
    """Every row that did not make it, by source row number and reason.

    The complete list, not a sample: the counts on the dataset row account for
    every rejection, and this is what makes each individual one findable in the
    user's own file.
    """
    dataset = await _dataset(microstructure, dataset_id, user.id)
    payload = await microstructure.rejections(dataset)
    return RejectionsOut(
        dataset_id=dataset.id,
        snapshot_rejections=payload.get("snapshot_rejections", []),
        event_rejections=payload.get("event_rejections", []),
        counts=dict(dataset.rejection_counts or {}),
    )


# ----------------------------------------------------------------- analytics
@router.post(
    "/datasets/{dataset_id}/analyze",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyse_dataset(
    dataset_id: uuid.UUID,
    payload: AnalyseBookRequest,
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Measure every snapshot in the dataset and summarise what was measurable.

    The gate is checked here rather than only inside the job, so a dataset that
    cannot support the analytics is a stated refusal at the boundary instead of
    a failed job the user has to go and read.
    """
    dataset = await _dataset(microstructure, dataset_id, user.id)
    try:
        microstructure.require_capability(dataset, MicrostructureCapability.TOP_OF_BOOK)
    except CapabilityRefused as exc:
        raise _refusal(exc) from exc

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.ANALYZE_MICROSTRUCTURE,
        input_reference={
            "dataset_id": str(dataset.id),
            "levels": payload.levels,
            "weighted_decay": payload.weighted_decay,
            "trade_sizes": payload.trade_sizes,
            "preview_points": payload.preview_points,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.ANALYZE_MICROSTRUCTURE),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/reports", response_model=list[BookReportOut])
async def list_reports(
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    dataset_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[BookReportOut]:
    rows = await microstructure.repository.list_reports(
        user.id, dataset_id=dataset_id, limit=limit, offset=offset
    )
    return [BookReportOut.model_validate(row) for row in rows]


@router.get("/reports/{report_id}", response_model=Envelope)
async def get_report(
    report_id: uuid.UUID, user: CurrentUser, microstructure: MicrostructureServiceDep
) -> Envelope:
    """One analytics run: every measure with its observation count, the
    snapshots that had no such measurement and why, and the series behind it."""
    row = await microstructure.repository.get_report(report_id, user.id)
    if row is None:
        raise NotFound("Book analytics report")

    unmeasured = [item for item in row.measures if item.get("observations", 0) == 0]
    return Envelope(
        status="OK" if not unmeasured else "PARTIAL",
        results={
            "report_id": str(row.id),
            "dataset_id": str(row.dataset_id),
            "instrument_id": str(row.instrument_id),
            "levels": row.levels,
            "weighted_decay": row.weighted_decay,
            "snapshots_analysed": row.snapshots_analysed,
            "window": {
                "start": row.window_start.isoformat() if row.window_start else None,
                "end": row.window_end.isoformat() if row.window_end else None,
            },
            "crossed_snapshots": row.crossed_snapshots,
            "locked_snapshots": row.locked_snapshots,
            "measures": row.measures,
            "trade_costs": row.trade_costs,
            "series": row.preview_series,
            "series_note": (
                "A subsample of the full per-snapshot series, spaced evenly "
                "across the window so the chart is not a picture of the opening."
            ),
        },
        warnings=row.warnings,
        provenance=ProvenanceOut(**row.provenance),
    )


# ----------------------------------------------------------------- intensity
@router.post(
    "/datasets/{dataset_id}/intensity",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fit_intensity(
    dataset_id: uuid.UUID,
    payload: FitIntensityRequest,
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Fit a constant rate and a self-exciting model, and score both out of sample.

    The self-exciting fit is reported **only** if it assigns more likelihood per
    event to a held-out window than the constant rate does, by more than the
    sampling error of that comparison. Otherwise the result is the constant rate
    and a statement of by how much the richer model failed to earn its
    parameters. There is no setting that skips the comparison.
    """
    dataset = await _dataset(microstructure, dataset_id, user.id)
    # Both capabilities are checked here rather than left to the job: a scope
    # the dataset cannot support should be a stated refusal at the boundary,
    # not a failed job the user has to go and read.
    required = [MicrostructureCapability.EVENT_INTENSITY]
    if BookEventType.CANCEL in payload.event_types:
        required.append(MicrostructureCapability.CANCELLATION_INTENSITY)
    try:
        for capability in required:
            microstructure.require_capability(dataset, capability)
    except CapabilityRefused as exc:
        raise _refusal(exc) from exc

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.FIT_INTENSITY,
        input_reference={"dataset_id": str(dataset.id), **payload.to_payload()},
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.FIT_INTENSITY),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/intensity", response_model=list[IntensityOut])
async def list_intensity_models(
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    dataset_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[IntensityOut]:
    rows = await microstructure.repository.list_intensities(
        user.id, dataset_id=dataset_id, limit=limit, offset=offset
    )
    return [IntensityOut.model_validate(row) for row in rows]


@router.get("/intensity/{model_id}", response_model=Envelope)
async def get_intensity_model(
    model_id: uuid.UUID, user: CurrentUser, microstructure: MicrostructureServiceDep
) -> Envelope:
    """One comparison, with both models and the verdict between them."""
    row = await microstructure.repository.get_intensity(model_id, user.id)
    if row is None:
        raise NotFound("Intensity model")

    return Envelope(
        status="OK" if row.hawkes_is_adopted else "PARTIAL",
        results={
            "intensity_model_id": str(row.id),
            "dataset_id": str(row.dataset_id),
            "scope": row.scope,
            "events_selected": row.events_selected,
            "adopted_model": row.adopted_model,
            "adopted_rate_per_second": row.adopted_rate,
            "hawkes_is_adopted": row.hawkes_is_adopted,
            "verdict_reason": row.verdict_reason,
            **row.comparison,
        },
        warnings=row.warnings,
        provenance=ProvenanceOut(**row.provenance),
    )


# --------------------------------------------------------------------- queue
@router.post("/datasets/{dataset_id}/queue", response_model=Envelope)
async def estimate_queue(
    dataset_id: uuid.UUID,
    payload: QueueEstimateRequest,
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    session: SessionDep,
) -> Envelope:
    """Bracket the outlook for a hypothetical order resting at a price level.

    Answered inline rather than as a job: it reads one level from one snapshot
    and counts the events at that price, and a user moving a price around
    should not have to poll for each answer.

    **The result is a bracket.** Its two ends differ only in whether
    cancellations at the level are assumed to remove size ahead of the order or
    behind it, which public data does not say. There is no single fill
    probability in the response, and there is no field that could hold one.
    """
    dataset = await _dataset(microstructure, dataset_id, user.id)
    try:
        result, _row_id = await microstructure.estimate_queue(
            user.id,
            dataset,
            QueueParams(
                side=payload.side,
                horizon_seconds=payload.horizon_seconds,
                price=payload.price,
                quantity_ahead=payload.quantity_ahead,
                as_of=payload.as_of,
            ),
        )
    except CapabilityRefused as exc:
        raise _refusal(exc) from exc
    await session.commit()

    body = result.to_dict()
    return Envelope(
        status=body["status"],
        results=body["results"],
        warnings=body["warnings"],
        provenance=ProvenanceOut(**body["provenance"]),
    )


@router.get("/queue-estimates", response_model=list[QueueEstimateOut])
async def list_queue_estimates(
    user: CurrentUser,
    microstructure: MicrostructureServiceDep,
    dataset_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[QueueEstimateOut]:
    rows = await microstructure.repository.list_queue_estimates(
        user.id, dataset_id=dataset_id, limit=limit, offset=offset
    )
    return [QueueEstimateOut.model_validate(row) for row in rows]
