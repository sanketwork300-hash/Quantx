"""Execution and transaction-cost schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator, model_validator

from api.schemas.common import APIModel, DecimalStr
from domains.execution.benchmarks import BenchmarkKind
from domains.execution.models import Side
from domains.instruments.enums import AssetClass, ExerciseStyle, SettlementType

MAX_PREVIEW_ROWS = 1_000


class TradeImportDefaultsIn(APIModel):
    """What the trade log does not say, stated rather than guessed.

    `multiplier` has no default: an assumed contract multiplier rescales every
    cost in the analysis. Left unset, the importer records `MULTIPLIER_ASSUMED`
    on the contracts it creates.
    """

    currency: str = Field(default="INR", min_length=3, max_length=3)
    exchange: str | None = Field(default=None, max_length=32)
    asset_class: AssetClass | None = None
    multiplier: DecimalStr | None = None
    tick_size: DecimalStr = Decimal("0.05")
    lot_size: DecimalStr = Decimal("1")
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_type: SettlementType = SettlementType.CASH
    create_missing_instruments: bool = True
    broker: str | None = Field(default=None, max_length=64)
    #: How far apart two fills may be and still be one inferred parent order.
    #: Changing it changes the parents, the windows and every benchmark.
    parent_gap_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)

    def to_payload(self) -> dict[str, Any]:
        return {
            "currency": self.currency.upper(),
            "exchange": self.exchange,
            "asset_class": str(self.asset_class) if self.asset_class else None,
            "multiplier": (format(self.multiplier, "f") if self.multiplier is not None else None),
            "tick_size": format(self.tick_size, "f"),
            "lot_size": format(self.lot_size, "f"),
            "exercise_style": str(self.exercise_style),
            "settlement_type": str(self.settlement_type),
            "create_missing_instruments": self.create_missing_instruments,
            "broker": self.broker,
            "parent_gap_seconds": self.parent_gap_seconds,
        }


class TradeImportPreviewRequest(APIModel):
    upload_id: uuid.UUID
    column_mapping: dict[str, str] | None = None
    defaults: TradeImportDefaultsIn = TradeImportDefaultsIn()
    limit: int | None = Field(default=None, ge=1, le=MAX_PREVIEW_ROWS)


class TradeImportCommitRequest(APIModel):
    upload_id: uuid.UUID
    column_mapping: dict[str, str]
    defaults: TradeImportDefaultsIn = TradeImportDefaultsIn()


class TradeImportPreviewOut(APIModel):
    """Three buckets. `committable` is false while any row is ambiguous."""

    upload_id: uuid.UUID
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    rows_in: int
    committable: bool
    resolved: list[dict[str, Any]]
    ambiguous: list[dict[str, Any]]
    invalid: list[dict[str, Any]]


class AnalyseExecutionsRequest(APIModel):
    start: datetime | None = None
    end: datetime | None = None
    instrument_id: uuid.UUID | None = None
    parent_order_key: str | None = Field(default=None, max_length=200)
    primary_benchmark: BenchmarkKind = BenchmarkKind.ARRIVAL
    #: Only used for fills the file did not assign a parent. Recorded on every
    #: report, because a different gap gives different answers.
    parent_gap_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    #: A quote older than this is not "the prevailing mid"; it is the last thing
    #: the platform happened to see, and benchmarks using it are flagged.
    staleness_tolerance_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    #: How far outside the execution window to look for observations. Arrival
    #: looks backwards and close looks forwards, so both need the padding.
    window_padding_seconds: float = Field(default=3600.0, ge=0.0, le=604_800.0)


class ExecutionOut(APIModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    side: str
    quantity: DecimalStr
    execution_price: DecimalStr
    exchange_timestamp: datetime
    order_id: str | None = None
    parent_order_key: str | None = None
    order_type: str
    limit_price: DecimalStr | None = None
    order_quantity: DecimalStr | None = None
    submit_timestamp: datetime | None = None
    decision_timestamp: datetime | None = None
    broker: str | None = None
    venue: str | None = None
    fees: DecimalStr
    source: str
    created_at: datetime


class ExecutionReportOut(APIModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    parent_order_key: str
    grouping_method: str
    #: True when the platform grouped the fills itself. It changes what every
    #: number below means, so it is on the summary and not buried.
    grouping_is_inferred: bool
    side: str
    canonical_key: str | None = None
    currency: str
    multiplier: DecimalStr
    fills: int
    filled_quantity: DecimalStr
    order_quantity: DecimalStr | None = None
    average_price: DecimalStr
    fees: DecimalStr
    window_start: datetime
    window_end: datetime
    primary_benchmark: str
    primary_benchmark_price: DecimalStr | None = None
    #: Null when no benchmark was available. That is an absence, not a cost of
    #: zero, and the two must not render the same way.
    shortfall_currency: float | None = None
    shortfall_bps: float | None = None
    shortfall_percent: float | None = None
    observations: int
    coverage_span_ratio: float
    coverage_is_sufficient: bool
    warnings: list[str] = []
    created_at: datetime


# ------------------------------------------------------------------ simulation
#: More slices than this is a resource limit, not a modelling one.
MAX_INTERVALS = 200


class SimulateRequest(APIModel):
    """A counterfactual run. Every input is supplied or declared.

    The volume, spread and per-interval volatility inputs do **not** come from
    the platform: it holds no intraday profile of its own. A strategy that needs
    one and does not get it reports itself unavailable with a reason rather than
    assuming the day is flat, which would silently turn a VWAP into a TWAP.
    """

    instrument_id: uuid.UUID
    side: Side
    quantity: DecimalStr = Field(gt=0)
    start: datetime
    end: datetime
    intervals: int = Field(default=6, ge=1, le=MAX_INTERVALS)

    strategies: list[str] = ["TWAP"]
    #: Coefficients default to the identity, which is not a calibration. Every
    #: result computed that way is flagged `IMPACT_COEFFICIENT_NOT_CALIBRATED`.
    impact_model: str = Field(default="SquareRootImpactModel", max_length=64)
    permanent_coefficient: float = Field(default=1.0, ge=0.0, le=100.0)
    temporary_coefficient: float = Field(default=1.0, ge=0.0, le=100.0)

    volatility: float = Field(default=0.2, gt=0.0, le=10.0)
    average_daily_volume: float = Field(gt=0.0)
    lot_size: DecimalStr = Decimal("1")

    expected_volumes: list[float] | None = Field(default=None, max_length=MAX_INTERVALS)
    spreads: list[float] | None = Field(default=None, max_length=MAX_INTERVALS)
    volatilities: list[float] | None = Field(default=None, max_length=MAX_INTERVALS)

    participation_rate: float = Field(default=0.10, gt=0.0, le=1.0)
    latency_seconds: float = Field(default=0.0, ge=0.0, le=3_600.0)
    #: A slice whose nearest observation is older than this is left unfilled
    #: rather than filled at a stale price, because a hypothetical fill against
    #: an hours-old quote asserts liquidity nobody saw.
    max_price_age_seconds: float | None = Field(default=None, gt=0.0, le=604_800.0)
    window_padding_seconds: float = Field(default=3600.0, ge=0.0, le=604_800.0)
    staleness_tolerance_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)

    @field_validator("strategies")
    @classmethod
    def _at_least_one(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one strategy is required")
        return value

    @model_validator(mode="after")
    def _sequences_match_intervals(self) -> SimulateRequest:
        if self.end <= self.start:
            raise ValueError("a simulation window must end after it starts")
        for name in ("expected_volumes", "spreads", "volatilities"):
            values = getattr(self, name)
            if values is not None and len(values) != self.intervals:
                raise ValueError(
                    f"{name} has {len(values)} value(s) for {self.intervals} "
                    "interval(s); a per-interval input must cover every interval "
                    "or the schedule silently assumes the rest"
                )
            if values is not None and any(item < 0 for item in values):
                raise ValueError(f"{name} cannot contain a negative value")
        return self


class StrategyOut(APIModel):
    name: str
    version: str
    description: str
    requires: list[str] = []


class ImpactModelOut(APIModel):
    name: str
    version: str
    description: str
    #: False for every model here until the caller supplies a coefficient
    #: measured on their own executions.
    ships_calibrated_coefficients: bool = False


class SimulationOut(APIModel):
    id: uuid.UUID
    comparison_id: uuid.UUID
    instrument_id: uuid.UUID
    #: Always true, and a database CHECK makes anything else unstorable.
    counterfactual: bool
    strategy: str
    impact_model: str
    impact_is_calibrated: bool
    side: str
    ordered_quantity: DecimalStr
    filled_quantity: DecimalStr
    completion_rate: float
    average_price: DecimalStr | None = None
    window_start: datetime
    window_end: datetime
    latency_seconds: float
    max_price_age_seconds: float
    modelled_impact_cost: DecimalStr
    modelled_spread_cost: DecimalStr
    primary_benchmark: str | None = None
    shortfall_currency: float | None = None
    shortfall_bps: float | None = None
    warnings: list[str] = []
    created_at: datetime
