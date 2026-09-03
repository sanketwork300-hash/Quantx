"""Unified order-analysis request and response schemas.

The response model is deliberately thin: the branch payloads are the domains'
own dictionaries, already shaped and already carrying their interpretations, and
restating them field by field here would give the API layer a second opinion
about what an analysis contains.

What this module *does* enforce is the phase's contract. There is no `action`,
`signal`, `rating`, `score`, `recommendation` or `verdict` field on any model
below, and a test walks the published OpenAPI schema for this route to assert
it. That is the guarantee: the shape of the response makes advice unsayable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from api.schemas.common import APIModel, DecimalStr, WarningOut
from api.schemas.risk import MarginGridIn
from domains.execution.models import OrderType, Side
from domains.risk.var import VaRMethod

#: A resource limit on the schedule, not a statistical one.
MAX_INTERVALS = 96
#: Eight hours. Longer than any single trading session this platform models.
MAX_HORIZON_SECONDS = 28_800.0
MAX_LOOKBACK = 5_000


class ExecutionAssumptionsIn(APIModel):
    """Everything the cost estimate needs and the platform does not hold.

    `average_daily_volume` and `volatility` have no defaults worth the name:
    the platform stores neither, and an impact model run without them reports
    the impact half of the estimate as absent rather than as zero. Supply them
    to get a number, and the number is then explicitly yours.
    """

    horizon_seconds: float = Field(default=1800.0, gt=0.0, le=MAX_HORIZON_SECONDS)
    intervals: int = Field(default=6, ge=1, le=MAX_INTERVALS)
    #: POV and VWAP can be asked for, but both need `expected_volumes`: without
    #: a profile they report themselves unavailable rather than becoming TWAP
    #: under another name, so they are not defaults.
    strategies: list[str] = Field(default=["IMMEDIATE", "TWAP"], min_length=1, max_length=8)
    impact_model: str = Field(default="SquareRootImpactModel", max_length=64)
    #: Both default to the identity. Every result computed at the identity is
    #: flagged uncalibrated, because one is not the right answer — it is the
    #: value that asserts nothing.
    permanent_coefficient: float = Field(default=1.0, ge=0.0, le=100.0)
    temporary_coefficient: float = Field(default=1.0, ge=0.0, le=100.0)
    volatility: float = Field(default=0.0, ge=0.0, le=10.0)
    average_daily_volume: float = Field(default=0.0, ge=0.0)
    participation_rate: float = Field(default=0.10, gt=0.0, le=1.0)
    #: One expected volume per interval, when the caller has a profile. Without
    #: one, VWAP reports itself unavailable rather than becoming TWAP wearing a
    #: volume-weighted name.
    expected_volumes: list[float] | None = Field(default=None, max_length=MAX_INTERVALS)


class MarginAssumptionsIn(APIModel):
    """The margin model to run on both sides of the comparison."""

    margin_model: str = Field(default="SimpleRiskMarginModel", max_length=64)
    grid: MarginGridIn | None = None
    short_option_minimum_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    concentration_add_on_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    concentration_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Omitted means unknown, and then buffer and utilisation are null rather
    #: than defaulted to the portfolio's value, which is a different quantity.
    eligible_capital: float | None = Field(default=None, gt=0.0)
    vol_co_shock: float = Field(default=0.0, ge=-1.0, le=1.0)


class OrderAnalysisRequestIn(APIModel):
    """One proposed order, and every parameter that decides the answer."""

    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    side: Side
    #: A magnitude in contracts. The side carries the sign, in one place.
    quantity: DecimalStr
    order_type: OrderType = OrderType.MARKET
    limit_price: DecimalStr | None = None

    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)
    #: Without it, time to expiry is undefined, so options carry no Greeks and
    #: cannot be repriced.
    settlement_time_utc: time | None = None
    as_of: datetime | None = None

    var_method: VaRMethod = VaRMethod.HISTORICAL
    #: A template name, a stored scenario's name, or either one's id. Omitted
    #: means no stress comparison is run, and the response says so.
    scenario: str | None = Field(default=None, max_length=120)
    lookback: int | None = Field(default=None, ge=2, le=MAX_LOOKBACK)
    horizon_days: int = Field(default=1, ge=1, le=250)
    seed: int = Field(default=20_260_924, ge=0)

    execution: ExecutionAssumptionsIn = ExecutionAssumptionsIn()
    margin: MarginAssumptionsIn = MarginAssumptionsIn()
    #: What counts as a surface deviation worth flagging. It decides the answer,
    #: so it is a request parameter and is recorded in provenance.
    min_deviation_z_score: float = Field(default=2.0, gt=0.0, le=20.0)

    @field_validator("quantity")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("an order quantity is a magnitude; the side carries the sign")
        return value


class OrderAnalysisSummaryOut(APIModel):
    """One stored analysis, as a list row.

    Note what is not here: nothing summarising the five branches into a single
    figure of merit. The branch statuses are counts of what could be computed,
    not a score.
    """

    id: uuid.UUID
    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    side: str
    quantity: DecimalStr
    order_type: str
    limit_price: DecimalStr | None = None
    as_of_timestamp: datetime | None = None
    market_state_id: str | None = None
    base_currency: str | None = None
    status: str
    branches_ok: int
    branches_failed: int
    branch_status: dict[str, str] = {}
    created_at: datetime


class OrderAnalysisOut(APIModel):
    """A stored analysis in full, as it was returned when it was computed."""

    id: uuid.UUID
    status: str
    results: dict[str, Any] | None = None
    warnings: list[WarningOut] = []
    provenance: dict[str, Any] = {}
