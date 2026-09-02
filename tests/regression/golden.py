"""Deterministic ingestion runner shared by the regression test and the
golden-file regeneration script.

It builds its own throwaway database so the output depends only on the input
file and the pipeline, never on test ordering or fixture state.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "tests" / "data"
GOLDEN_DIR = ROOT / "tests" / "regression" / "golden"

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
COLUMN_MAPPING = {
    "strike": "STRIKE_PRICE",
    "option_type": "CE_PE",
    "expiry": "EXPIRY_DT",
    "bid_price": "BID",
    "ask_price": "ASK",
    "last_price": "LTP",
    "bid_size": "BIDQTY",
    "ask_size": "ASKQTY",
    "volume": "VOL",
    "open_interest": "OI",
    "underlying_price": "UNDERLYING_VALUE",
}

#: Scores are float64 arithmetic; rounding here keeps the golden file stable
#: across platforms without hiding a real change (12 dp is far tighter than any
#: change a formula edit would produce).
SCORE_PRECISION = 12


async def _ingest(data: bytes, database_url: str) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    # Importing the ORM modules registers them on Base.metadata.
    import domains.derivatives.orm  # noqa: F401
    import domains.instruments.orm  # noqa: F401
    import domains.jobs.orm  # noqa: F401
    import domains.market_data.orm  # noqa: F401
    import domains.reports.orm  # noqa: F401
    from domains.instruments.service import InstrumentService
    from domains.market_data.ingestion.column_mapping import ColumnMapping
    from domains.market_data.ingestion.pipeline import (
        ContractSpec,
        OptionChainIngestionPipeline,
        OptionChainIngestionRequest,
        UnderlyingSpec,
    )
    from domains.market_data.repository import MarketDataRepository
    from domains.users.orm import UserORM
    from infrastructure.database.base import Base

    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        user = UserORM(id=uuid.UUID(int=1), email="golden@example.com", password_hash="x")
        session.add(user)
        await session.flush()

        pipeline = OptionChainIngestionPipeline(
            instrument_service=InstrumentService(session),
            repository=MarketDataRepository(session),
            code_commit="golden",
        )
        request = OptionChainIngestionRequest(
            user_id=user.id,
            underlying=UnderlyingSpec(symbol="NIFTY", exchange="SYNTH", currency="INR"),
            as_of=AS_OF,
            column_mapping=ColumnMapping(mapping=dict(COLUMN_MAPPING)),
            contract=ContractSpec(
                multiplier=Decimal(75),
                tick_size=Decimal("0.05"),
                lot_size=Decimal(75),
                expiry_time_utc=time(10, 0),
            ),
            risk_free_rate=0.065,
            dividend_yield=0.0,
            dataset_digest="golden",
            provider="csv",
        )
        result = await pipeline.ingest(data, request)
        rows = await MarketDataRepository(session).get_option_quotes(result.results.snapshot_id)
        payload = _canonicalise(result, rows)
        await session.rollback()

    await engine.dispose()
    return payload


def _canonicalise(result, rows) -> dict:
    """Everything that a formula change would move; nothing that time moves."""
    summary = result.results
    return {
        "status": str(result.status),
        "counts": {
            "input": summary.rows_input,
            "kept": summary.rows_kept,
            "excluded": summary.rows_excluded,
            "rejected": summary.rows_rejected,
        },
        "exclusion_counts": dict(sorted(summary.exclusion_counts.items())),
        "rejection_counts": dict(sorted(summary.rejection_counts.items())),
        "flag_counts": dict(sorted(summary.flag_counts.items())),
        "aggregate_quality": {
            key: round(value, SCORE_PRECISION)
            for key, value in summary.aggregate_quality.to_dict().items()
            if key != "flags"
        },
        "warning_codes": sorted({warning.code for warning in result.warnings}),
        "model_versions": dict(sorted(result.provenance.model_versions.items())),
        "quotes": [
            {
                "expiry": str(row.expiry),
                "strike": format(row.strike, "f"),
                "option_type": row.option_type,
                "source_row_number": row.source_row_number,
                "excluded": row.excluded,
                "exclusion_reason": row.exclusion_reason,
                "scores": {
                    "stale": round(row.stale_score, SCORE_PRECISION),
                    "spread": round(row.spread_score, SCORE_PRECISION),
                    "liquidity": round(row.liquidity_score, SCORE_PRECISION),
                    "consistency": round(row.consistency_score, SCORE_PRECISION),
                    "completeness": round(row.completeness_score, SCORE_PRECISION),
                    "overall": round(row.overall_score, SCORE_PRECISION),
                },
                "flags": sorted(flag["code"] for flag in row.quality_flags),
            }
            for row in rows
        ],
    }


def run_case(name: str) -> dict:
    data = (DATA_DIR / f"{name}.csv").read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        return asyncio.run(_ingest(data, f"sqlite+aiosqlite:///{tmp}/golden.db"))


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"expected_ingestion_{name}.json"


def load_golden(name: str) -> dict:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def write_golden(name: str, payload: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path(name).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


CASES = ("options_chain_clean", "options_chain_bad_quotes")
