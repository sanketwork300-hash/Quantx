from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Any

from pydantic import Field, model_validator

from api.schemas.common import APIModel, DecimalStr
from quant.daycount import DEFAULT_DAY_COUNT, DayCount


# ------------------------------------------------------------- chain analysis
class AnalyzeChainRequest(APIModel):
    """Everything the analysis needs that the chain snapshot does not carry."""

    #: Discount rate, continuously compounded. Stated as an assumption in the
    #: result unless a real curve is supplied (curve upload is Phase 2).
    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield_assumed: bool = True
    #: Settlement time on the expiry date. Without it, time to expiry is
    #: undefined and no implied volatility can be solved.
    settlement_time_utc: time | None = None
    day_count: DayCount = DEFAULT_DAY_COUNT
    #: Quotes the quality engine excluded stay out by default; a quote set aside
    #: for a reason should not silently enter a calibration.
    include_excluded_quotes: bool = False


class ForwardEstimateOut(APIModel):
    method: str
    selected: bool
    value: float | None
    confidence: float
    observations: int
    residual_error: float | None = None
    discount_factor: float | None = None
    error: str | None = None
    assumptions: list[str] = []


class ImpliedVolPointOut(APIModel):
    instrument_id: uuid.UUID
    expiry: date
    strike: DecimalStr
    option_type: str
    price_used: float | None = None
    price_source: str
    #: Implied by the observed price. The fitted reference IV is a separate
    #: field that arrives in Phase 2 and is never written from this one.
    market_iv: float | None = None
    market_iv_bid: float | None = None
    market_iv_ask: float | None = None
    iv_envelope_width: float | None = None
    converged: bool
    iterations: int
    solver: str
    error: str | None = None
    vega: float | None = None
    #: Volatility uncertainty implied by one price ulp; do not display more
    #: precision than this allows.
    uncertainty: float | None = None
    time_to_expiry: float | None = None
    log_moneyness: float | None = None
    total_variance: float | None = None
    weight: float = 0.0
    used_for_smile: bool = False
    smile_exclusion: str | None = None


class SliceCountsOut(APIModel):
    quotes: int
    solved: int
    used_for_smile: int


class SmileSliceOut(APIModel):
    expiry: date
    time_to_expiry: float | None = None
    settlement_time_assumed: bool = False
    forward: dict[str, Any]
    counts: SliceCountsOut
    solve_failures: dict[str, int] = {}
    atm_volatility: float | None = None
    skew: float | None = None
    curvature: float | None = None
    reason: str | None = None
    points: list[ImpliedVolPointOut] = []


class ChainAnalysisCountsOut(APIModel):
    quotes: int
    solved: int
    expiries: int


class ChainAnalysisOut(APIModel):
    analysis_id: uuid.UUID | None = None
    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    underlying_price: DecimalStr | None = None
    curve_id: str | None = None
    counts: ChainAnalysisCountsOut
    slices: list[SmileSliceOut] = []


class AnalysisSummaryOut(APIModel):
    analysis_id: uuid.UUID
    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    curve_id: str | None = None
    quotes_in: int
    quotes_solved: int
    expiries: int
    created_at: datetime


# -------------------------------------------------------- stateless calculators
class _SpotOrForward(APIModel):
    spot: float | None = Field(default=None, gt=0)
    forward: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _require_one(self):
        if self.spot is None and self.forward is None:
            raise ValueError("supply either 'spot' or 'forward'")
        return self


class ImpliedVolRequest(_SpotOrForward):
    price: float = Field(gt=0)
    strike: float = Field(gt=0)
    time_to_expiry: float = Field(gt=0, description="Year fraction under the stated day count")
    is_call: bool
    rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)


class GreeksRequest(_SpotOrForward):
    strike: float = Field(gt=0)
    time_to_expiry: float = Field(gt=0)
    sigma: float = Field(gt=0, le=10.0)
    is_call: bool
    rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)


class ForwardRequest(APIModel):
    time_to_expiry: float = Field(gt=0)
    spot: float | None = Field(default=None, gt=0)
    rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield_assumed: bool = True
    future_price: float | None = Field(default=None, gt=0)
    #: Aligned arrays of strikes with their call and put mid prices, for the
    #: put-call-parity regression.
    strikes: list[float] | None = None
    call_prices: list[float] | None = None
    put_prices: list[float] | None = None

    @model_validator(mode="after")
    def _check_parity_arrays(self):
        arrays = [self.strikes, self.call_prices, self.put_prices]
        supplied = [a for a in arrays if a is not None]
        if supplied and len(supplied) != 3:
            raise ValueError(
                "put-call-parity needs 'strikes', 'call_prices' and 'put_prices' together"
            )
        if supplied and len({len(a) for a in arrays}) != 1:
            raise ValueError("strikes, call_prices and put_prices must be the same length")
        if not supplied and self.spot is None and self.future_price is None:
            raise ValueError(
                "supply a spot, a future price, or put/call prices to estimate a forward"
            )
        return self


# ------------------------------------------------------------------ surfaces
class CalibrateSurfaceRequest(APIModel):
    """Calibration is deterministic: the same analysis and seed refit identically."""

    seed: int = Field(default=20_260_924, ge=0)
    #: Weight each quote by its spread and liquidity scores from the quality
    #: engine, so a wide thin quote does not drag the slice.
    use_weights: bool = True


class SVIParametersOut(APIModel):
    a: float
    b: float
    rho: float
    m: float
    sigma: float


class CalibrationMetricsOut(APIModel):
    status: str
    n_observations: int
    rmse_total_variance: float | None = None
    weighted_rmse: float | None = None
    rmse_vol_points: float | None = None
    max_error_vol_points: float | None = None
    optimizer: str = "SLSQP"
    optimizer_message: str | None = None
    iterations: int = 0
    starts_attempted: int = 0
    starts_feasible: int = 0
    #: Minimum of Durrleman's g. Negative means a negative implied density.
    min_durrleman_g: float | None = None
    min_durrleman_k: float | None = None
    wing_slope: float | None = None
    constraints_satisfied: bool = False
    error: str | None = None


class SurfaceSliceOut(APIModel):
    expiry: date
    time_to_expiry: float
    forward: float
    discount_factor: float
    forward_method: str | None = None
    forward_confidence: float = 0.0
    #: Log-moneyness actually fitted. Outside it, a lookup is extrapolation.
    k_min: float | None = None
    k_max: float | None = None
    parameters: SVIParametersOut | None = None
    calibration: CalibrationMetricsOut


class SurfaceOut(APIModel):
    surface_id: str
    surface_row_id: uuid.UUID | None = None
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    model: str
    model_version: str
    curve_id: str | None = None
    analysis_id: uuid.UUID | None = None
    counts: dict[str, int]
    slices: list[SurfaceSliceOut] = []


class SurfaceSummaryOut(APIModel):
    surface_row_id: uuid.UUID
    surface_id: str
    underlying_id: uuid.UUID
    analysis_id: uuid.UUID
    as_of_timestamp: datetime
    model: str
    model_version: str
    curve_id: str | None = None
    slices_total: int
    slices_fitted: int
    created_at: datetime


class ReferenceRequestItem(APIModel):
    strike: DecimalStr
    expiry: date
    option_type: str | None = Field(default=None, pattern="^(CALL|PUT)$")


class ReferenceRequest(APIModel):
    requests: list[ReferenceRequestItem] = Field(min_length=1, max_length=5000)


class ReferencePointOut(APIModel):
    strike: DecimalStr
    expiry: date
    option_type: str | None = None
    #: EXACT_SLICE, INTERPOLATED_MATURITY, EXTRAPOLATED_MATURITY or UNAVAILABLE.
    method: str
    time_to_expiry: float | None = None
    forward: float | None = None
    discount_factor: float | None = None
    log_moneyness: float | None = None
    #: A model output. Never a fair value, and never written over a market IV.
    reference_iv: float | None = None
    total_variance: float | None = None
    reference_price: float | None = None
    calibration_rmse_vol_points: float | None = None
    flags: list[str] = []
    error: str | None = None


# ----------------------------------------------------------------- arbitrage
class ArbitrageViolationOut(APIModel):
    scope: str
    type: str
    severity: str
    #: How far the condition is breached, in that condition's own units.
    magnitude: float
    #: What the magnitude was judged against, in the same units.
    tolerance: float | None = None
    expiry: date | None = None
    strike: DecimalStr | None = None
    option_type: str | None = None
    detail: dict[str, Any] = {}
    affected_instruments: list[str] = []


class ArbitrageReportOut(APIModel):
    scope: str
    severity: str | None = None
    violations_total: int
    observations: int
    checks_run: list[str] = []
    summary: dict[str, Any] = {}
    violations: list[ArbitrageViolationOut] = []


class ArbitrageOut(APIModel):
    analysis_id: uuid.UUID
    #: Raw-market and fitted-surface findings are never merged: a smooth fit
    #: must not be able to hide a broken market, and a broken fit must not be
    #: reported as a market anomaly.
    raw_market: ArbitrageReportOut | None = None
    fitted_surface: ArbitrageReportOut | None = None


# ------------------------------------------------------------------ anomalies
class ScanAnomaliesRequest(APIModel):
    """The detection policy. It decides the answer, so it is recorded."""

    #: Minimum standardised deviation: how many times the difference exceeds
    #: everything that could explain it (bid/ask width, calibration error,
    #: measurement resolution).
    min_z_score: float = Field(default=2.0, ge=0.0, le=100.0)
    #: Require that the market's own two-sided quote does not already account
    #: for the difference.
    require_outside_envelope: bool = True
    min_confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    min_liquidity: float = Field(default=0.05, ge=0.0, le=1.0)


class ExplanationOut(APIModel):
    factor: str
    #: SUPPORTS, REDUCES or NEUTRAL — the effect on confidence, not on a trade.
    effect: str
    detail: str
    value: float | None = None


class SurfaceAnomalyOut(APIModel):
    instrument_id: uuid.UUID
    expiry: date
    strike: DecimalStr
    option_type: str

    #: Implied by the observed price.
    market_iv: float
    #: Produced by the fitted surface. A model output, never a fair value.
    reference_iv: float
    iv_difference: float
    iv_difference_vol_points: float
    relative_deviation: float

    market_iv_bid: float | None = None
    market_iv_ask: float | None = None
    envelope_position: str
    excess_over_envelope: float = 0.0

    #: The combined size of everything that could account for the difference.
    explained_scale: float
    z_score: float
    historical_z_score: float | None = None
    historical_observations: int = 0

    liquidity_score: float
    data_quality_score: float
    calibration_rmse_vol_points: float | None = None
    iv_uncertainty: float | None = None
    reference_method: str
    reference_flags: list[str] = []

    confidence: float
    flagged: bool
    #: Grounded reasons, each naming the measurement behind it.
    explanation: list[ExplanationOut] = []


class AnomalyScanOut(APIModel):
    scan_id: uuid.UUID
    surface_id: str
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    counts: dict[str, int]
    policy: dict[str, Any]
    anomalies: list[SurfaceAnomalyOut] = []


class AnomalyScanSummaryOut(APIModel):
    scan_id: uuid.UUID
    surface_id: uuid.UUID
    analysis_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    quotes_examined: int
    quotes_scored: int
    flagged: int
    created_at: datetime


# -------------------------------------------------------------------- history
class DistributionOut(APIModel):
    count: int
    mean: float | None = None
    std: float | None = None
    min: float | None = None
    max: float | None = None
    median: float | None = None
    p10: float | None = None
    p90: float | None = None
    is_reliable: bool


class CharacteristicPercentileOut(APIModel):
    name: str
    current: float | None = None
    percentile: float | None = None
    z_score: float | None = None
    distribution: DistributionOut
    is_reliable: bool


class TenorHistoryOut(APIModel):
    tenor_days: int
    as_of_timestamp: datetime | None = None
    observations: int
    is_reliable: bool
    #: Below this many observations a percentile is reported but not trusted.
    minimum_reliable_observations: int
    percentiles: list[CharacteristicPercentileOut] = []
    series: list[dict[str, Any]] = []


class SurfaceCharacteristicOut(APIModel):
    kind: str
    expiry: date | None = None
    tenor_days: int | None = None
    time_to_expiry: float
    forward: float
    atm_volatility: float
    skew: float
    curvature: float
    atm_total_variance: float
    method: str
    flags: list[str] = []
