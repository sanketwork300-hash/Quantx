"""Risk and scenario request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from pydantic import Field, field_validator

from api.schemas.common import APIModel
from domains.risk.var import VaRMethod
from domains.scenarios.models import RiskFactorKind, ScenarioSource, ShockType
from quant.simulation.paths import Distribution

#: A Monte Carlo run reprices the whole book once per path. The cap is a
#: resource limit, not a statistical one, and it is stated as such.
MAX_PATHS = 200_000
#: Beyond this the lookback exceeds any history the platform plausibly holds.
MAX_LOOKBACK = 5_000


class ShockIn(APIModel):
    kind: RiskFactorKind
    shock_type: ShockType
    value: float
    #: Underlying id or currency pair. Omit for "every factor of this kind".
    target: str | None = None


class ShockOut(APIModel):
    kind: str
    shock_type: str
    value: float
    target: str | None = None
    label: str


class ScenarioCreateRequest(APIModel):
    name: str = Field(min_length=1, max_length=120)
    shocks: list[ShockIn] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=1000)


class DeriveScenarioRequest(APIModel):
    """Compute a scenario from a series this platform actually holds.

    There is no way to assert a historical event by hand: a scenario is only
    labelled historical when it was derived here, from data, and it then carries
    the series and the date the move came from.
    """

    name: str = Field(min_length=1, max_length=120)
    underlying_id: uuid.UUID
    window_days: int = Field(default=1, ge=1, le=250)
    #: Omit for the worst move in the series; supply one for a quantile of it.
    percentile: float | None = Field(default=None, gt=0.0, lt=1.0)
    lookback: int | None = Field(default=None, ge=2, le=MAX_LOOKBACK)
    include_volatility: bool = True


class DerivationOut(APIModel):
    series: str
    observations: int
    start_date: str
    end_date: str
    event_date: str
    window_days: int
    method: str


class ScenarioOut(APIModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    source: ScenarioSource
    shocks: list[ShockOut]
    derivation: DerivationOut | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] = {}


class RiskRunRequest(APIModel):
    """Shared parameters for every risk run."""

    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)
    #: Without it, time to expiry is undefined, so options carry no Greeks and
    #: cannot be repriced. Risk runs need it in practice.
    settlement_time_utc: time | None = None
    as_of: datetime | None = None
    lookback: int | None = Field(default=None, ge=2, le=MAX_LOOKBACK)
    include_volatility_factor: bool = True


class VaRRequest(RiskRunRequest):
    method: VaRMethod = VaRMethod.HISTORICAL
    horizon_days: int = Field(default=1, ge=1, le=250)
    confidences: list[float] = [0.95, 0.99]
    paths: int = Field(default=10_000, ge=2, le=MAX_PATHS)
    seed: int = Field(default=20_260_924, ge=0)
    distribution: Distribution = Distribution.NORMAL
    degrees_of_freedom: float = Field(default=5.0, gt=2.0, le=100.0)

    @field_validator("confidences")
    @classmethod
    def _valid_confidences(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("at least one confidence level is required")
        for level in value:
            if not 0.0 < level < 1.0:
                raise ValueError(f"confidence must be in (0, 1), got {level}")
        return sorted(set(value))


class StressRequest(RiskRunRequest):
    #: A template name, a stored scenario's name, or either one's id.
    scenario: str = Field(min_length=1, max_length=120)
    #: Days of time decay to apply along with the shock. Zero means an
    #: instantaneous move, which is what a stress test usually means.
    time_decay_days: float = Field(default=0.0, ge=0.0, le=365.0)


class TailRiskOut(APIModel):
    confidence: float
    value_at_risk: float
    expected_shortfall: float
    observations: int
    tail_observations: int
    quantile_method: str
    mean_loss: float
    worst_loss: float
    is_reliable: bool
    warnings: list[str] = []
    interpretation: dict[str, str] = {}


class VaRSummaryOut(APIModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    snapshot_id: uuid.UUID
    method: str
    horizon_days: int
    scenarios: int
    base_value: float
    seed: int | None = None
    tail_risk: list[TailRiskOut] = []
    warnings: list[str] = []
    created_at: datetime


class StressSummaryOut(APIModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    snapshot_id: uuid.UUID
    scenario_name: str
    scenario_source: str
    base_value: float
    shocked_value: float
    pnl: float
    greek_estimate: float
    time_decay_days: float
    created_at: datetime


class RiskSnapshotOut(APIModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    valuation_id: uuid.UUID
    as_of_timestamp: datetime
    base_currency: str
    market_state_id: str | None = None
    positions: int
    excluded_positions: int
    base_value: float
    reported_value: float
    delta: float
    gamma: float
    vega_per_vol_point: float
    theta_per_day: float
    rho_per_bp: float
    excluded: list[dict[str, Any]] = []
    created_at: datetime


# ---------------------------------------------------------------------- margin
#: More rungs than this on either the grid or the ladder is a resource limit,
#: not a statistical one.
MAX_GRID_POINTS = 200
MAX_LADDER_POINTS = 200


class MarginGridIn(APIModel):
    """The grid a scan-based model measures its worst loss over.

    Declared explicitly because the grid *is* the model: a margin number is the
    worst loss over the moves someone chose to look at, and a reader who cannot
    see those moves cannot judge the number.
    """

    spot_returns: list[float] | None = Field(default=None, max_length=MAX_GRID_POINTS)
    vol_points: list[float] | None = Field(default=None, max_length=MAX_GRID_POINTS)

    @field_validator("spot_returns")
    @classmethod
    def _must_include_no_move(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and 0.0 not in value:
            raise ValueError(
                "a shock grid needs an unshocked point, or it cannot show that "
                "the book is flat where the market actually is"
            )
        return value


class MarginRequest(RiskRunRequest):
    """Estimate margin under a named model, and scan the buffer ladder.

    `short_option_minimum_rate` and `concentration_add_on_rate` default to zero
    on purpose. A short option far out of the money shows almost no loss on a
    scan grid while carrying unbounded tail risk, and a real margin system
    floors it for that reason — but the *rate* at which it does is a venue's
    rule. Picking a plausible number here would be inventing one, so the default
    is zero and the response says what that leaves out.
    """

    margin_model: str = Field(default="SimpleRiskMarginModel", max_length=64)
    grid: MarginGridIn | None = None
    short_option_minimum_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    concentration_add_on_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    concentration_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    #: Capital available to meet margin. Omitted means unknown, and then
    #: utilisation and buffer are null rather than defaulted to portfolio value.
    eligible_capital: float | None = Field(default=None, gt=0.0)
    ladder: list[float] | None = Field(default=None, max_length=MAX_LADDER_POINTS)
    vol_co_shock: float = Field(default=0.0, ge=-1.0, le=1.0)


class MarginSummaryOut(APIModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    snapshot_id: uuid.UUID
    method: str
    model_version: str
    currency: str
    estimated_margin: float
    confidence: float
    eligible_capital: float | None = None
    buffer: float | None = None
    utilisation: float | None = None
    in_shortfall_at_rest: bool
    vol_co_shock: float
    worst_spot_return: float
    worst_vol_points: float
    worst_loss: float
    worst_at_grid_edge: bool
    positions: int
    excluded_positions: int
    summary: str
    warnings: list[str] = []
    created_at: datetime


class MarginModelOut(APIModel):
    """What a model is, and what it explicitly is not."""

    name: str
    version: str
    description: str
    is_broker_equivalent: bool = False
