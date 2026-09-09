"""Research: strategies, backtests and the experiment record."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from api.schemas.common import APIModel
from domains.research.models import ExecutionTiming


class CostComponentIn(APIModel):
    """One line of a fee schedule, supplied by whoever knows the rates.

    The platform ships no default rates. Brokerage, exchange charges, STT, stamp
    duty and GST are set by brokers, exchanges and regulators, they differ by
    segment and side, and they change — inventing them would put a net return in
    front of a user that is wrong in a way nobody could detect.
    """

    name: str = Field(min_length=1, max_length=64)
    basis: str = Field(pattern="^(TURNOVER|PER_UNIT|PER_ORDER|ON_OTHER_COMPONENTS)$")
    rate: str
    side: str = Field(default="BOTH", pattern="^(BOTH|BUY|SELL)$")
    maximum: str | None = None
    minimum: str | None = None
    #: For ON_OTHER_COMPONENTS, which components this is charged on. Empty means
    #: all of them — GST on brokerage and exchange charges is written this way.
    applies_to: list[str] = Field(default_factory=list)


class RunBacktestRequest(APIModel):
    name: str = Field(min_length=1, max_length=200)
    instrument_id: uuid.UUID
    strategy_name: str = Field(min_length=1, max_length=64)
    strategy_parameters: dict = Field(default_factory=dict)
    start: datetime | None = None
    end: datetime | None = None
    exchange: str | None = Field(default=None, max_length=32)
    dataset_id: uuid.UUID | None = None
    initial_cash: str = "1000000"
    #: Both options are causal. There is no "same bar's close" — a decision that
    #: used a bar's close cannot also be filled at it.
    timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN
    cost_schedule_name: str | None = Field(default=None, max_length=64)
    cost_schedule_source: str | None = Field(default=None, max_length=256)
    cost_components: list[CostComponentIn] = Field(default_factory=list)
    #: Adverse basis points applied to every fill. Zero is legitimate and is
    #: labelled: fills at the reference price are optimistic by an unmeasured
    #: amount, not free.
    slippage_basis_points: str | None = None
    slippage_source: str | None = Field(default=None, max_length=256)
    max_position_weight: str = "1"
    max_gross_exposure: str = "1"
    include_flagged_bars: bool = False
    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)


class StrategyOut(APIModel):
    name: str
    version: str
    #: The features it declares it needs, so a caller can see what a run will
    #: depend on before running it.
    features: list[str]
    parameters: dict


class DrawdownOut(APIModel):
    depth: float
    peak_timestamp: datetime | None
    trough_timestamp: datetime | None
    #: Null when the curve never recovered — different from recovering instantly.
    recovery_bars: int | None


class TradeStatisticsOut(APIModel):
    count: int
    wins: int
    losses: int
    win_rate: float | None
    #: Null where nothing lost: an infinite profit factor is not a number worth
    #: printing beside finite ones.
    profit_factor: float | None
    average_trade: float | None
    best_trade: float | None
    worst_trade: float | None
    turnover: float | None


class MetricsOut(APIModel):
    observations: int
    is_reliable: bool
    years: float
    #: Measured from the median gap between bars, not assumed. Every annualised
    #: figure scales by its square root.
    periods_per_year: float
    risk_free_rate: float
    #: True when no cost schedule was supplied, so every return here is gross.
    gross_of_costs: bool
    total_return: float
    cagr: float | None
    annualised_volatility: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    max_drawdown: DrawdownOut
    value_at_risk: dict | None
    trades: TradeStatisticsOut
    benchmark: dict | None
    warnings: list[str]
    model_version: str
    interpretation: dict


class AttributionOut(APIModel):
    initial_equity: str
    final_equity: str
    equity_change: str
    realised_pnl: str
    unrealised_pnl: str
    costs: str
    #: Already inside the price P&L; reported to quantify it, not subtracted.
    slippage_against_reference: str
    attributed: str
    residual: str
    #: False means the decomposition does not add up to the equity change, which
    #: is a bug rather than an approximation.
    reconciles: bool
    identity: str
    model_version: str
    instruments: list[dict]
    not_attributed: dict


class ExperimentSummaryOut(APIModel):
    id: uuid.UUID
    name: str
    status: str
    instrument_id: uuid.UUID
    dataset_id: uuid.UUID | None
    strategy_name: str
    strategy_version: str
    start_timestamp: datetime
    end_timestamp: datetime
    initial_equity: str
    final_equity: str
    total_costs: str
    total_slippage: str
    gross_of_costs: bool
    total_return: float | None
    cagr: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    bars_in: int
    bars_used: int
    fill_count: int
    created_at: datetime


class ExperimentDetailOut(ExperimentSummaryOut):
    """Everything needed to run it again, and everything it produced."""

    strategy_parameters: dict
    features: list[str]
    engine_config: dict
    #: Verbatim. A net return means nothing without a statement of what was
    #: deducted from it.
    cost_schedule: dict
    slippage_model: dict
    code_commit: str
    data_digest: str | None
    traded_notional: str
    metrics: MetricsOut
    attribution: AttributionOut
    warnings: list[dict]
    provenance: dict


class EquityPointOut(APIModel):
    timestamp: datetime
    equity: str
    cash: str
    market_value: str
    gross_exposure: str
    costs: str
    slippage: str


class EquityCurveOut(APIModel):
    items: list[EquityPointOut]
    count: int


class FillOut(APIModel):
    instrument_id: uuid.UUID
    timestamp: datetime
    quantity: str
    price: str
    #: The price before slippage, so the two are never confused.
    reference_price: str
    notional: str
    slippage_cost: str
    cost: dict
    reason: str


class FillListOut(APIModel):
    items: list[FillOut]
    count: int
