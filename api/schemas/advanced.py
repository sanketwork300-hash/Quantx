"""Phase 9 schemas: global surface, local volatility, density, consensus.

The shape of :class:`ModelConsensusOut` is the phase's contract made visible.
It has a ``reference_value``, a ``reference_range`` and a ``model_dispersion``,
and it has no ``best_model``, no ``fair_value`` and no field that could hold
either — a test asserts the absence, because a field that does not exist cannot
be filled in later by someone who thought it would be helpful.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from api.schemas.common import APIModel, DecimalStr


class CalibrateGlobalSurfaceRequest(APIModel):
    """Calibration is deterministic: the same analysis and seed refit identically."""

    seed: int = Field(default=20_260_924, ge=0)
    use_weights: bool = True
    #: Impose Gatheral-Jacquier Theorem 4.2's *sufficient* butterfly bounds as
    #: constraints. They are strong: a steep short-dated smile can be admissible
    #: under Durrleman's condition and still fail them. Turning them off keeps
    #: Durrleman's condition, which is the one that decides the density's sign.
    enforce_butterfly_bounds: bool = True
    calibrate_heston: bool = True
    #: Require ``2 kappa theta > xi^2``. Off by default: real index surfaces
    #: routinely calibrate to parameter sets that violate it, and refusing them
    #: would mean refusing to describe the market.
    require_feller: bool = False
    build_local_volatility: bool = True
    build_densities: bool = True


class SSVIParametersOut(APIModel):
    rho: float
    eta: float
    gamma: float


class GlobalSurfaceSummaryOut(APIModel):
    global_surface_row_id: uuid.UUID
    surface_id: str
    underlying_id: uuid.UUID
    analysis_id: uuid.UUID
    as_of_timestamp: datetime
    model: str
    model_version: str
    status: str
    curve_id: str | None = None
    parameters: SSVIParametersOut | None = None
    n_slices: int
    n_observations: int
    rmse_vol_points: float | None = None
    min_durrleman_g: float | None = None
    max_butterfly_quantity: float | None = None
    butterfly_bounds_satisfied: bool
    #: For SSVI this is a structural property of the fit, not a diagnostic that
    #: happened to pass: a non-decreasing variance term structure cannot contain
    #: calendar arbitrage.
    calendar_arbitrage_free: bool
    created_at: datetime


class LocalVolatilitySummaryOut(APIModel):
    local_volatility_row_id: uuid.UUID
    global_surface_row_id: uuid.UUID
    as_of_timestamp: datetime
    model_version: str
    spot: float
    carry: float
    total_points: int
    valid_points: int
    #: Grid points where Dupire's formula produced nothing. Kept as holes with
    #: their reasons rather than interpolated over.
    flagged_points: int
    coverage: float
    flag_counts: dict[str, int] = {}
    log_moneyness: list[float] = []
    maturities: list[float] = []
    values: list[list[float | None]] = []


class DensityOut(APIModel):
    density_row_id: uuid.UUID
    expiry: date
    time_to_expiry: float
    forward: float
    discount_factor: float
    total_mass: float
    implied_mean: float
    negative_mass: float
    mean_error: float
    is_admissible: bool
    flags: list[str] = []
    #: Absent unless the density is admissible: a quantile of a curve that does
    #: not integrate to one is a number with no meaning.
    percentiles: dict[str, float | None] = {}
    strikes: list[float] = []
    density: list[float] = []


class HestonCalibrationOut(APIModel):
    heston_calibration_row_id: uuid.UUID
    as_of_timestamp: datetime
    model_version: str
    status: str
    v0: float | None = None
    kappa: float | None = None
    theta: float | None = None
    xi: float | None = None
    rho: float | None = None
    n_observations: int
    n_maturities: int
    rmse_vol_points: float | None = None
    max_error_vol_points: float | None = None
    #: ``2 kappa theta - xi^2``, reported whether or not it was enforced.
    feller: float | None = None
    satisfies_feller: bool
    feller_enforced: bool
    warnings: list[str] = []
    error: str | None = None


class PriceConsensusRequest(APIModel):
    """One contract, and the models to compare on it."""

    instrument_id: uuid.UUID
    #: Empty means every model the platform has. A model whose inputs are
    #: missing reports itself unavailable rather than being dropped.
    models: list[str] = Field(default_factory=list, max_length=8)
    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)
    paths: int = Field(default=100_000, ge=1_000, le=2_000_000)
    seed: int = Field(default=20_260_924, ge=0)
    grid_nodes: int = Field(default=401, ge=51, le=4001)
    grid_steps: int = Field(default=200, ge=20, le=4000)
    global_surface_row_id: uuid.UUID | None = None


class ModelValueOut(APIModel):
    model: str
    model_version: str
    #: Exactly one of these two is set, enforced by a database CHECK.
    value: float | None = None
    unavailable_reason: str | None = None
    method: str
    inputs_used: dict = {}
    diagnostics: dict = {}
    warnings: list[str] = []


class ConfidenceContributionOut(APIModel):
    name: str
    score: float
    weight: float
    basis: str


class ModelDispersionOut(APIModel):
    absolute: float | None = None
    relative: float | None = None
    standard_deviation: float | None = None


class ModelConsensusOut(APIModel):
    consensus_row_id: uuid.UUID
    global_surface_row_id: uuid.UUID | None = None
    instrument_id: uuid.UUID
    expiry: date
    strike: DecimalStr
    option_type: str
    as_of_timestamp: datetime
    model_version: str

    spot: float
    time_to_expiry: float
    risk_free_rate: float
    dividend_yield: float
    reference_volatility: float | None = None

    models_requested: int
    models_available: int
    #: The median of the models that produced a value. Not a price the contract
    #: is worth, and deliberately less prominent than the range around it.
    reference_value: float | None = None
    reference_range: list[float] | None = None
    model_dispersion: ModelDispersionOut

    #: An observation, stored apart from every model output and never written
    #: from one. Absent when there is no two-sided market.
    market_price: float | None = None
    market_deviation: float | None = None
    market_deviation_relative: float | None = None

    confidence: float
    confidence_contributions: list[ConfidenceContributionOut] = []
    vanna: float | None = None
    volga: float | None = None
    charm_per_day: float | None = None
    values: list[ModelValueOut] = []
    created_at: datetime
