from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AuditAction(StrEnum):
    USER_REGISTERED = "USER_REGISTERED"
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    UPLOAD_RECEIVED = "UPLOAD_RECEIVED"
    UPLOAD_INGESTED = "UPLOAD_INGESTED"
    INSTRUMENT_CREATED = "INSTRUMENT_CREATED"
    JOB_SUBMITTED = "JOB_SUBMITTED"
    JOB_CANCELLED = "JOB_CANCELLED"


@dataclass(frozen=True, slots=True)
class User:
    id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime
