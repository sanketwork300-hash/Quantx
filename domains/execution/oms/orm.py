"""Trading persistence: accounts, orders, fills and the audit trail.

Three invariants are enforced by the database rather than by the service, so
they survive a bug in the service.

``filled_quantity <= quantity`` — an order cannot fill more than it asked for.

``fees >= 0`` — a negative charge is a rebate, and a rebate that arrived through
a fee column would silently improve every P&L it touched.

An account's ``kill_switch_engaged_at`` is set with the reason in the same
statement, so there is no window in which trading is halted and nothing records
why.

Decimal columns need a word of explanation. ``DecimalType`` is NUMERIC on
Postgres and TEXT elsewhere, so a CHECK written as ``filled_quantity <=
quantity`` compares two *strings* on SQLite, where ``'4' <= '10'`` is false.
Every constraint here that compares decimals therefore casts to NUMERIC, which
is a no-op on Postgres and gives SQLite the numeric affinity the comparison
needs. A constraint that holds on one dialect and quietly does not on another is
worse than no constraint, because it passes in one place and fails in the other.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from infrastructure.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from infrastructure.database.types import DecimalType, JSONDict, UTCDateTime


class TradingAccountORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A book that orders are placed against.

    ``venue`` is on the account and repeated on every order. That is not
    redundancy to be normalised away: an account's venue could in principle be
    changed, and an order must remain able to say, for as long as it is kept,
    whether it was real money.
    """

    __tablename__ = "trading_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(8), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    #: Cash, moved by every fill. Allowed to go negative: an account that has
    #: traded beyond its balance says so rather than having the trade refused
    #: after it happened. Whether it may is a risk limit, checked before.
    cash: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    opening_cash: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    #: The cost schedule these fills are charged under, stored as supplied. A
    #: net P&L is meaningless without the schedule that produced it, so the
    #: schedule lives with the account rather than in a config file.
    cost_schedule: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    fill_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="QUOTE_ONLY")
    max_quote_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    risk_limits: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    kill_switch_engaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    kill_switch_reason: Mapped[str | None] = mapped_column(String(500))
    #: Set only by an explicit arming action, and cleared by the kill switch.
    #: Live orders need this *and* the deployment-level flag.
    live_armed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    account_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONDict, nullable=False, default=dict
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_trading_account_name"),
        CheckConstraint("venue in ('PAPER','LIVE')", name="ck_trading_account_venue"),
        CheckConstraint(
            "(kill_switch_engaged_at is null) = (kill_switch_reason is null)",
            name="ck_kill_switch_has_a_reason",
        ),
        Index("ix_trading_accounts_user", "user_id", "created_at"),
    )


class TradingPositionORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The account's book, kept as a running average-cost position.

    Reconstructible from the fills, and a test asserts that replaying them
    reproduces exactly this. Stored anyway because a live risk view cannot
    replay a year of fills on every request.
    """

    __tablename__ = "trading_positions"

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: Signed. Zero is kept rather than deleted: a flat position that has traded
    #: still carries realised P&L, and deleting the row would lose it.
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    average_price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    realised_pnl: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)
    fees_paid: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)
    strategy_tag: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_trading_position"),
        CheckConstraint(
            "cast(fees_paid as numeric) >= 0",
            name="ck_trading_position_fees_non_negative",
        ),
    )


class OrderORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "trading_orders"

    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: The caller's idempotency key, unique within the account. A retried
    #: submission finds this row instead of placing a second order.
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    venue: Mapped[str] = mapped_column(String(8), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)
    average_fill_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    fees: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)
    #: What the caller says the decision was taken against. Absent when they did
    #: not state one, and then no slippage figure is reported at all.
    decision_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rejection_reason: Mapped[str | None] = mapped_column(String(48))
    rejection_detail: Mapped[str | None] = mapped_column(Text)
    rejection_observed: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    strategy_tag: Mapped[str | None] = mapped_column(String(64))
    parent_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_orders.id", ondelete="SET NULL")
    )
    order_metadata: Mapped[dict] = mapped_column("metadata", JSONDict, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("account_id", "client_order_id", name="uq_order_client_id"),
        CheckConstraint("cast(quantity as numeric) > 0", name="ck_order_quantity_positive"),
        CheckConstraint(
            "cast(filled_quantity as numeric) >= 0",
            name="ck_order_filled_non_negative",
        ),
        CheckConstraint(
            "cast(filled_quantity as numeric) <= cast(quantity as numeric)",
            name="ck_order_filled_within_quantity",
        ),
        CheckConstraint("cast(fees as numeric) >= 0", name="ck_order_fees_non_negative"),
        CheckConstraint("venue in ('PAPER','LIVE')", name="ck_order_venue"),
        CheckConstraint(
            "status in ('NEW','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED')",
            name="ck_order_status",
        ),
        CheckConstraint(
            "(status <> 'REJECTED') or (rejection_reason is not null)",
            name="ck_rejected_order_has_a_reason",
        ),
        Index("ix_orders_account_status", "account_id", "status", "created_at"),
        Index("ix_orders_instrument", "account_id", "instrument_id"),
    )


class OrderFillORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution. Append-only in practice: a fill is never edited.

    ``price_basis`` is not nullable. A fill price with no statement of where it
    came from is the exact ambiguity the observation/estimate separation exists
    to prevent, and allowing a null here would let one in through the back door.
    """

    __tablename__ = "trading_order_fills"

    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_orders.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    #: Signed, so a fill row alone says which way it went.
    quantity: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False)
    price_basis: Mapped[str] = mapped_column(String(24), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    reference_price: Mapped[Decimal | None] = mapped_column(DecimalType())
    reference_basis: Mapped[str | None] = mapped_column(String(24))
    #: The exchange timestamp of the quote the paper fill was decided against.
    #: This is an observation and is never overwritten.
    quote_exchange_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cost_total: Mapped[Decimal] = mapped_column(DecimalType(), nullable=False, default=0)
    cost_components: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    costs_modelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    broker_trade_id: Mapped[str | None] = mapped_column(String(64))
    flags: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("cast(quantity as numeric) <> 0", name="ck_fill_quantity_non_zero"),
        CheckConstraint("cast(price as numeric) > 0", name="ck_fill_price_positive"),
        CheckConstraint("cast(cost_total as numeric) >= 0", name="ck_fill_cost_non_negative"),
        UniqueConstraint("order_id", "sequence", name="uq_fill_sequence"),
        Index("ix_fills_account", "account_id", "filled_at"),
    )


class OrderEventORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The audit trail build spec §46 requires for live trading.

    Append-only and never updated. Every state change, every gate decision and
    every broker exchange lands here with the payload that caused it, so the
    question "why did this order do that" has an answer that does not depend on
    logs having been retained.

    ``payload`` is written by the service and must never contain a credential:
    the broker adapters redact before they hand anything over.
    """

    __tablename__ = "trading_order_events"

    order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_orders.id", ondelete="CASCADE")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_order_events_order", "order_id", "occurred_at"),
        Index("ix_order_events_account", "account_id", "occurred_at"),
    )
