"""Model registry, and the stored unified order analysis.

Analytical models are versioned exactly like ML models. Every persisted result
references the model version that produced it, so a change in output can always
be attributed either to the market or to us.

The Phase 11 table encodes two of that phase's guarantees in the schema, so
that what the code refuses to produce the database also refuses to store:

``ck_order_analysis_status_matches_its_branches``
    A row cannot claim ``OK`` while a branch failed, or ``PARTIAL`` while all
    five succeeded. The headline status is then always readable as a count.

``ck_order_analysis_names_its_market_state``
    Any row that produced a result at all names the one snapshot every branch
    read. A result whose branches could have come from different moments has
    nowhere to be stored.

And a guarantee by omission: there is no column here for an action, a signal, a
rating or a score. Adding one would be a migration, which is exactly the amount
of deliberation such a field should take.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import DecimalType, JSONDict, UTCDateTime


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


class OrderAnalysisORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One proposed order, analysed across five engines over one snapshot."""

    __tablename__ = "order_analyses"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalType())

    as_of_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: The one snapshot every branch below read. The whole phase is this column.
    market_state_id: Mapped[str | None] = mapped_column(String(64))
    base_currency: Mapped[str | None] = mapped_column(String(3))

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    branches_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    branches_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    branch_status: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    results: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    # The quantity is deliberately *not* range-checked here: it is stored as
    # text on dialects without a native numeric type, where a `> 0` comparison
    # would be a string comparison that passes for everything. A constraint that
    # means two different things on two dialects is worse than none, so the
    # magnitude is enforced in the domain, where it means one thing.
    __table_args__ = (
        Index("ix_order_analyses_portfolio", "portfolio_id", "created_at"),
        Index("ix_order_analyses_user", "user_id", "created_at"),
        CheckConstraint(
            "(status = 'OK' AND branches_failed = 0 AND branches_ok > 0) "
            "OR (status = 'PARTIAL' AND branches_failed > 0 AND branches_ok > 0) "
            "OR (status = 'FAILED' AND branches_ok = 0)",
            name="ck_order_analysis_status_matches_its_branches",
        ),
        CheckConstraint(
            "status = 'FAILED' OR market_state_id IS NOT NULL",
            name="ck_order_analysis_names_its_market_state",
        ),
    )
