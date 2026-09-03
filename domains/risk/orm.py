"""Risk persistence.

Every risk result points at the ``portfolio_valuations`` row it was computed
from, so the chain from a VaR number back to the quotes that produced it is a
matter of following foreign keys rather than of trust.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import JSONDict, UTCDateTime


class RiskSnapshotORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The repriceable state of a portfolio at one moment.

    Sits between a valuation and the risk measures taken from it, so a VaR and a
    stress run in the same request are provably looking at the same book.
    """

    __tablename__ = "risk_snapshots"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    valuation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("portfolio_valuations.id", ondelete="CASCADE"),
        nullable=False,
    )
    as_of_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    market_state_id: Mapped[str | None] = mapped_column(String(64))

    positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Model value at the unshocked anchors, and the value the portfolio was
    #: marked at. Separate columns: the gap between them is a fact about the
    #: book, not an error to be reconciled away.
    base_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reported_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    delta: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gamma: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vega_per_vol_point: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    theta_per_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rho_per_bp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    excluded: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_risk_snapshots_portfolio", "portfolio_id", "as_of_timestamp"),
        CheckConstraint("excluded_positions >= 0", name="ck_risk_snapshot_excluded_non_negative"),
    )


class VaRResultORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "var_results"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("risk_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Scenarios repriced: historical observations, or simulated paths.
    scenarios: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: The seed, for the Monte Carlo method. Null for the others, because a
    #: seed on a method that does not sample would be meaningless.
    seed: Mapped[int | None] = mapped_column(Integer)

    tail_risk: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    estimate_intervals: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    assumptions: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    factor_panel: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_var_results_portfolio", "portfolio_id", "created_at"),
        Index("ix_var_results_snapshot", "snapshot_id", "method"),
        CheckConstraint("scenarios >= 0", name="ck_var_scenarios_non_negative"),
        CheckConstraint(
            "method <> 'MONTE_CARLO' OR seed IS NOT NULL",
            name="ck_var_monte_carlo_records_its_seed",
        ),
    )


class StressResultORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "stress_results"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("risk_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    #: Null for a shipped template, which has no row of its own.
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stress_scenarios.id", ondelete="SET NULL")
    )
    scenario_name: Mapped[str] = mapped_column(String(120), nullable=False)
    scenario_source: Mapped[str] = mapped_column(String(32), nullable=False)
    shocks: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    base_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    shocked_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: The linear estimate, stored beside the answer rather than instead of it,
    #: so the size of the approximation is auditable after the fact.
    greek_estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    time_decay_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    floored_volatilities: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    contributions: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    positions: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_stress_results_portfolio", "portfolio_id", "created_at"),
        Index("ix_stress_results_snapshot", "snapshot_id"),
    )


class MarginResultORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An estimate from a named model, with everything that produced it.

    Note the columns that are **not** here: nothing called `required_margin`,
    nothing naming a broker or a venue, and no liquidation price. The schema is
    the first place a claim about broker equivalence could creep in, so it is the
    first place it is refused.
    """

    __tablename__ = "margin_results"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("risk_snapshots.id", ondelete="CASCADE"), nullable=False
    )

    method: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(24), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    estimated_margin: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    #: User-supplied. Null means unknown, and then buffer and utilisation are
    #: null too rather than defaulted to something that is not capital.
    eligible_capital: Mapped[float | None] = mapped_column(Float)
    buffer: Mapped[float | None] = mapped_column(Float)
    utilisation: Mapped[float | None] = mapped_column(Float)
    in_shortfall_at_rest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vol_co_shock: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    worst_spot_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    worst_vol_points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    worst_loss: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    worst_at_grid_edge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The sentence the API and the UI both render, stored so that what a user
    #: was told is recoverable later rather than regenerated by newer code.
    summary: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    components: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    assumptions: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    parameters: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    shortfall_region: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    ladder: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_margin_results_portfolio", "portfolio_id", "created_at"),
        Index("ix_margin_results_snapshot", "snapshot_id"),
        CheckConstraint("estimated_margin >= 0", name="ck_margin_estimate_non_negative"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_margin_confidence_in_range"
        ),
        # No buffer and no utilisation without the capital they are measured
        # against. Defaulting capital to portfolio value would produce a
        # confident number about a quantity nobody supplied.
        CheckConstraint(
            "eligible_capital IS NOT NULL OR (buffer IS NULL AND utilisation IS NULL)",
            name="ck_margin_buffer_requires_capital",
        ),
    )
