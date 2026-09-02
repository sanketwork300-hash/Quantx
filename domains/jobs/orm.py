from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import JSONDict, UTCDateTime


class JobORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "jobs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    started_at: Mapped[object | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[object | None] = mapped_column(UTCDateTime)
    #: Inputs are stored, not the payload: a large input lands in the object
    #: store and this holds the pointer.
    input_reference: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    result_reference: Mapped[dict | None] = mapped_column(JSONDict)
    error: Mapped[dict | None] = mapped_column(JSONDict)

    __table_args__ = (
        Index("ix_jobs_user_status", "user_id", "status", "created_at"),
        CheckConstraint("progress >= 0.0 AND progress <= 1.0", name="ck_job_progress_range"),
    )
