"""Market-data persistence.

Observation tables are append-only. A correction inserts a new row with a new
receive timestamp; nothing that was observed is ever updated in place, because
overwriting an observation with a better guess is precisely what build spec 1.2
forbids.

``option_quotes`` stores both kept and excluded rows. An excluded row carries a
NOT NULL ``exclusion_reason`` whenever ``excluded`` is true, which makes the
Phase 1 acceptance criterion "every excluded quote has a reason" a database
constraint rather than a convention.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

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
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import DecimalType, JSONDict, UTCDateTime


class UploadORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "uploads"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The client's filename is metadata only. The stored key is server
    #: generated, so a hostile filename can never influence a storage path.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RECEIVED")
    error: Mapped[dict | None] = mapped_column(JSONDict)

    __table_args__ = (Index("ix_uploads_user", "user_id", "created_at"),)


class OptionChainSnapshotORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "option_chain_snapshots"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    as_of_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_digest: Mapped[str | None] = mapped_column(String(64))
    upload_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("uploads.id", ondelete="SET NULL")
    )
    underlying_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    rows_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_kept: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_summary: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_chain_snapshots_underlying", "underlying_id", "as_of_timestamp"),
        Index("ix_chain_snapshots_user", "user_id", "created_at"),
        CheckConstraint(
            "rows_input = rows_kept + rows_excluded + rows_rejected",
            name="ck_chain_snapshot_row_conservation",
        ),
    )


class OptionQuoteORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "option_quotes"

    chain_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("option_chain_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_row_number: Mapped[int | None] = mapped_column(Integer)

    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    option_type: Mapped[str] = mapped_column(String(8), nullable=False)

    exchange_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    receive_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    bid_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    bid_size: Mapped[Decimal | None] = mapped_column(DecimalType())
    ask_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    ask_size: Mapped[Decimal | None] = mapped_column(DecimalType())
    last_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    volume: Mapped[Decimal | None] = mapped_column(DecimalType())
    open_interest: Mapped[Decimal | None] = mapped_column(DecimalType())
    sequence_number: Mapped[int | None] = mapped_column(BigInteger)
    underlying_price: Mapped[Decimal | None] = mapped_column(DecimalType())

    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    stale_score: Mapped[float] = mapped_column(Float, nullable=False)
    spread_score: Mapped[float] = mapped_column(Float, nullable=False)
    liquidity_score: Mapped[float] = mapped_column(Float, nullable=False)
    consistency_score: Mapped[float] = mapped_column(Float, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    quality_flags: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_option_quotes_chain", "chain_snapshot_id", "expiry", "strike", "option_type"),
        Index("ix_option_quotes_instrument", "instrument_id", "exchange_timestamp"),
        CheckConstraint(
            "excluded = false OR exclusion_reason IS NOT NULL",
            name="ck_excluded_quote_has_reason",
        ),
    )


class MarketQuoteORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Non-option quotes: underlyings, futures, equities, crypto, FX."""

    __tablename__ = "market_quotes"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    exchange_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    receive_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    bid_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    bid_size: Mapped[Decimal | None] = mapped_column(DecimalType())
    ask_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    ask_size: Mapped[Decimal | None] = mapped_column(DecimalType())
    last_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    last_size: Mapped[Decimal | None] = mapped_column(DecimalType())
    volume: Mapped[Decimal | None] = mapped_column(DecimalType())
    open_interest: Mapped[Decimal | None] = mapped_column(DecimalType())
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence_number: Mapped[int | None] = mapped_column(BigInteger)
    overall_score: Mapped[float | None] = mapped_column(Float)
    quality_flags: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)

    __table_args__ = (
        Index("ix_market_quotes_instrument_time", "instrument_id", "exchange_timestamp"),
    )


class DataQualityReportORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "data_quality_reports"

    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stale_score: Mapped[float] = mapped_column(Float, nullable=False)
    spread_score: Mapped[float] = mapped_column(Float, nullable=False)
    liquidity_score: Mapped[float] = mapped_column(Float, nullable=False)
    consistency_score: Mapped[float] = mapped_column(Float, nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    flag_counts: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    flags: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (Index("ix_quality_reports_scope", "scope_type", "scope_id"),)
