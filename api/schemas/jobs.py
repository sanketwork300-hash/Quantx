from __future__ import annotations

import uuid
from datetime import datetime

from api.schemas.common import APIModel


class JobOut(APIModel):
    job_id: uuid.UUID
    job_type: str
    status: str
    progress: float
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict | None = None


class JobListOut(APIModel):
    items: list[JobOut]


class JobResultOut(APIModel):
    job_id: uuid.UUID
    status: str
    result: dict | None = None
    error: dict | None = None
