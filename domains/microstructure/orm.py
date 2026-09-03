"""Microstructure persistence.

**The bulk data is not here.** Depth snapshots and event tapes live in the
object store as parquet, and these tables hold what a relational store is good
for: identity, ownership, provenance, and the small structured results the
platform reasons about. One session of L2 for one liquid contract would put
millions of rows in PostgreSQL for an access pattern — read three columns over
a time range — that a columnar file answers far better.

Three constraints in this module encode the gate the phase exists for, so that
what the code refuses to do the schema also refuses to store:

``ck_intensity_hawkes_needs_a_held_out_win``
    A row cannot claim the self-exciting model unless its held-out test
    statistic actually cleared the critical value.

``ck_intensity_adopted_model_matches_the_verdict``
    And the adopted model name has to agree with that flag, so the field a
    reader looks at cannot drift from the field the gate set.

``ck_queue_estimate_is_a_bracket``
    A queue outlook is stored as two ends, and the optimistic end can never be
    worse than the pessimistic one. A single number has nowhere to go.
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


class MicrostructureDatasetORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One imported L2 dataset: where the parquet is, and what it can support."""

    __tablename__ = "microstructure_datasets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)

    snapshot_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    event_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    #: Object-store keys. Null when that half of the dataset was not supplied.
    snapshot_key: Mapped[str | None] = mapped_column(String(512))
    event_key: Mapped[str | None] = mapped_column(String(512))
    #: The complete per-row rejection list, which is unbounded and so does not
    #: belong in a column. The counts below are complete without it; this is
    #: what makes every individual rejected row retrievable by row number.
    rejection_key: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_digest: Mapped[str | None] = mapped_column(String(64))

    snapshot_rows_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_rows_kept: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_rows_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_rows_kept: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Reason -> count, over every rejected row. Complete by construction.
    rejection_counts: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    first_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    span_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_depth_levels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The full availability report: profile, per-capability verdicts, evidence
    #: and the thresholds the verdicts were taken against.
    availability: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    #: Denormalised for filtering, so "which datasets support a queue model?"
    #: does not need a JSON scan.
    available_capabilities: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_microstructure_datasets_user", "user_id", "created_at"),
        Index("ix_microstructure_datasets_instrument", "instrument_id", "first_timestamp"),
        # The conservation law, in the schema. A row that went missing without a
        # reason cannot be recorded as having been imported.
        CheckConstraint(
            "snapshot_rows_in = snapshot_rows_kept + snapshot_rows_rejected",
            name="ck_dataset_conserves_snapshot_rows",
        ),
        CheckConstraint(
            "event_rows_in = event_rows_kept + event_rows_rejected",
            name="ck_dataset_conserves_event_rows",
        ),
        CheckConstraint(
            "snapshot_key IS NOT NULL OR event_key IS NOT NULL",
            name="ck_dataset_holds_something",
        ),
        CheckConstraint("span_seconds >= 0", name="ck_dataset_span_non_negative"),
    )


class BookAnalyticsReportORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One run of the snapshot analytics over one dataset."""

    __tablename__ = "book_analytics_reports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("microstructure_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )

    levels: Mapped[int] = mapped_column(Integer, nullable=False)
    weighted_decay: Mapped[float] = mapped_column(Float, nullable=False)
    snapshots_analysed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    window_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    crossed_snapshots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_snapshots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Headline values, denormalised so a list view needs no JSON parsing. Each
    #: is a median rather than a mean: a session of books is not normal, and a
    #: handful of auction instants own the mean.
    median_spread: Mapped[float | None] = mapped_column(Float)
    median_relative_spread: Mapped[float | None] = mapped_column(Float)
    median_imbalance: Mapped[float | None] = mapped_column(Float)
    median_microprice_tilt: Mapped[float | None] = mapped_column(Float)

    #: Per-measure summaries, each with its observation count and the reasons
    #: the snapshots that had no such measurement did not.
    measures: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    trade_costs: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    #: Downsampled series for the UI. The full per-snapshot series is parquet in
    #: the object store, at `series_key`.
    preview_series: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    series_key: Mapped[str | None] = mapped_column(String(512))
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_book_reports_user", "user_id", "created_at"),
        Index("ix_book_reports_dataset", "dataset_id", "created_at"),
        CheckConstraint("levels >= 1", name="ck_book_report_levels_positive"),
        CheckConstraint("weighted_decay >= 0", name="ck_book_report_decay_non_negative"),
        CheckConstraint("snapshots_analysed >= 0", name="ck_book_report_snapshots_non_negative"),
    )


class IntensityModelORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One Poisson-versus-Hawkes comparison, and which model it adopted.

    Both models are stored whatever the verdict, because "the self-exciting fit
    was tried and did not earn its parameters here" is a result worth keeping —
    it is the evidence that the gate ran.
    """

    __tablename__ = "intensity_models"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("microstructure_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )

    #: ``TRADE+CANCEL/BID/24000.00`` — the process being modelled, in one field,
    #: because a rate without its scope is a rate for something unspecified.
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    event_types: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    side: Mapped[str | None] = mapped_column(String(8))
    price: Mapped[Decimal | None] = mapped_column(DecimalType())
    events_selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    split_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    train_fraction: Mapped[float] = mapped_column(Float, nullable=False)

    poisson_rate: Mapped[float] = mapped_column(Float, nullable=False)
    poisson_train_log_likelihood: Mapped[float] = mapped_column(Float, nullable=False)
    poisson_held_out_log_likelihood: Mapped[float] = mapped_column(Float, nullable=False)

    hawkes_mu: Mapped[float | None] = mapped_column(Float)
    hawkes_alpha: Mapped[float | None] = mapped_column(Float)
    hawkes_beta: Mapped[float | None] = mapped_column(Float)
    hawkes_branching_ratio: Mapped[float | None] = mapped_column(Float)
    hawkes_train_log_likelihood: Mapped[float | None] = mapped_column(Float)
    hawkes_held_out_log_likelihood: Mapped[float | None] = mapped_column(Float)
    hawkes_converged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    held_out_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mean_gain_per_event: Mapped[float | None] = mapped_column(Float)
    test_statistic: Mapped[float | None] = mapped_column(Float)
    critical_value: Mapped[float] = mapped_column(Float, nullable=False)
    hawkes_is_adopted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adopted_model: Mapped[str] = mapped_column(String(32), nullable=False, default="POISSON")
    adopted_rate: Mapped[float] = mapped_column(Float, nullable=False)
    verdict_reason: Mapped[str] = mapped_column(String(1000), nullable=False)

    comparison: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_intensity_models_user", "user_id", "created_at"),
        Index("ix_intensity_models_dataset", "dataset_id", "created_at"),
        CheckConstraint("window_end > window_start", name="ck_intensity_window_ordered"),
        CheckConstraint(
            "split_timestamp > window_start AND split_timestamp < window_end",
            name="ck_intensity_split_inside_window",
        ),
        CheckConstraint("poisson_rate >= 0", name="ck_intensity_rate_non_negative"),
        # The gate, in the schema. A stored row cannot claim the richer model
        # unless the held-out test it had to pass actually says so.
        CheckConstraint(
            "NOT hawkes_is_adopted OR ("
            "  test_statistic IS NOT NULL"
            "  AND test_statistic > critical_value"
            "  AND hawkes_converged"
            "  AND hawkes_branching_ratio IS NOT NULL"
            "  AND hawkes_branching_ratio < 1"
            ")",
            name="ck_intensity_hawkes_needs_a_held_out_win",
        ),
        CheckConstraint(
            "(adopted_model = 'HAWKES_EXPONENTIAL') = hawkes_is_adopted",
            name="ck_intensity_adopted_model_matches_the_verdict",
        ),
    )


class QueueEstimateORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One bracketed queue outlook.

    There is deliberately no single ``fill_probability`` column. The two ends
    are the answer; a column able to hold one number would be filled in by
    something eventually, and then read as a measurement.
    """

    __tablename__ = "queue_estimates"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("microstructure_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    snapshot_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    quantity_ahead: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    level_quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    horizon_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    observation_window_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    trades_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancels_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: CANCELS_BEHIND: only trades advance the order.
    pessimistic_fill_probability: Mapped[float] = mapped_column(Float, nullable=False)
    pessimistic_wait_seconds: Mapped[float | None] = mapped_column(Float)
    #: CANCELS_AHEAD: cancellations remove size in front of it too.
    optimistic_fill_probability: Mapped[float] = mapped_column(Float, nullable=False)
    optimistic_wait_seconds: Mapped[float | None] = mapped_column(Float)

    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    assumptions: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    detail: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_queue_estimates_user", "user_id", "created_at"),
        Index("ix_queue_estimates_dataset", "dataset_id", "created_at"),
        CheckConstraint("quantity_ahead >= 0", name="ck_queue_quantity_ahead_non_negative"),
        CheckConstraint("horizon_seconds > 0", name="ck_queue_horizon_positive"),
        CheckConstraint(
            "pessimistic_fill_probability >= 0 AND pessimistic_fill_probability <= 1"
            " AND optimistic_fill_probability >= 0 AND optimistic_fill_probability <= 1",
            name="ck_queue_probabilities_are_probabilities",
        ),
        CheckConstraint(
            "optimistic_fill_probability >= pessimistic_fill_probability",
            name="ck_queue_estimate_is_a_bracket",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_queue_confidence_is_a_score"
        ),
        CheckConstraint(
            "trades_observed + cancels_observed > 0",
            name="ck_queue_needs_an_observed_departure",
        ),
    )
