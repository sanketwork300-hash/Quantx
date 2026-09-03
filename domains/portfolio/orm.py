"""Portfolio persistence."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
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


class PortfolioORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portfolios"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (Index("ix_portfolios_user", "user_id", "created_at"),)


class PositionORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "positions"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: Signed; negative is short. ``side`` is kept as the source stated it, and a
    #: disagreement is rejected at import rather than reconciled.
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    average_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="MANUAL")
    strategy_tag: Mapped[str | None] = mapped_column(String(64))
    position_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONDict, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_positions_portfolio", "portfolio_id", "instrument_id"),
        CheckConstraint("quantity <> 0", name="ck_position_quantity_non_zero"),
    )


class PortfolioValuationORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One valuation run of one portfolio against one market snapshot."""

    __tablename__ = "portfolio_valuations"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    as_of_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    #: Content-addressed snapshot id. Two valuations reporting the same id
    #: provably saw the same inputs.
    market_state_id: Mapped[str | None] = mapped_column(String(64))

    positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    base_market_value: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    gross_exposure: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    net_exposure: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)

    delta: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gamma: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vega_per_vol_point: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    theta_per_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rho_per_bp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    valuation_methods: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    aggregates: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_portfolio_valuations", "portfolio_id", "as_of_timestamp"),
        CheckConstraint("valued <= positions", name="ck_valuation_valued_le_positions"),
    )


class PositionValuationORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One position, valued.

    ``market_price`` and ``model_price`` are separate columns and neither is
    ever written from the other; ``price_used`` and ``valuation_method`` say
    which was taken and why.
    """

    __tablename__ = "position_valuations"

    valuation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("portfolio_valuations.id", ondelete="CASCADE"),
        nullable=False,
    )
    position_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(24), nullable=False)
    underlying_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(DecimalType())
    option_type: Mapped[str | None] = mapped_column(String(8))

    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    market_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    model_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    price_used: Mapped[Decimal | None] = mapped_column(DecimalType())
    valuation_method: Mapped[str] = mapped_column(String(24), nullable=False)

    market_value: Mapped[Decimal | None] = mapped_column(DecimalType())
    base_market_value: Mapped[Decimal | None] = mapped_column(DecimalType())
    fx_rate: Mapped[Decimal | None] = mapped_column(DecimalType())
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(DecimalType())

    delta: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gamma: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    vega_per_vol_point: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    theta_per_day: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rho_per_bp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Which volatility the Greeks were taken at.
    greek_source: Mapped[str] = mapped_column(String(24), nullable=False)
    implied_volatility: Mapped[float | None] = mapped_column(Float)
    time_to_expiry: Mapped[float | None] = mapped_column(Float)
    quote_age_seconds: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    __table_args__ = (
        Index("ix_position_valuations", "valuation_id", "asset_class"),
        Index("ix_position_valuations_instrument", "instrument_id"),
        CheckConstraint(
            "base_market_value IS NOT NULL OR valuation_method = 'UNAVAILABLE'",
            name="ck_position_valuation_has_value_or_reason",
        ),
    )
