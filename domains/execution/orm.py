"""Execution persistence.

`executions` is **append-only**. There is no update path in the repository and
no correction column: a corrected fill is a new row that supersedes an earlier
one, and both stay. A trade log that can be quietly rewritten cannot support a
cost analysis anyone should act on.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import DecimalType, JSONDict, UTCDateTime


class ExecutionORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "executions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Always positive. Direction is carried by ``side``, so a sign convention
    #: cannot drift between the importer and the analytics.
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    execution_price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    exchange_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    receive_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)

    order_id: Mapped[str | None] = mapped_column(String(128))
    #: The parent named by the file. Null means the grouping had to be inferred,
    #: which every analysis of it reports.
    parent_order_key: Mapped[str | None] = mapped_column(String(128))
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    order_quantity: Mapped[Decimal | None] = mapped_column(DecimalType())
    submit_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decision_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)

    broker: Mapped[str | None] = mapped_column(String(64))
    venue: Mapped[str | None] = mapped_column(String(32))
    fees: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="CSV_IMPORT")
    execution_metadata: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_executions_user_time", "user_id", "exchange_timestamp"),
        Index("ix_executions_instrument_time", "instrument_id", "exchange_timestamp"),
        Index("ix_executions_parent", "user_id", "parent_order_key"),
        CheckConstraint("quantity > 0", name="ck_execution_quantity_positive"),
        CheckConstraint("execution_price >= 0", name="ck_execution_price_non_negative"),
        CheckConstraint("fees >= 0", name="ck_execution_fees_non_negative"),
        # A fill cannot precede its own submission. The pair is the basis of
        # every arrival benchmark, so an impossible ordering is refused at the
        # boundary rather than producing a negative delay downstream.
        CheckConstraint(
            "submit_timestamp IS NULL OR submit_timestamp <= exchange_timestamp",
            name="ck_execution_submit_not_after_fill",
        ),
    )


class ExecutionReportORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One parent order's transaction cost analysis, as it was reported."""

    __tablename__ = "execution_reports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    parent_order_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Whether the file named the parent or the platform inferred it. Carried on
    #: the row because it changes what every number below means.
    grouping_method: Mapped[str] = mapped_column(String(24), nullable=False)
    grouping_is_inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    canonical_key: Mapped[str | None] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=1)

    fills: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    order_quantity: Mapped[Decimal | None] = mapped_column(DecimalType())
    average_price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    fees: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)

    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    primary_benchmark: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Null when no benchmark was available, which is a stateable outcome and
    #: not the same as a shortfall of zero.
    primary_benchmark_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    shortfall_currency: Mapped[float | None] = mapped_column(Float)
    shortfall_bps: Mapped[float | None] = mapped_column(Float)
    shortfall_percent: Mapped[float | None] = mapped_column(Float)

    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_span_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    coverage_is_sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    benchmarks: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    shortfalls: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    unavailable_shortfalls: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    decomposition: Mapped[dict | None] = mapped_column(JSONDict)
    market_window: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_execution_reports_user", "user_id", "created_at"),
        Index("ix_execution_reports_parent", "user_id", "parent_order_key"),
        CheckConstraint("filled_quantity > 0", name="ck_report_filled_quantity_positive"),
        CheckConstraint("window_end >= window_start", name="ck_report_window_ordered"),
        # A shortfall without the benchmark it was measured against is a number
        # with no meaning, so the pair travels together or neither does.
        CheckConstraint(
            "(primary_benchmark_price IS NULL) = (shortfall_currency IS NULL)",
            name="ck_report_shortfall_needs_benchmark",
        ),
    )


class ExecutionSimulationORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One counterfactual run: a schedule priced against a path it never touched.

    The `counterfactual` column exists only to be constrained. It is `True` on
    every row and a CHECK forbids anything else, so a simulated result cannot be
    stored without the label that says it never happened — not by a future
    refactor, not by a bulk insert, not by hand.
    """

    __tablename__ = "execution_simulations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: Groups the rows produced by one strategy comparison.
    comparison_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    counterfactual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_model: Mapped[str] = mapped_column(String(64), nullable=False)
    #: False when the impact coefficients were left at the identity, which every
    #: number derived from them then depends on.
    impact_is_calibrated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    ordered_quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    completion_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    average_price: Mapped[Decimal | None] = mapped_column(DecimalType())

    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_price_age_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    modelled_impact_cost: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    modelled_spread_cost: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)

    primary_benchmark: Mapped[str | None] = mapped_column(String(24))
    shortfall_currency: Mapped[float | None] = mapped_column(Float)
    shortfall_bps: Mapped[float | None] = mapped_column(Float)

    schedule: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    context: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    fills: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    unfilled: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    benchmarks: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_simulations_user", "user_id", "created_at"),
        Index("ix_simulations_comparison", "comparison_id"),
        CheckConstraint("counterfactual", name="ck_simulation_is_always_counterfactual"),
        CheckConstraint("ordered_quantity > 0", name="ck_simulation_ordered_quantity_positive"),
        CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= ordered_quantity",
            name="ck_simulation_filled_within_ordered",
        ),
        CheckConstraint(
            "completion_rate >= 0 AND completion_rate <= 1",
            name="ck_simulation_completion_rate_is_a_rate",
        ),
        CheckConstraint("window_end >= window_start", name="ck_simulation_window_ordered"),
        # Same pairing rule as the TCA report: a shortfall without the benchmark
        # it was measured against is a number with no meaning.
        CheckConstraint(
            "(primary_benchmark IS NULL) = (shortfall_currency IS NULL)",
            name="ck_simulation_shortfall_needs_benchmark",
        ),
    )
