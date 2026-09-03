"""Migrations must be reversible and must match the ORM metadata.

CI runs ``upgrade head`` then ``downgrade base``. A migration that cannot be
undone is a migration nobody will dare to apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parent.parent.parent

EXPECTED_TABLES = {
    "users",
    "audit_logs",
    "instruments",
    "instrument_aliases",
    "uploads",
    "option_chain_snapshots",
    "option_quotes",
    "market_quotes",
    "data_quality_reports",
    "jobs",
    "model_versions",
    "yield_curves",
    "chain_analyses",
    "forward_estimates",
    "option_implied_vols",
    "volatility_surfaces",
    "surface_slices",
    "surface_parameters",
    "arbitrage_reports",
    "arbitrage_violations",
    "surface_characteristics",
    "anomaly_scans",
    "surface_anomalies",
    "portfolios",
    "positions",
    "portfolio_valuations",
    "position_valuations",
    "stress_scenarios",
    "risk_snapshots",
    "var_results",
    "stress_results",
    "margin_results",
    "executions",
    "execution_reports",
    "execution_simulations",
    "global_surfaces",
    "global_surface_slices",
    "local_volatility_surfaces",
    "risk_neutral_densities",
    "heston_calibrations",
    "model_consensus_runs",
    "model_values",
}


@pytest.fixture
def alembic_config(tmp_path, monkeypatch):
    database_path = tmp_path / "migrations.db"
    monkeypatch.setenv("QIP_DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")

    from infrastructure.settings import reset_settings_cache

    reset_settings_cache()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    yield config, database_path
    reset_settings_cache()


def _sync_url(path: Path) -> str:
    return f"sqlite:///{path}"


class TestMigrations:
    def test_upgrade_creates_every_table(self, alembic_config):
        config, database_path = alembic_config
        command.upgrade(config, "head")

        engine = create_engine(_sync_url(database_path))
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
        assert tables >= EXPECTED_TABLES, EXPECTED_TABLES - tables

    def test_downgrade_removes_everything(self, alembic_config):
        config, database_path = alembic_config
        command.upgrade(config, "head")
        command.downgrade(config, "base")

        engine = create_engine(_sync_url(database_path))
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
        assert tables <= {"alembic_version"}

    def test_upgrade_downgrade_upgrade_is_stable(self, alembic_config):
        config, database_path = alembic_config
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")

        engine = create_engine(_sync_url(database_path))
        tables = set(inspect(engine).get_table_names())
        engine.dispose()
        assert tables >= EXPECTED_TABLES

    def test_schema_matches_the_orm_metadata(self, alembic_config):
        """Autogenerate must find nothing to do after a clean upgrade.

        If it does, the migrations and the models have drifted and a deploy
        would silently run against the wrong schema.
        """
        config, database_path = alembic_config
        command.upgrade(config, "head")

        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        # Import every ORM module so Base.metadata is complete. (migrations/env.py
        # does the same, but importing it here would run the alembic script
        # outside an alembic context.)
        import domains.derivatives.orm  # noqa: F401
        import domains.execution.orm  # noqa: F401
        import domains.instruments.orm  # noqa: F401
        import domains.jobs.orm  # noqa: F401
        import domains.market_data.orm  # noqa: F401
        import domains.portfolio.orm  # noqa: F401
        import domains.reports.orm  # noqa: F401
        import domains.risk.orm  # noqa: F401
        import domains.scenarios.orm  # noqa: F401
        import domains.users.orm  # noqa: F401
        from infrastructure.database.base import Base

        engine = create_engine(_sync_url(database_path))
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
        engine.dispose()

        # SQLite cannot express every constraint the models declare, so only
        # table- and column-level drift is meaningful here.
        significant = [
            entry
            for entry in diff
            if isinstance(entry, tuple)
            and entry[0] in {"add_table", "remove_table", "add_column", "remove_column"}
        ]
        assert significant == [], significant


class TestSchemaConstraints:
    def test_row_conservation_and_exclusion_reason_are_database_constraints(self):
        """Two invariants are enforced by CHECK constraints, not by convention."""
        source = (ROOT / "migrations" / "versions" / "ce949be630d8_phase_0_baseline.py").read_text()
        assert "ck_chain_snapshot_row_conservation" in source
        assert "ck_excluded_quote_has_reason" in source
        assert "ck_job_progress_range" in source
