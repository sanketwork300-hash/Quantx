"""historical warehouse registry

The data itself is partitioned Parquet in the object store; these two tables are
the record of it — what exists, what it covers, how good it is and where the
files are. That split is the point of the phase: a tick tape does not go in
PostgreSQL, but a registry is user-activity sized and transactional.

Revision ID: 437d1ff57460
Revises: 2b71f8184ec4
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import infrastructure.database.types as qip

revision: str = "437d1ff57460"
down_revision: str | None = "2b71f8184ec4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "warehouse_datasets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("layer", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("dataset_digest", sa.String(length=64), nullable=True),
        sa.Column("corporate_action_treatment", sa.String(length=24), nullable=False),
        sa.Column("continuous", sa.Boolean(), nullable=False),
        sa.Column("rows_in", sa.BigInteger(), nullable=False),
        sa.Column("rows_written", sa.BigInteger(), nullable=False),
        sa.Column("rows_excluded", sa.BigInteger(), nullable=False),
        sa.Column("rows_rejected", sa.BigInteger(), nullable=False),
        sa.Column("rows_flagged", sa.BigInteger(), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("partition_count", sa.Integer(), nullable=False),
        sa.Column("bytes_written", sa.BigInteger(), nullable=False),
        sa.Column("first_observation", qip.UTCDateTime(), nullable=True),
        sa.Column("last_observation", qip.UTCDateTime(), nullable=True),
        sa.Column("completeness_score", sa.Float(), nullable=True),
        sa.Column("consistency_score", sa.Float(), nullable=True),
        sa.Column("outlier_score", sa.Float(), nullable=True),
        sa.Column("source_score", sa.Float(), nullable=True),
        sa.Column("freshness_score", sa.Float(), nullable=True),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("quality_evidence", qip.JSONDict(), nullable=False),
        sa.Column("validation_summary", qip.JSONDict(), nullable=False),
        sa.Column("findings_key", sa.String(length=512), nullable=True),
        sa.Column("provenance", qip.JSONDict(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        # A registry row that claims more rows than it accounted for is a bug,
        # and the database is the last place that can still say so.
        sa.CheckConstraint(
            "rows_in = rows_written + rows_excluded + rows_rejected",
            name="ck_warehouse_dataset_row_conservation",
        ),
    )
    op.create_index("ix_warehouse_datasets_user", "warehouse_datasets", ["user_id", "created_at"])
    op.create_index(
        "ix_warehouse_datasets_scope", "warehouse_datasets", ["layer", "kind", "exchange"]
    )

    op.create_table(
        "warehouse_partitions",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("rows_flagged", sa.BigInteger(), nullable=False),
        sa.Column("bytes_written", sa.BigInteger(), nullable=False),
        sa.Column("first_observation", qip.UTCDateTime(), nullable=True),
        sa.Column("last_observation", qip.UTCDateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(["dataset_id"], ["warehouse_datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "object_key", name="uq_warehouse_partition_key"),
    )
    op.create_index("ix_warehouse_partitions_dataset", "warehouse_partitions", ["dataset_id"])
    op.create_index(
        "ix_warehouse_partitions_instrument_day",
        "warehouse_partitions",
        ["instrument_id", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_warehouse_partitions_instrument_day", table_name="warehouse_partitions")
    op.drop_index("ix_warehouse_partitions_dataset", table_name="warehouse_partitions")
    op.drop_table("warehouse_partitions")
    op.drop_index("ix_warehouse_datasets_scope", table_name="warehouse_datasets")
    op.drop_index("ix_warehouse_datasets_user", table_name="warehouse_datasets")
    op.drop_table("warehouse_datasets")
