"""Model registry.

Analytical models are versioned exactly like ML models. Every persisted result
references the model version that produced it, so a change in output can always
be attributed either to the market or to us.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import JSONDict, UTCDateTime


class ModelVersionORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_versions"

    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    training_period_start: Mapped[date | None] = mapped_column(Date)
    training_period_end: Mapped[date | None] = mapped_column(Date)
    calibration_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    code_commit: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("model_name", "version", name="uq_model_name_version"),)
