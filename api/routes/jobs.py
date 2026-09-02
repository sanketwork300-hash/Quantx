from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from api.dependencies.core import CurrentUser, JobServiceDep, SessionDep
from api.errors import Conflict, NotFound
from api.schemas.jobs import JobListOut, JobOut, JobResultOut
from domains.jobs.service import IllegalJobTransition

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _to_out(job) -> JobOut:
    return JobOut(
        job_id=job.id,
        job_type=str(job.job_type),
        status=str(job.status),
        progress=job.progress,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
    )


@router.get("", response_model=JobListOut)
async def list_jobs(
    user: CurrentUser,
    jobs: JobServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> JobListOut:
    items = await jobs.list_for_user(user.id, limit=limit, offset=offset)
    return JobListOut(items=[_to_out(job) for job in items])


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, user: CurrentUser, jobs: JobServiceDep) -> JobOut:
    job = await jobs.get(job_id, user.id)
    if job is None:
        raise NotFound("Job")
    return _to_out(job)


@router.get("/{job_id}/result", response_model=JobResultOut)
async def get_job_result(job_id: uuid.UUID, user: CurrentUser, jobs: JobServiceDep) -> JobResultOut:
    job = await jobs.get(job_id, user.id)
    if job is None:
        raise NotFound("Job")
    return JobResultOut(
        job_id=job.id,
        status=str(job.status),
        result=job.result_reference,
        error=job.error,
    )


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: uuid.UUID, user: CurrentUser, jobs: JobServiceDep, session: SessionDep
) -> JobOut:
    if await jobs.get(job_id, user.id) is None:
        raise NotFound("Job")
    try:
        job = await jobs.cancel(job_id, user.id)
    except IllegalJobTransition as exc:
        raise Conflict(
            "JOB_ALREADY_TERMINAL",
            f"A {exc.current} job cannot be cancelled.",
        ) from exc
    await session.commit()
    return _to_out(job)
