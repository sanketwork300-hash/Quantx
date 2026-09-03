"""Constrained Heston calibration.

Five parameters fitted to a whole surface at once, with three decisions worth
stating:

1. **The objective is vega-weighted price error, which is volatility error to
   first order.** Fitting raw prices would let a single deep in-the-money quote,
   worth twenty times an out-of-the-money one, dominate the fit while carrying
   almost no volatility information. Inverting every model price to an implied
   volatility on every iteration is the honest alternative and costs a root
   solve per quote per evaluation; dividing the price residual by the market
   vega is the same quantity to first order at a fraction of the cost, and the
   final diagnostics report the *exact* volatility error so the approximation
   never reaches the reported number.

2. **Feller is reported, and only optionally enforced.** ``2 kappa theta > xi^2``
   keeps the variance process from reaching zero. Real index surfaces routinely
   calibrate to parameter sets that violate it, and refusing those fits would
   mean refusing to describe the market. The condition is therefore a diagnostic
   by default; ``require_feller`` makes it a constraint for callers who need the
   process rather than the surface.

3. **Multi-start is deterministic**, for the same reason as SVI: a calibration
   that landed somewhere different on each run could not be reproduced, and the
   parameters are stored precisely so that it can be.

Reference: S. Heston, *A Closed-Form Solution for Options with Stochastic
Volatility*, RFS 6(2), 1993; H. Albrecher et al., *The little Heston trap*,
Wilmott, 2007.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from scipy.optimize import minimize

from quant.pricing.heston import HestonError, HestonParameters, heston_price
from quant.volatility.svi_calibration import VOL_POINT, CalibrationStatus

#: Five parameters. Below this many quotes the fit interpolates noise, and the
#: result says so rather than returning a confident parameter set.
MIN_OBSERVATIONS = 5

#: Bounds. Wide enough for index and single-stock surfaces, narrow enough that
#: the optimizer cannot wander into regions where the characteristic function's
#: quadrature stops converging.
BOUNDS: tuple[tuple[float, float], ...] = (
    (1e-6, 2.0),  # v0
    (1e-3, 20.0),  # kappa
    (1e-6, 2.0),  # theta
    (1e-3, 5.0),  # xi
    (-0.999, 0.999),  # rho
)

#: Below this vega a quote carries essentially no volatility information and
#: dividing by it would amplify its price error without bound.
MIN_VEGA = 1e-6

#: The vega-weighted objective lands around 1e-8 on a well-fitting surface,
#: which is under SLSQP's convergence tolerance, so the optimizer would stop in
#: the middle of the valley and report a parameter set several percent from the
#: minimum. Scaling the objective moves it off the tolerance floor; it changes
#: nothing about where the minimum is. Measured on a surface generated from
#: known parameters: without the scaling kappa came back 3.01 against a true
#: 1.80, with it 1.80.
OBJECTIVE_SCALE = 1e6

#: Finite-difference step for SLSQP's gradients. The parameters span four orders
#: of magnitude (v0 near 0.04, kappa near 2), and the default step is too coarse
#: for the small ones.
GRADIENT_STEP = 1e-8


#: Below this many expiries the variance term structure is too short to separate
#: the mean-reversion speed from its level. The fit is still reported — the
#: *surface* is described well, and that is what the consensus prices from — but
#: kappa and theta individually are not identified and the result says so.
IDENTIFIABLE_MATURITIES = 3


class HestonCalibrationWarning(StrEnum):
    FELLER_VIOLATED = "HESTON_FELLER_VIOLATED"
    AT_PARAMETER_BOUND = "HESTON_AT_PARAMETER_BOUND"
    SINGLE_MATURITY = "HESTON_SINGLE_MATURITY"
    #: kappa and theta trade off against each other; only their product is
    #: pinned by a short term structure.
    MEAN_REVERSION_NOT_IDENTIFIED = "HESTON_MEAN_REVERSION_NOT_IDENTIFIED"


@dataclass(frozen=True, slots=True)
class HestonObservation:
    """One quote, in the coordinates the calibration reads."""

    strike: float
    maturity: float
    price: float
    is_call: bool
    #: Black-Scholes vega at the *observed* implied volatility. The weight, not
    #: an input to the model.
    vega: float
    rate: float = 0.0
    dividend: float = 0.0
    market_volatility: float | None = None
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class HestonCalibrationResult:
    parameters: HestonParameters | None
    status: CalibrationStatus
    n_observations: int
    n_maturities: int = 0
    rmse_price: float | None = None
    rmse_vol_points: float | None = None
    max_error_vol_points: float | None = None
    optimizer: str = "SLSQP"
    optimizer_message: str = ""
    iterations: int = 0
    starts_attempted: int = 0
    starts_feasible: int = 0
    feller: float | None = None
    satisfies_feller: bool = False
    feller_enforced: bool = False
    warnings: tuple[str, ...] = field(default=())
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.parameters is not None and self.status is CalibrationStatus.CONVERGED

    def to_dict(self) -> dict:
        return {
            "model": "HESTON",
            "parameters": self.parameters.to_dict() if self.parameters else None,
            "status": str(self.status),
            "n_observations": self.n_observations,
            "n_maturities": self.n_maturities,
            "rmse_price": self.rmse_price,
            "rmse_vol_points": self.rmse_vol_points,
            "max_error_vol_points": self.max_error_vol_points,
            "optimizer": self.optimizer,
            "optimizer_message": self.optimizer_message,
            "iterations": self.iterations,
            "starts_attempted": self.starts_attempted,
            "starts_feasible": self.starts_feasible,
            "feller": self.feller,
            "satisfies_feller": self.satisfies_feller,
            "feller_enforced": self.feller_enforced,
            "warnings": list(self.warnings),
            "error": self.error,
        }


def _to_parameters(vector: np.ndarray) -> HestonParameters | None:
    try:
        return HestonParameters(
            v0=float(vector[0]),
            kappa=float(vector[1]),
            theta=float(vector[2]),
            xi=float(vector[3]),
            rho=float(vector[4]),
        )
    except HestonError:
        return None


def _seeds(observations: list[HestonObservation], rng: np.random.Generator) -> list[np.ndarray]:
    """Fixed starts anchored on the observed variance, plus perturbations.

    The first start reads ``v0`` and ``theta`` off the shortest and longest
    observed at-the-money volatilities, which is what they mean: the variance
    now and the variance it reverts to.
    """
    with_vol = [o for o in observations if o.market_volatility]
    if with_vol:
        ordered = sorted(with_vol, key=lambda o: o.maturity)
        front = float(ordered[0].market_volatility or 0.2) ** 2
        back = float(ordered[-1].market_volatility or 0.2) ** 2
    else:
        front = back = 0.04

    fixed = [
        np.array([front, 2.0, back, 0.5, -0.6]),
        np.array([front, 1.0, back, 0.3, -0.7]),
        np.array([0.04, 3.0, 0.04, 0.8, -0.5]),
        np.array([front, 5.0, back, 1.0, -0.3]),
        np.array([back, 0.5, back, 0.4, -0.8]),
    ]
    perturbed = [
        np.array(
            [
                float(rng.uniform(0.5, 1.5)) * front,
                float(rng.uniform(0.2, 8.0)),
                float(rng.uniform(0.5, 1.5)) * back,
                float(rng.uniform(0.1, 1.5)),
                float(rng.uniform(-0.95, 0.2)),
            ]
        )
        for _ in range(5)
    ]
    return fixed + perturbed


def calibrate_heston(
    spot: float,
    observations: list[HestonObservation],
    *,
    seed: int = 20_260_924,
    max_iterations: int = 200,
    require_feller: bool = False,
    quadrature_nodes: int = 128,
) -> HestonCalibrationResult:
    """Fit one parameter set to every quote at once.

    ``quadrature_nodes`` is lower than the pricer's default during the fit and
    the reported diagnostics are recomputed at full accuracy afterwards: the
    optimizer needs the objective's *shape*, and paying full quadrature on every
    one of tens of thousands of evaluations buys precision the search cannot use.
    """
    n = len(observations)
    if spot <= 0:
        return HestonCalibrationResult(
            parameters=None,
            status=CalibrationStatus.FAILED,
            n_observations=n,
            error="the spot must be positive",
        )
    if n < MIN_OBSERVATIONS:
        return HestonCalibrationResult(
            parameters=None,
            status=CalibrationStatus.INSUFFICIENT_OBSERVATIONS,
            n_observations=n,
            error=f"{n} observations; {MIN_OBSERVATIONS} are needed for five parameters",
        )

    usable = [
        o
        for o in observations
        if o.maturity > 0 and o.strike > 0 and np.isfinite(o.price) and o.price >= 0
    ]
    if len(usable) < MIN_OBSERVATIONS:
        return HestonCalibrationResult(
            parameters=None,
            status=CalibrationStatus.INSUFFICIENT_OBSERVATIONS,
            n_observations=len(usable),
            error="too few quotes survived validation",
        )

    maturities = sorted({round(o.maturity, 10) for o in usable})
    weights = np.array(
        [o.weight / max(abs(o.vega), MIN_VEGA) for o in usable],
        dtype=float,
    )
    weights = np.where(np.isfinite(weights) & (weights > 0), weights, 0.0)
    if not np.any(weights > 0):
        weights = np.ones(len(usable))
    weights = weights / weights.sum()
    market = np.array([o.price for o in usable], dtype=float)
    vegas = np.maximum(np.array([abs(o.vega) for o in usable], dtype=float), MIN_VEGA)

    def model_prices(parameters: HestonParameters, nodes: int) -> np.ndarray:
        return np.array(
            [
                heston_price(
                    spot,
                    o.strike,
                    o.maturity,
                    o.rate,
                    o.dividend,
                    parameters,
                    o.is_call,
                    nodes=nodes,
                )
                for o in usable
            ],
            dtype=float,
        )

    def objective(vector: np.ndarray) -> float:
        parameters = _to_parameters(vector)
        if parameters is None:
            return 1e6
        try:
            modelled = model_prices(parameters, quadrature_nodes)
        except (HestonError, FloatingPointError, ValueError):
            return 1e6
        if not np.all(np.isfinite(modelled)):
            return 1e6
        residual = (modelled - market) / vegas
        return OBJECTIVE_SCALE * float(np.sum(weights * residual * residual))

    constraints: list[dict] = []
    if require_feller:
        constraints.append({"type": "ineq", "fun": lambda v: 2.0 * v[1] * v[2] - v[3] * v[3]})

    rng = np.random.default_rng(seed)
    starts = _seeds(usable, rng)
    best_vector: np.ndarray | None = None
    best_value = np.inf
    best_message = ""
    best_iterations = 0
    feasible = 0

    for start in starts:
        clipped = np.array(
            [np.clip(value, low, high) for value, (low, high) in zip(start, BOUNDS, strict=True)]
        )
        try:
            outcome = minimize(
                objective,
                clipped,
                method="SLSQP",
                bounds=list(BOUNDS),
                constraints=constraints,
                options={
                    "maxiter": max_iterations,
                    "ftol": 1e-14,
                    "eps": GRADIENT_STEP,
                },
            )
        except (ValueError, FloatingPointError):
            continue

        vector = np.asarray(outcome.x, dtype=float)
        if not np.all(np.isfinite(vector)) or _to_parameters(vector) is None:
            continue
        if require_feller and 2.0 * vector[1] * vector[2] - vector[3] ** 2 < -1e-12:
            continue

        feasible += 1
        value = objective(vector)
        if value < best_value:
            best_value = value
            best_vector = vector
            best_message = str(outcome.message)
            best_iterations = int(getattr(outcome, "nit", 0))

    if best_vector is None:
        return HestonCalibrationResult(
            parameters=None,
            status=CalibrationStatus.FAILED,
            n_observations=len(usable),
            n_maturities=len(maturities),
            starts_attempted=len(starts),
            starts_feasible=0,
            error="no feasible Heston parameters were found from any starting point",
        )

    parameters = _to_parameters(best_vector)
    assert parameters is not None

    modelled = model_prices(parameters, 256)
    residual = modelled - market
    rmse_price = float(np.sqrt(np.mean(residual * residual)))

    # The exact volatility error, not the vega linearisation the optimizer used.
    from quant.volatility.implied import implied_vol_bsm

    vol_errors: list[float] = []
    for observation, price in zip(usable, modelled, strict=True):
        if observation.market_volatility is None:
            continue
        solved = implied_vol_bsm(
            float(price),
            spot,
            observation.strike,
            observation.maturity,
            observation.rate,
            observation.dividend,
            observation.is_call,
        )
        if solved.implied_volatility is None:
            continue
        vol_errors.append(
            abs(solved.implied_volatility - observation.market_volatility) * VOL_POINT
        )

    warnings: list[str] = []
    if len(maturities) == 1:
        warnings.append(HestonCalibrationWarning.SINGLE_MATURITY)
    if len(maturities) < IDENTIFIABLE_MATURITIES:
        warnings.append(HestonCalibrationWarning.MEAN_REVERSION_NOT_IDENTIFIED)
    if not parameters.satisfies_feller:
        warnings.append(HestonCalibrationWarning.FELLER_VIOLATED)
    if any(
        abs(value - low) < 1e-8 or abs(value - high) < 1e-8
        for value, (low, high) in zip(best_vector, BOUNDS, strict=True)
    ):
        warnings.append(HestonCalibrationWarning.AT_PARAMETER_BOUND)

    return HestonCalibrationResult(
        parameters=parameters,
        status=CalibrationStatus.CONVERGED,
        n_observations=len(usable),
        n_maturities=len(maturities),
        rmse_price=rmse_price,
        rmse_vol_points=float(np.sqrt(np.mean(np.square(vol_errors)))) if vol_errors else None,
        max_error_vol_points=float(np.max(vol_errors)) if vol_errors else None,
        optimizer_message=best_message,
        iterations=best_iterations,
        starts_attempted=len(starts),
        starts_feasible=feasible,
        feller=parameters.feller,
        satisfies_feller=parameters.satisfies_feller,
        feller_enforced=require_feller,
        warnings=tuple(warnings),
    )
