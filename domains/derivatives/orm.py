"""Derivatives persistence.

Implied volatilities live in their own table rather than as columns on
``option_quotes``. That is the observation/estimate separation made physical:
``option_quotes`` is an append-only record of what the market showed, and this
table holds what a *model* implied from it under a stated forward, curve and
day count. Re-running the analysis with a different curve writes new rows here
and touches nothing in the observation table.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
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
from infrastructure.database.types import DecimalType, JSONDict, UTCDateTime


class YieldCurveORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A dated discount curve, addressed by its content-derived id."""

    __tablename__ = "yield_curves"

    curve_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    as_of_timestamp: Mapped[object] = mapped_column(UTCDateTime, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    day_count: Mapped[str] = mapped_column(String(16), nullable=False)
    interpolation: Mapped[str] = mapped_column(String(24), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    #: Times and zero rates, kept together so a curve rebuilds exactly.
    points: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (Index("ix_yield_curves_lookup", "currency", "as_of_timestamp"),)


class ChainAnalysisORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One run of the Phase 1 analysis over one chain snapshot."""

    __tablename__ = "chain_analyses"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chain_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("option_chain_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    as_of_timestamp: Mapped[object] = mapped_column(UTCDateTime, nullable=False)
    curve_id: Mapped[str | None] = mapped_column(String(64))
    day_count: Mapped[str] = mapped_column(String(16), nullable=False)
    settlement_time_utc: Mapped[str | None] = mapped_column(String(16))
    underlying_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    quotes_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes_solved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expiries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_chain_analyses_snapshot", "chain_snapshot_id", "created_at"),
        Index("ix_chain_analyses_user", "user_id", "created_at"),
        CheckConstraint("quotes_solved <= quotes_in", name="ck_analysis_solved_le_in"),
    )


class ForwardEstimateORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every estimate that was made, not only the one that was used.

    Storing the rejected estimates is what makes a later "why was this slice's
    log-moneyness wrong?" answerable.
    """

    __tablename__ = "forward_estimates"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chain_analyses.id", ondelete="CASCADE"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    value: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    residual_error: Mapped[float | None] = mapped_column(Float)
    discount_factor: Mapped[float | None] = mapped_column(Float)
    time_to_expiry: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(32))
    assumptions: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    __table_args__ = (
        Index("ix_forward_estimates_analysis", "analysis_id", "expiry"),
        UniqueConstraint("analysis_id", "expiry", "method", name="uq_forward_per_method"),
    )


class ImpliedVolORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Market-implied volatility for one quote under one analysis."""

    __tablename__ = "option_implied_vols"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chain_analyses.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    option_type: Mapped[str] = mapped_column(String(8), nullable=False)

    price_used: Mapped[float | None] = mapped_column(Float)
    price_source: Mapped[str] = mapped_column(String(8), nullable=False, default="NONE")
    price_spread: Mapped[float | None] = mapped_column(Float)

    #: Implied by the observed price. Never written from a fitted surface; the
    #: Phase 2 reference IV gets its own column in its own table.
    market_iv: Mapped[float | None] = mapped_column(Float)
    market_iv_bid: Mapped[float | None] = mapped_column(Float)
    market_iv_ask: Mapped[float | None] = mapped_column(Float)

    converged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    solver: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    error: Mapped[str | None] = mapped_column(String(32))
    vega: Mapped[float | None] = mapped_column(Float)
    #: Volatility uncertainty implied by one price ulp. Phase 3 confidence
    #: scoring reads this; nothing should display more precision than it allows.
    uncertainty: Mapped[float | None] = mapped_column(Float)

    data_quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    liquidity_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    time_to_expiry: Mapped[float | None] = mapped_column(Float)
    log_moneyness: Mapped[float | None] = mapped_column(Float)
    total_variance: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    used_for_smile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    smile_exclusion: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_implied_vols_analysis", "analysis_id", "expiry", "strike", "option_type"),
        Index("ix_implied_vols_instrument", "instrument_id"),
        CheckConstraint(
            "market_iv IS NOT NULL OR error IS NOT NULL",
            name="ck_implied_vol_has_value_or_reason",
        ),
    )


class VolatilitySurfaceORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A calibrated surface header.

    ``surface_id`` is content-addressed: two rows with the same id were fitted
    from the same numbers, so a provenance record naming one is enough to know
    exactly which surface produced a reference value.
    """

    __tablename__ = "volatility_surfaces"

    surface_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chain_analyses.id", ondelete="CASCADE"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    as_of_timestamp: Mapped[object] = mapped_column(UTCDateTime, nullable=False)
    model: Mapped[str] = mapped_column(String(24), nullable=False)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False)
    curve_id: Mapped[str | None] = mapped_column(String(64))
    slices_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slices_fitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calibration_timestamp: Mapped[object] = mapped_column(UTCDateTime, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_surfaces_underlying", "underlying_id", "as_of_timestamp"),
        Index("ix_surfaces_user", "user_id", "created_at"),
        CheckConstraint("slices_fitted <= slices_total", name="ck_surface_fitted_le_total"),
    )


class SurfaceSliceORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One expiry's calibration, with its metrics and its market context."""

    __tablename__ = "surface_slices"

    surface_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("volatility_surfaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    time_to_expiry: Mapped[float] = mapped_column(Float, nullable=False)
    forward: Mapped[float] = mapped_column(Float, nullable=False)
    discount_factor: Mapped[float] = mapped_column(Float, nullable=False)
    forward_method: Mapped[str | None] = mapped_column(String(24))
    forward_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rmse_total_variance: Mapped[float | None] = mapped_column(Float)
    weighted_rmse: Mapped[float | None] = mapped_column(Float)
    rmse_vol_points: Mapped[float | None] = mapped_column(Float)
    max_error_vol_points: Mapped[float | None] = mapped_column(Float)
    optimizer: Mapped[str] = mapped_column(String(24), nullable=False, default="SLSQP")
    optimizer_message: Mapped[str | None] = mapped_column(String(255))
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    starts_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    starts_feasible: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Durrleman's g at its minimum: negative means a negative implied density.
    min_durrleman_g: Mapped[float | None] = mapped_column(Float)
    min_durrleman_k: Mapped[float | None] = mapped_column(Float)
    wing_slope: Mapped[float | None] = mapped_column(Float)
    constraints_satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Log-moneyness range actually fitted. A lookup outside it is extrapolation.
    k_min: Mapped[float | None] = mapped_column(Float)
    k_max: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("ix_surface_slices_surface", "surface_id", "expiry"),
        UniqueConstraint("surface_id", "expiry", name="uq_surface_slice_expiry"),
    )


class SurfaceParametersORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The fitted parameters, alone.

    Separate from the slice metrics on purpose: reference implied volatilities
    must be reproducible from *these five numbers plus the forward and maturity*
    and nothing else. Keeping them in their own row makes that contract explicit
    and makes the reproduction test read the same way the code does.
    """

    __tablename__ = "surface_parameters"

    slice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("surface_slices.id", ondelete="CASCADE"), nullable=False
    )
    parameterization: Mapped[str] = mapped_column(String(24), nullable=False, default="RAW_SVI")
    a: Mapped[float] = mapped_column(Float, nullable=False)
    b: Mapped[float] = mapped_column(Float, nullable=False)
    rho: Mapped[float] = mapped_column(Float, nullable=False)
    m: Mapped[float] = mapped_column(Float, nullable=False)
    sigma: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("slice_id", "parameterization", name="uq_slice_parameterization"),
    )


class ArbitrageReportORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Raw-market and fitted-surface findings, kept apart.

    Collapsing the two scopes would let a smooth fit hide a broken market and a
    broken fit be reported as a market anomaly, so ``scope`` is part of the
    identity of a report rather than a label on it.
    """

    __tablename__ = "arbitrage_reports"

    surface_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("volatility_surfaces.id", ondelete="CASCADE")
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chain_analyses.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(16))
    violations_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checks_run: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    summary: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_arbitrage_reports_analysis", "analysis_id", "scope", "created_at"),
        # One report per scope per *surface*, not per analysis: an analysis can
        # legitimately be recalibrated (a different seed, a later code version),
        # and keeping the earlier reports is how you see whether a refit fixed a
        # violation or merely moved it.
        UniqueConstraint("surface_id", "scope", name="uq_arbitrage_report_scope"),
    )


class ArbitrageViolationORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "arbitrage_violations"

    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("arbitrage_reports.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    violation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    #: How far the condition is breached, in that condition's own units.
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    #: What the magnitude was judged against, in the same units.
    tolerance: Mapped[float | None] = mapped_column(Float)
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(DecimalType())
    option_type: Mapped[str | None] = mapped_column(String(8))
    detail: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    affected_instruments: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    __table_args__ = (
        Index("ix_arbitrage_violations_report", "report_id", "severity"),
        Index("ix_arbitrage_violations_expiry", "report_id", "expiry"),
    )


class SurfaceCharacteristicORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A surface's shape at a standard tenor.

    Recorded at fixed tenors rather than at fitted expiries because expiries
    roll: a time series of "the October slice" runs out in October, whereas a
    time series of "the 30-day level" is what percentile analytics need.
    """

    __tablename__ = "surface_characteristics"

    surface_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("volatility_surfaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    as_of_timestamp: Mapped[object] = mapped_column(UTCDateTime, nullable=False)

    tenor_days: Mapped[int] = mapped_column(Integer, nullable=False)
    time_to_expiry: Mapped[float] = mapped_column(Float, nullable=False)
    forward: Mapped[float] = mapped_column(Float, nullable=False)

    atm_volatility: Mapped[float] = mapped_column(Float, nullable=False)
    skew: Mapped[float] = mapped_column(Float, nullable=False)
    curvature: Mapped[float] = mapped_column(Float, nullable=False)
    atm_total_variance: Mapped[float] = mapped_column(Float, nullable=False)

    method: Mapped[str] = mapped_column(String(32), nullable=False)
    flags: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    __table_args__ = (
        Index(
            "ix_characteristics_history",
            "user_id",
            "underlying_id",
            "tenor_days",
            "as_of_timestamp",
        ),
        UniqueConstraint("surface_id", "tenor_days", name="uq_characteristic_tenor"),
    )


class AnomalyScanORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One run of the surface scanner."""

    __tablename__ = "anomaly_scans"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    surface_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("volatility_surfaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chain_analyses.id", ondelete="CASCADE"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    as_of_timestamp: Mapped[object] = mapped_column(UTCDateTime, nullable=False)
    quotes_examined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quotes_scored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flagged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The detection policy in force. Recorded because it decides the answer.
    policy: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_anomaly_scans_underlying", "user_id", "underlying_id", "created_at"),
        CheckConstraint("flagged <= quotes_scored", name="ck_scan_flagged_le_scored"),
    )


class SurfaceAnomalyORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One scored quote.

    Every scored quote is stored, not only the flagged ones: a deviation that
    fell below the threshold is the evidence that the threshold was doing
    something, and it is also the history a later scan measures against.
    """

    __tablename__ = "surface_anomalies"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("anomaly_scans.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    option_type: Mapped[str] = mapped_column(String(8), nullable=False)

    #: Implied by the observed price.
    market_iv: Mapped[float] = mapped_column(Float, nullable=False)
    #: Produced by the fitted surface. A model output, never a fair value.
    reference_iv: Mapped[float] = mapped_column(Float, nullable=False)
    iv_difference: Mapped[float] = mapped_column(Float, nullable=False)
    relative_deviation: Mapped[float] = mapped_column(Float, nullable=False)

    market_iv_bid: Mapped[float | None] = mapped_column(Float)
    market_iv_ask: Mapped[float | None] = mapped_column(Float)
    envelope_position: Mapped[str] = mapped_column(String(16), nullable=False)
    excess_over_envelope: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    #: Everything that could account for the difference, combined.
    explained_scale: Mapped[float] = mapped_column(Float, nullable=False)
    z_score: Mapped[float] = mapped_column(Float, nullable=False)
    historical_z_score: Mapped[float | None] = mapped_column(Float)
    historical_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    liquidity_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    data_quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    calibration_rmse_vol_points: Mapped[float | None] = mapped_column(Float)
    iv_uncertainty: Mapped[float | None] = mapped_column(Float)
    reference_method: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_flags: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Grounded reasons, each naming the measurement behind it. Never an opaque
    #: narrative, and never a recommendation.
    explanation: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    __table_args__ = (
        Index("ix_surface_anomalies_scan", "scan_id", "flagged", "z_score"),
        Index("ix_surface_anomalies_instrument", "instrument_id", "created_at"),
        CheckConstraint(
            "flagged = false OR confidence > 0", name="ck_flagged_anomaly_has_confidence"
        ),
    )
