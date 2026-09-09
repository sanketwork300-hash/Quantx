"""research experiment registry

A backtest is a claim, and a claim nobody can reproduce is an anecdote. Every
column here is something build spec §16 requires for a run to be rebuildable:
the dataset, the window, the strategy and its parameters, the features, the cost
schedule, the slippage assumption, the results, the code version and the data
digest.

Revision ID: e6104a2345e6
Revises: 437d1ff57460
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import infrastructure.database.types as qip

revision: str = "e6104a2345e6"
down_revision: str | None = "437d1ff57460"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_experiments",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("start_timestamp", qip.UTCDateTime(), nullable=False),
        sa.Column("end_timestamp", qip.UTCDateTime(), nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=24), nullable=False),
        sa.Column("strategy_parameters", qip.JSONDict(), nullable=False),
        sa.Column("features", qip.JSONDict(), nullable=False),
        sa.Column("engine_config", qip.JSONDict(), nullable=False),
        sa.Column("cost_schedule", qip.JSONDict(), nullable=False),
        sa.Column("slippage_model", qip.JSONDict(), nullable=False),
        sa.Column("code_commit", sa.String(length=64), nullable=False),
        sa.Column("data_digest", sa.String(length=64), nullable=True),
        sa.Column("initial_equity", qip.DecimalType(), nullable=False),
        sa.Column("final_equity", qip.DecimalType(), nullable=False),
        sa.Column("total_costs", qip.DecimalType(), nullable=False),
        sa.Column("total_slippage", qip.DecimalType(), nullable=False),
        sa.Column("gross_of_costs", sa.Boolean(), nullable=False),
        sa.Column("total_return", sa.Float(), nullable=True),
        sa.Column("cagr", sa.Float(), nullable=True),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("sortino", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("bars_in", sa.Integer(), nullable=False),
        sa.Column("bars_used", sa.Integer(), nullable=False),
        sa.Column("fill_count", sa.Integer(), nullable=False),
        sa.Column("traded_notional", qip.DecimalType(), nullable=False),
        sa.Column("metrics", qip.JSONDict(), nullable=False),
        sa.Column("attribution", qip.JSONDict(), nullable=False),
        sa.Column("warnings", qip.JSONDict(), nullable=False),
        sa.Column("provenance", qip.JSONDict(), nullable=False),
        sa.Column("equity_curve_key", sa.String(length=512), nullable=True),
        sa.Column("fills_key", sa.String(length=512), nullable=True),
        sa.Column("bytes_written", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            qip.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            qip.UTCDateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_id"], ["warehouse_datasets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_experiments_user", "research_experiments", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_research_experiments_strategy",
        "research_experiments",
        ["strategy_name", "instrument_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_experiments_strategy", table_name="research_experiments")
    op.drop_index("ix_research_experiments_user", table_name="research_experiments")
    op.drop_table("research_experiments")
