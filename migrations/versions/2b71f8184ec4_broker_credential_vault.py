"""broker credential vault

Replaces the arrangement where a provider access token lived in an environment
variable and had to be replaced by hand whenever the provider expired it. A
credential is now a per-user row, encrypted at rest, with the key that sealed it
recorded so keys can be rotated.

Revision ID: 2b71f8184ec4
Revises: 9edd21fa707a
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import infrastructure.database.types as qip

revision: str = "2b71f8184ec4"
down_revision: str | None = "9edd21fa707a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_connections",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("encryption_key_id", sa.String(length=32), nullable=True),
        sa.Column("access_token_nonce", sa.LargeBinary(length=16), nullable=True),
        sa.Column("access_token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_nonce", sa.LargeBinary(length=16), nullable=True),
        sa.Column("refresh_token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("provider_account_id", sa.String(length=64), nullable=True),
        sa.Column("scopes", qip.JSONDict(), nullable=False),
        sa.Column("expires_at", qip.UTCDateTime(), nullable=True),
        sa.Column("expiry_source", sa.String(length=24), nullable=False),
        sa.Column("connected_at", qip.UTCDateTime(), nullable=True),
        sa.Column("last_refreshed_at", qip.UTCDateTime(), nullable=True),
        sa.Column("last_used_at", qip.UTCDateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("pending_state_nonce", sa.String(length=64), nullable=True),
        sa.Column("pending_state_expires_at", qip.UTCDateTime(), nullable=True),
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
        # One credential per user per provider: two rows would make "which token
        # is current" a question the code would have to guess at.
        sa.UniqueConstraint("user_id", "provider", name="uq_broker_connection_user_provider"),
    )
    op.create_index(
        "ix_broker_connection_status", "broker_connections", ["provider", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_broker_connection_status", table_name="broker_connections")
    op.drop_table("broker_connections")
