"""The dataset registry.

The data lives in the object store; what lives here is the record of it. That
split is the whole point of the phase: a tick tape does not belong in PostgreSQL,
but "which datasets exist, what do they cover, how good are they and where are
their files" is user-activity sized, transactional, and is what makes a dataset
findable without listing a bucket.

Row conservation is a database constraint rather than a docstring, as it is for
option-chain snapshots. A registry row that claims more rows than it accounted
for is a bug, and the database is the last place that can still say so.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import JSONDict, UTCDateTime


class WarehouseDatasetORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One registered historical dataset."""

    __tablename__ = "warehouse_datasets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    source: Mapped[str | None] = mapped_column(String(128))
    dataset_digest: Mapped[str | None] = mapped_column(String(64))
    #: Declared by whoever registered the dataset, never inferred. The platform
    #: holds no corporate-action feed and will not adjust a series.
    corporate_action_treatment: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Whether the dataset is meant to stay current. Freshness is scored only
    #: for these; a historical archive is not stale, it is history.
    continuous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    rows_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rows_excluded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rows_flagged: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    instrument_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partition_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    first_observation: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_observation: Mapped[datetime | None] = mapped_column(UTCDateTime)

    completeness_score: Mapped[float | None] = mapped_column(Float)
    consistency_score: Mapped[float | None] = mapped_column(Float)
    outlier_score: Mapped[float | None] = mapped_column(Float)
    source_score: Mapped[float | None] = mapped_column(Float)
    #: Null when the dataset is not continuous — not measurable, which is a
    #: different statement from zero.
    freshness_score: Mapped[float | None] = mapped_column(Float)
    overall_score: Mapped[float | None] = mapped_column(Float)

    quality_evidence: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    #: Counts by code plus a capped sample. The complete finding list is
    #: unbounded and lives in the object store beside the partitions.
    validation_summary: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    findings_key: Mapped[str | None] = mapped_column(String(512))
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_warehouse_datasets_user", "user_id", "created_at"),
        Index("ix_warehouse_datasets_scope", "layer", "kind", "exchange"),
        CheckConstraint(
            "rows_in = rows_written + rows_excluded + rows_rejected",
            name="ck_warehouse_dataset_row_conservation",
        ),
    )


class WarehousePartitionORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One written partition: one instrument, one day, one file."""

    __tablename__ = "warehouse_partitions"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("warehouse_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rows_flagged: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_observation: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_observation: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        # A partition is a whole-file replacement, so one row per file. Writing
        # the same day twice replaces rather than accumulates, which is what
        # makes a re-ingestion idempotent.
        UniqueConstraint("dataset_id", "object_key", name="uq_warehouse_partition_key"),
        Index("ix_warehouse_partitions_dataset", "dataset_id"),
        Index("ix_warehouse_partitions_instrument_day", "instrument_id", "day"),
    )
