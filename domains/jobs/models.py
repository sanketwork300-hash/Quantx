from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


#: Legal transitions. Enforced in the service so a race between a worker and a
#: cancellation cannot resurrect a terminal job.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class JobType(StrEnum):
    INGEST_OPTION_CHAIN = "INGEST_OPTION_CHAIN"
    ANALYZE_OPTION_CHAIN = "ANALYZE_OPTION_CHAIN"
    CALIBRATE_SURFACE = "CALIBRATE_SURFACE"
    SCAN_ANOMALIES = "SCAN_ANOMALIES"
    # Later phases register their types here; the enum is the contract between
    # the API, the worker and the frontend.


@dataclass(frozen=True, slots=True)
class Job:
    id: uuid.UUID
    user_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    progress: float
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input_reference: dict = field(default_factory=dict)
    result_reference: dict | None = None
    error: dict | None = None
