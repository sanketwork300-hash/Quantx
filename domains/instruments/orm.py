"""Instrument persistence."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import DecimalType, JSONDict


class InstrumentORM(TimestampMixin, Base):
    __tablename__ = "instruments"

    # Not UUIDPrimaryKeyMixin: instrument ids are derived (uuid5 of the
    # canonical key), never randomly generated.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    asset_class: Mapped[str] = mapped_column(String(24), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str | None] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    lot_size: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(DecimalType())
    option_type: Mapped[str | None] = mapped_column(String(8))
    exercise_style: Mapped[str | None] = mapped_column(String(16))
    settlement_type: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    instrument_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONDict, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_instruments_symbol", "exchange", "symbol"),
        Index("ix_instruments_chain", "underlying_id", "expiry", "strike", "option_type"),
        Index("ix_instruments_asset_class", "asset_class"),
    )


class InstrumentAliasORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "instrument_aliases"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    alias_symbol: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "alias_symbol", name="uq_alias_source_symbol"),
        Index("ix_alias_instrument", "instrument_id"),
    )
