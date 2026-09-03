"""Alembic environment.

The database URL always comes from ``QIP_DATABASE_URL`` via ``Settings``, never
from ``alembic.ini``, so migrations cannot be pointed at the wrong database by
an out-of-date config file.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the ORM modules is what populates Base.metadata. A model that is not
# imported here is invisible to autogenerate, so this list is the registry.
import domains.derivatives.orm  # noqa: F401, E402
import domains.execution.orm  # noqa: F401, E402
import domains.instruments.orm  # noqa: F401, E402
import domains.jobs.orm  # noqa: F401, E402
import domains.market_data.orm  # noqa: F401, E402
import domains.portfolio.orm  # noqa: F401, E402
import domains.reports.orm  # noqa: F401, E402
import domains.risk.orm  # noqa: F401, E402
import domains.scenarios.orm  # noqa: F401, E402
import domains.users.orm  # noqa: F401, E402
from infrastructure.database.base import Base
from infrastructure.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render our TypeDecorators with a module prefix the migration imports."""
    if type_ == "type" and obj.__class__.__module__ == "infrastructure.database.types":
        autogen_context.imports.add("import infrastructure.database.types as qip")
        return f"qip.{obj.__class__.__name__}()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
