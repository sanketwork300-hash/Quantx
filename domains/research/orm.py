"""The experiment record.

A backtest is a claim, and a claim nobody can reproduce is an anecdote. Build
spec §16 lists what has to be recorded for it to be more than that — dataset,
window, strategy, parameters, features, costs, slippage, results, code version,
data version — and every one of those is a column here.

The equity curve and the fills live in the object store rather than in a column.
They grow with the length of the run, which is the same reason a tick tape does
not go in PostgreSQL, and the summary in the row is complete without them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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


class ResearchExperimentORM(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One backtest run, with everything needed to run it again."""

    __tablename__ = "research_experiments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # ---------------------------------------------------------------- inputs
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("warehouse_datasets.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    start_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    end_timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(24), nullable=False)
    strategy_parameters: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    #: The features the strategy declared it needed. Recorded so a run is
    #: rebuildable from the row rather than from code that has since moved.
    features: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    engine_config: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    #: The fee schedule, verbatim. A net return means nothing without a
    #: statement of what was deducted from it.
    cost_schedule: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    slippage_model: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    # ------------------------------------------------------------ provenance
    code_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The dataset digest the run read. Two experiments quoting the same digest
    #: provably saw the same bars.
    data_digest: Mapped[str | None] = mapped_column(String(64))

    # --------------------------------------------------------------- results
    initial_equity: Mapped[object] = mapped_column(DecimalType(), nullable=False)
    final_equity: Mapped[object] = mapped_column(DecimalType(), nullable=False)
    total_costs: Mapped[object] = mapped_column(DecimalType(), nullable=False)
    total_slippage: Mapped[object] = mapped_column(DecimalType(), nullable=False)
    #: True when no cost schedule was supplied, so every return is gross. A
    #: column rather than something to infer, because it changes what the
    #: neighbouring numbers mean.
    gross_of_costs: Mapped[bool] = mapped_column(nullable=False, default=True)

    total_return: Mapped[float | None] = mapped_column(Float)
    cagr: Mapped[float | None] = mapped_column(Float)
    sharpe: Mapped[float | None] = mapped_column(Float)
    sortino: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)

    bars_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bars_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    traded_notional: Mapped[object] = mapped_column(DecimalType(), nullable=False)

    metrics: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    attribution: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSONDict, nullable=False, default=list)
    provenance: Mapped[dict] = mapped_column(JSONDict, nullable=False, default=dict)

    #: Object-store keys. The curve and the fills grow with the run's length,
    #: which is the same reason a tick tape is not a column.
    equity_curve_key: Mapped[str | None] = mapped_column(String(512))
    fills_key: Mapped[str | None] = mapped_column(String(512))
    bytes_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        Index("ix_research_experiments_user", "user_id", "created_at"),
        Index("ix_research_experiments_strategy", "strategy_name", "instrument_id"),
    )
