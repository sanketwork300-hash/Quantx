"""order management, paper accounts and the audit trail

Build spec §23, §24 and §25. Five tables: the account that holds cash and the
rules it trades under, its positions, its orders, their fills, and an
append-only record of everything that happened to any of them.

Three constraints here are not defensive programming but statements about what
the data means. An order cannot have filled more than it asked for. A rejected
order always carries a reason, so "why did this not trade" is answerable from
the row. A halted account always carries the reason it was halted, written in
the same statement that halted it.

A note on the decimal CHECKs. ``DecimalType`` is NUMERIC on Postgres and TEXT
elsewhere, so ``filled_quantity <= quantity`` compares strings on SQLite, where
``'4' <= '10'`` is false. Each comparison casts to NUMERIC: a no-op on Postgres,
and the numeric affinity SQLite needs. A constraint that holds on one dialect
and not the other is worse than none.

Revision ID: a3c7d5e91b04
Revises: e6104a2345e6
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import infrastructure.database.types as qip

revision: str = "a3c7d5e91b04"
down_revision: str | None = "e6104a2345e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trading_accounts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", qip.UTCDateTime(), nullable=False),
        sa.Column("updated_at", qip.UTCDateTime(), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=8), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("cash", qip.DecimalType(), nullable=False),
        sa.Column("opening_cash", qip.DecimalType(), nullable=False),
        sa.Column("cost_schedule", qip.JSONDict(), nullable=False),
        sa.Column("fill_policy", sa.String(length=24), nullable=False),
        sa.Column("max_quote_age_seconds", sa.Integer(), nullable=False),
        sa.Column("risk_limits", qip.JSONDict(), nullable=False),
        sa.Column("kill_switch_engaged_at", qip.UTCDateTime(), nullable=True),
        sa.Column("kill_switch_reason", sa.String(length=500), nullable=True),
        sa.Column("live_armed_at", qip.UTCDateTime(), nullable=True),
        sa.Column("metadata", qip.JSONDict(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_trading_account_name"),
        sa.CheckConstraint("venue in ('PAPER','LIVE')", name="ck_trading_account_venue"),
        sa.CheckConstraint(
            "(kill_switch_engaged_at is null) = (kill_switch_reason is null)",
            name="ck_kill_switch_has_a_reason",
        ),
    )
    op.create_index("ix_trading_accounts_user", "trading_accounts", ["user_id", "created_at"])

    op.create_table(
        "trading_positions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", qip.UTCDateTime(), nullable=False),
        sa.Column("updated_at", qip.UTCDateTime(), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("quantity", qip.DecimalType(), nullable=False),
        sa.Column("average_price", qip.DecimalType(), nullable=False),
        sa.Column("realised_pnl", qip.DecimalType(), nullable=False),
        sa.Column("fees_paid", qip.DecimalType(), nullable=False),
        sa.Column("strategy_tag", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "instrument_id", name="uq_trading_position"),
        sa.CheckConstraint(
            "cast(fees_paid as numeric) >= 0",
            name="ck_trading_position_fees_non_negative",
        ),
    )

    op.create_table(
        "trading_orders",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", qip.UTCDateTime(), nullable=False),
        sa.Column("updated_at", qip.UTCDateTime(), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", qip.DecimalType(), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("time_in_force", sa.String(length=8), nullable=False),
        sa.Column("limit_price", qip.DecimalType(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("venue", sa.String(length=8), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("filled_quantity", qip.DecimalType(), nullable=False),
        sa.Column("average_fill_price", qip.DecimalType(), nullable=True),
        sa.Column("fees", qip.DecimalType(), nullable=False),
        sa.Column("decision_price", qip.DecimalType(), nullable=True),
        sa.Column("submitted_at", qip.UTCDateTime(), nullable=True),
        sa.Column("acknowledged_at", qip.UTCDateTime(), nullable=True),
        sa.Column("closed_at", qip.UTCDateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=48), nullable=True),
        sa.Column("rejection_detail", sa.Text(), nullable=True),
        sa.Column("rejection_observed", qip.JSONDict(), nullable=False),
        sa.Column("strategy_tag", sa.String(length=64), nullable=True),
        sa.Column("parent_order_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("metadata", qip.JSONDict(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_order_id"], ["trading_orders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "client_order_id", name="uq_order_client_id"),
        sa.CheckConstraint("cast(quantity as numeric) > 0", name="ck_order_quantity_positive"),
        sa.CheckConstraint(
            "cast(filled_quantity as numeric) >= 0",
            name="ck_order_filled_non_negative",
        ),
        sa.CheckConstraint(
            "cast(filled_quantity as numeric) <= cast(quantity as numeric)",
            name="ck_order_filled_within_quantity",
        ),
        sa.CheckConstraint("cast(fees as numeric) >= 0", name="ck_order_fees_non_negative"),
        sa.CheckConstraint("venue in ('PAPER','LIVE')", name="ck_order_venue"),
        sa.CheckConstraint(
            "status in ('NEW','ACKNOWLEDGED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED')",
            name="ck_order_status",
        ),
        sa.CheckConstraint(
            "(status <> 'REJECTED') or (rejection_reason is not null)",
            name="ck_rejected_order_has_a_reason",
        ),
    )
    op.create_index(
        "ix_orders_account_status", "trading_orders", ["account_id", "status", "created_at"]
    )
    op.create_index("ix_orders_instrument", "trading_orders", ["account_id", "instrument_id"])

    op.create_table(
        "trading_order_fills",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", qip.UTCDateTime(), nullable=False),
        sa.Column("updated_at", qip.UTCDateTime(), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("quantity", qip.DecimalType(), nullable=False),
        sa.Column("price", qip.DecimalType(), nullable=False),
        sa.Column("price_basis", sa.String(length=24), nullable=False),
        sa.Column("filled_at", qip.UTCDateTime(), nullable=False),
        sa.Column("reference_price", qip.DecimalType(), nullable=True),
        sa.Column("reference_basis", sa.String(length=24), nullable=True),
        sa.Column("quote_exchange_timestamp", qip.UTCDateTime(), nullable=True),
        sa.Column("cost_total", qip.DecimalType(), nullable=False),
        sa.Column("cost_components", qip.JSONDict(), nullable=False),
        sa.Column("costs_modelled", sa.Boolean(), nullable=False),
        sa.Column("broker_trade_id", sa.String(length=64), nullable=True),
        sa.Column("flags", qip.JSONDict(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["trading_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "sequence", name="uq_fill_sequence"),
        sa.CheckConstraint("cast(quantity as numeric) <> 0", name="ck_fill_quantity_non_zero"),
        sa.CheckConstraint("cast(price as numeric) > 0", name="ck_fill_price_positive"),
        sa.CheckConstraint("cast(cost_total as numeric) >= 0", name="ck_fill_cost_non_negative"),
    )
    op.create_index("ix_fills_account", "trading_order_fills", ["account_id", "filled_at"])

    op.create_table(
        "trading_order_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", qip.UTCDateTime(), nullable=False),
        sa.Column("updated_at", qip.UTCDateTime(), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", qip.UTCDateTime(), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("payload", qip.JSONDict(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["trading_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["trading_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_events_order", "trading_order_events", ["order_id", "occurred_at"])
    op.create_index(
        "ix_order_events_account", "trading_order_events", ["account_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_table("trading_order_events")
    op.drop_table("trading_order_fills")
    op.drop_table("trading_orders")
    op.drop_table("trading_positions")
    op.drop_table("trading_accounts")
