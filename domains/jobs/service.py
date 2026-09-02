"""Job lifecycle.

The job system ships in Phase 0, before any long calculation exists, so that
every later phase has somewhere to put work that must not block a request. The
API never runs a Monte Carlo; it creates a job.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.jobs.models import ALLOWED_TRANSITIONS, Job, JobStatus, JobType
from domains.jobs.orm import JobORM
from quant.numerical.tolerances import clamp


class JobError(Exception):
    pass


class IllegalJobTransition(JobError):
    def __init__(self, current: JobStatus, requested: JobStatus) -> None:
        super().__init__(f"cannot move a job from {current} to {requested}")
        self.current = current
        self.requested = requested


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: uuid.UUID, job_type: JobType, input_reference: dict) -> Job:
        row = JobORM(
            user_id=user_id,
            job_type=str(job_type),
            status=str(JobStatus.QUEUED),
            progress=0.0,
            input_reference=input_reference,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_domain(row)

    async def get(self, job_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Job | None:
        row = await self._session.get(JobORM, job_id)
        if row is None:
            return None
        # Ownership is checked here rather than in the route so that no caller
        # can reach a job by id alone.
        if user_id is not None and row.user_id != user_id:
            return None
        return _to_domain(row)

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[Job]:
        stmt = (
            select(JobORM)
            .where(JobORM.user_id == user_id)
            .order_by(JobORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def mark_running(self, job_id: uuid.UUID) -> Job:
        return await self._transition(job_id, JobStatus.RUNNING, started_at=datetime.now(UTC))

    async def set_progress(self, job_id: uuid.UUID, progress: float) -> None:
        row = await self._require(job_id)
        if JobStatus(row.status) is not JobStatus.RUNNING:
            return
        row.progress = clamp(progress, 0.0, 1.0)
        await self._session.flush()

    async def complete(self, job_id: uuid.UUID, result_reference: dict) -> Job:
        return await self._transition(
            job_id,
            JobStatus.COMPLETED,
            completed_at=datetime.now(UTC),
            progress=1.0,
            result_reference=result_reference,
        )

    async def fail(self, job_id: uuid.UUID, error: dict) -> Job:
        return await self._transition(
            job_id, JobStatus.FAILED, completed_at=datetime.now(UTC), error=error
        )

    async def cancel(self, job_id: uuid.UUID, user_id: uuid.UUID) -> Job:
        row = await self._require(job_id)
        if row.user_id != user_id:
            raise JobError("job not found")
        return await self._transition(job_id, JobStatus.CANCELLED, completed_at=datetime.now(UTC))

    # ------------------------------------------------------------- internals
    async def _require(self, job_id: uuid.UUID) -> JobORM:
        row = await self._session.get(JobORM, job_id)
        if row is None:
            raise JobError(f"job {job_id} not found")
        return row

    async def _transition(self, job_id: uuid.UUID, target: JobStatus, **updates) -> Job:
        row = await self._require(job_id)
        current = JobStatus(row.status)
        if target not in ALLOWED_TRANSITIONS[current]:
            raise IllegalJobTransition(current, target)
        row.status = str(target)
        for key, value in updates.items():
            setattr(row, key, value)
        await self._session.flush()
        return _to_domain(row)


def _to_domain(row: JobORM) -> Job:
    return Job(
        id=row.id,
        user_id=row.user_id,
        job_type=JobType(row.job_type),
        status=JobStatus(row.status),
        progress=row.progress,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        input_reference=dict(row.input_reference or {}),
        result_reference=row.result_reference,
        error=row.error,
    )
