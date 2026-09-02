"""Optional TimescaleDB hypertables.

Time-ordered observation tables become hypertables when the ``timescaledb``
extension is available. If it is not (plain PostgreSQL, or SQLite in CI), the
tables stay ordinary tables with the same indexes and every query still works.
Nothing in the application depends on Timescale-specific SQL.

Revision ID: 0002_timescale
Revises: ce949be630d8
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_timescale"
down_revision: str | None = "ce949be630d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, time column). Chunked weekly: large enough that chunk count stays
#: manageable over years, small enough that a day-range query prunes well.
HYPERTABLES: tuple[tuple[str, str], ...] = (
    ("market_quotes", "exchange_timestamp"),
    ("option_quotes", "exchange_timestamp"),
)
CHUNK_INTERVAL = "7 days"


def _timescale_available(connection) -> bool:
    if connection.dialect.name != "postgresql":
        return False
    result = connection.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    ).scalar()
    return bool(result)


def upgrade() -> None:
    connection = op.get_bind()
    if not _timescale_available(connection):
        return

    connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    for table, time_column in HYPERTABLES:
        # The primary key must include the partitioning column for a hypertable.
        connection.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT {table}_pkey"))
        connection.execute(sa.text(f"ALTER TABLE {table} ADD PRIMARY KEY (id, {time_column})"))
        connection.execute(
            sa.text(
                f"SELECT create_hypertable('{table}', '{time_column}', "
                f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', "
                "migrate_data => TRUE, if_not_exists => TRUE)"
            )
        )


def downgrade() -> None:
    # Hypertable conversion is not reversible in place. Downgrading leaves the
    # tables as hypertables, which remain fully queryable; the alternative is a
    # full table rewrite that would be far more dangerous than the state it
    # replaces.
    return
