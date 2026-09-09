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
    #: Broker credential lifecycle. Recorded because a stored broker token is
    #: the most sensitive thing the platform holds: who connected what, when it
    #: was renewed, and when it stopped working must all be reconstructable.
    BROKER_AUTHORIZATION_STARTED = "BROKER_AUTHORIZATION_STARTED"
    BROKER_AUTHORIZATION_FAILED = "BROKER_AUTHORIZATION_FAILED"
    BROKER_CONNECTION_AUTHORIZED = "BROKER_CONNECTION_AUTHORIZED"
    BROKER_CONNECTION_REFRESHED = "BROKER_CONNECTION_REFRESHED"
    BROKER_CONNECTION_REVOKED = "BROKER_CONNECTION_REVOKED"
    BROKER_CREDENTIAL_REJECTED = "BROKER_CREDENTIAL_REJECTED"
    #: A unified order analysis was run. Recorded because it is the one endpoint
    #: that reads a whole portfolio and an unplaced order together.
    ORDER_ANALYSED = "ORDER_ANALYSED"


@dataclass(frozen=True, slots=True)
class User:
    id: uuid.UUID
    email: str
    is_active: bool
    created_at: datetime
