"""Raw-SVI calibration.

Weighted least squares on **total variance**, subject to the constraints that
make a slice admissible. Two design decisions carry most of the weight:

1. **The no-arbitrage conditions are constraints, not a post-hoc check.** A fit
   that lands outside them is not a slightly worse fit, it is a surface with a
   negative implied density somewhere. SLSQP is given the minimum-variance
   condition, Lee's wing bound, *and* Durrleman's ``g(k) >= 0`` evaluated on a
   grid, so an admissible slice is what the optimizer is searching for rather
   than what it is checked against afterwards.

   This matters on real data: a single badly mispriced quote is enough to bend
   an unconstrained least-squares fit into a negative implied density. With the
   condition in the feasible set the fit absorbs the bad quote as error instead,
   which is the right trade — and the raw-market arbitrage report still names
   the quote.

2. **Multi-start is deterministic.** The objective is not convex and a single
   start lands in a local minimum on real smiles. The starts are a fixed list
   plus seeded perturbations, so the same quotes always produce the same
   parameters — a surface that re-fitted differently on each run could not be
   reproduced, and reproducibility is the whole point of storing it.

Reference: J. Gatheral, *The Volatility Surface* (2006); Gatheral & Jacquier,
*Arbitrage-free SVI volatility surfaces* (2014).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from scipy.optimize import minimize

from quant.volatility.svi import (
    LEE_WING_BOUND,
    SVIParameters,
    min_durrleman_g,
    raw_svi_total_variance,
    wing_slope,
)

#: Five free parameters need at least this many points before a fit means
#: anything. Below it the slice is reported as insufficient rather than fitted
#: with a degenerate parameter set that would interpolate noise.
MIN_OBSERVATIONS = 5

#: Grid over which the Durrleman condition is checked after fitting. Wider than
#: the observed strikes on purpose: a surface is queried in its wings, and a
#: butterfly violation just outside the data is still a violation.
DURRLEMAN_MARGIN = 1.0
DURRLEMAN_POINTS = 801

#: Grid used for the Durrleman *constraint* inside the optimizer. Coarser than
#: the check grid because every point costs an evaluation on every iteration;
#: the fine grid afterwards is what decides the reported status, so a violation
#: that slips between constraint points is still caught and reported.
DURRLEMAN_CONSTRAINT_POINTS = 61

#: Numerical tolerance on the constraint functions.
CONSTRAINT_TOL = 1e-9

VOL_POINT = 100.0


class CalibrationStatus(StrEnum):
    #: Optimizer converged, constraints hold, and the slice is butterfly-free.
    CONVERGED = "CONVERGED"
    #: A fit was found but something is wrong with it: a butterfly violation, or
    #: an optimizer that stopped without declaring success. Usable with care and
    #: always reported.
    DEGRADED = "DEGRADED"
    #: No feasible parameters were found.
    FAILED = "FAILED"
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"


@dataclass(frozen=True, slots=True)
class SVICalibrationResult:
    parameters: SVIParameters | None
    status: CalibrationStatus
    n_observations: int
    #: Root mean squared error in total variance (the fitted quantity).
    rmse_total_variance: float | None = None
    #: The same error weighted by the calibration weights.
    weighted_rmse: float | None = None
    #: RMS error in **volatility points**, which is the unit practitioners read.
    rmse_vol_points: float | None = None
    max_error_vol_points: float | None = None
    optimizer: str = "SLSQP"
    optimizer_message: str = ""
    iterations: int = 0
    starts_attempted: int = 0
    starts_feasible: int = 0
    #: Minimum of Durrleman's g over the check grid, and where it occurred.
    min_durrleman_g: float | None = None
    min_durrleman_k: float | None = None
    wing_slope: float | None = None
    constraints_satisfied: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.parameters is not None and self.status is CalibrationStatus.CONVERGED

    def to_dict(self) -> dict:
        return {
            "parameters": self.parameters.to_dict() if self.parameters else None,
            "status": str(self.status),
            "n_observations": self.n_observations,
            "rmse_total_variance": self.rmse_total_variance,
            "weighted_rmse": self.weighted_rmse,
            "rmse_vol_points": self.rmse_vol_points,
            "max_error_vol_points": self.max_error_vol_points,
            "optimizer": self.optimizer,
            "optimizer_message": self.optimizer_message,
            "iterations": self.iterations,
            "starts_attempted": self.starts_attempted,
            "starts_feasible": self.starts_feasible,
            "min_durrleman_g": self.min_durrleman_g,
            "min_durrleman_k": self.min_durrleman_k,
            "wing_slope": self.wing_slope,
            "constraints_satisfied": self.constraints_satisfied,
            "error": self.error,
        }


def _to_params(vector: np.ndarray) -> SVIParameters | None:
    try:
        return SVIParameters(
            a=float(vector[0]),
            b=float(vector[1]),
            rho=float(vector[2]),
            m=float(vector[3]),
            sigma=float(vector[4]),
        )
    except ValueError:
        return None


def _model(vector: np.ndarray, k: np.ndarray) -> np.ndarray:
    a, b, rho, m, sigma = vector
    shifted = k - m
    return a + b * (rho * shifted + np.sqrt(shifted * shifted + sigma * sigma))


def _durrleman_from_vector(vector: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Durrleman's ``g`` straight from a parameter vector.

    Written against the raw vector rather than :class:`SVIParameters` because
    the optimizer evaluates it at infeasible points where constructing a
    validated parameter object would raise.
    """
    a, b, rho, m, sigma = vector
    shifted = k - m
    root = np.sqrt(shifted * shifted + sigma * sigma)

    with np.errstate(divide="ignore", invalid="ignore"):
        w = a + b * (rho * shifted + root)
        dw = b * (rho + shifted / root)
        d2w = b * sigma * sigma / (root**3)

        term = 1.0 - k * dw / (2.0 * w)
        g = term * term - (dw * dw / 4.0) * (1.0 / w + 0.25) + d2w / 2.0

    # Where total variance is non-positive the slice is inadmissible outright;
    # a large negative pushes the optimizer away rather than producing a nan.
    return np.where((w > 0) & np.isfinite(g), g, -1e3)


def _seeds(k: np.ndarray, w: np.ndarray, rng: np.random.Generator) -> list[np.ndarray]:
    """Fixed starting points plus seeded perturbations.

    The first seed is a moment-based guess: the smile minimum locates ``m`` and
    ``a``, and the difference between the wing slopes gives the sign and rough
    size of ``rho``.
    """
    w_min = float(np.min(w))
    k_at_min = float(k[int(np.argmin(w))])
    k_span = max(float(np.max(k) - np.min(k)), 1e-3)

    left = k < k_at_min
    right = k > k_at_min
    left_slope = (
        float((w[left][0] - w_min) / (k_at_min - k[left][0]))
        if np.any(left) and k_at_min != k[left][0]
        else 0.1
    )
    right_slope = (
        float((w[right][-1] - w_min) / (k[right][-1] - k_at_min))
        if np.any(right) and k[right][-1] != k_at_min
        else 0.1
    )
    b_guess = float(np.clip((abs(left_slope) + abs(right_slope)) / 2.0, 1e-3, 1.0))
    rho_guess = float(
        np.clip((right_slope - left_slope) / max(right_slope + left_slope, 1e-9), -0.9, 0.9)
    )

    fixed = [
        np.array([w_min * 0.5, b_guess, rho_guess, k_at_min, k_span * 0.5]),
        np.array([w_min * 0.9, 0.1, -0.5, k_at_min, 0.10]),
        np.array([max(w_min * 0.1, 1e-6), 0.2, -0.7, 0.0, 0.20]),
        np.array([0.0, 0.05, 0.0, 0.0, 0.50]),
        np.array([w_min, 0.02, -0.3, k_at_min, 0.05]),
    ]
    perturbed = [
        np.array(
            [
                w_min * rng.uniform(0.0, 1.0),
                float(rng.uniform(0.01, 0.6)),
                float(rng.uniform(-0.9, 0.5)),
                float(rng.uniform(np.min(k), np.max(k))),
                float(rng.uniform(0.02, 0.6)),
            ]
        )
        for _ in range(5)
    ]
    return fixed + perturbed


def calibrate_svi(
    k: np.ndarray,
    total_variance: np.ndarray,
    tau: float,
    weights: np.ndarray | None = None,
    *,
    seed: int = 20_260_924,
    max_iterations: int = 400,
) -> SVICalibrationResult:
    """Fit one expiry slice.

    ``k`` is log-moneyness, ``total_variance`` is ``sigma^2 tau`` at those
    points, and ``weights`` are the spread/liquidity weights carried through
    from the quality engine.
    """
    k = np.asarray(k, dtype=float)
    w = np.asarray(total_variance, dtype=float)
    n = int(k.size)

    if n < MIN_OBSERVATIONS:
        return SVICalibrationResult(
            parameters=None,
            status=CalibrationStatus.INSUFFICIENT_OBSERVATIONS,
            n_observations=n,
            error=f"{n} observations; {MIN_OBSERVATIONS} are needed for five parameters",
        )
    if tau <= 0:
        return SVICalibrationResult(
            parameters=None,
            status=CalibrationStatus.FAILED,
            n_observations=n,
            error="non-positive time to expiry",
        )

    omega = np.ones_like(k) if weights is None else np.asarray(weights, dtype=float)
    omega = np.where(np.isfinite(omega) & (omega > 0), omega, 0.0)
    if not np.any(omega > 0):
        omega = np.ones_like(k)
    omega = omega / omega.sum()

    w_max = float(np.max(w))
    k_min, k_max = float(np.min(k)), float(np.max(k))
    bounds = [
        (-2.0 * w_max, 2.0 * w_max),  # a
        (0.0, 4.0),  # b
        (-0.999, 0.999),  # rho
        (k_min - 1.0, k_max + 1.0),  # m
        (1e-4, 2.0),  # sigma
    ]

    def objective(vector: np.ndarray) -> float:
        residual = _model(vector, k) - w
        return float(np.sum(omega * residual * residual))

    # The Durrleman constraint is imposed over the observed strikes plus a
    # margin, because a surface is queried in its wings too.
    constraint_grid = np.linspace(
        k_min - DURRLEMAN_MARGIN, k_max + DURRLEMAN_MARGIN, DURRLEMAN_CONSTRAINT_POINTS
    )

    constraints = [
        # Minimum total variance must be non-negative: a + b*sigma*sqrt(1-rho^2).
        {
            "type": "ineq",
            "fun": lambda v: v[0] + v[1] * v[4] * np.sqrt(max(1.0 - v[2] ** 2, 0.0)),
        },
        # Lee's moment formula: the steeper wing slope may not exceed 2.
        {"type": "ineq", "fun": lambda v: LEE_WING_BOUND - v[1] * (1.0 + abs(v[2]))},
        # No negative implied density anywhere on the grid.
        {"type": "ineq", "fun": lambda v: _durrleman_from_vector(v, constraint_grid)},
    ]

    def is_feasible(vector: np.ndarray) -> bool:
        return all(
            np.all(np.asarray(constraint["fun"](vector)) >= -CONSTRAINT_TOL)
            for constraint in constraints
        )

    rng = np.random.default_rng(seed)
    starts = _seeds(k, w, rng)
    best_vector: np.ndarray | None = None
    best_value = np.inf
    best_message = ""
    best_iterations = 0
    feasible_starts = 0

    for start in starts:
        clipped = np.array(
            [np.clip(value, low, high) for value, (low, high) in zip(start, bounds, strict=True)]
        )
        try:
            outcome = minimize(
                objective,
                clipped,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": max_iterations, "ftol": 1e-14},
            )
        except (ValueError, FloatingPointError):
            continue

        vector = np.asarray(outcome.x, dtype=float)
        if not np.all(np.isfinite(vector)):
            continue
        if _to_params(vector) is None:
            continue
        if not is_feasible(vector):
            continue

        feasible_starts += 1
        value = objective(vector)
        if value < best_value:
            best_value = value
            best_vector = vector
            best_message = str(outcome.message)
            best_iterations = int(getattr(outcome, "nit", 0))

    if best_vector is None:
        return SVICalibrationResult(
            parameters=None,
            status=CalibrationStatus.FAILED,
            n_observations=n,
            starts_attempted=len(starts),
            starts_feasible=0,
            error="no feasible parameters found from any starting point",
        )

    params = _to_params(best_vector)
    assert params is not None

    fitted = raw_svi_total_variance(k, params)
    residual = fitted - w
    weighted_rmse = float(np.sqrt(np.sum(omega * residual * residual)))
    rmse = float(np.sqrt(np.mean(residual * residual)))

    fitted_vol = np.sqrt(np.maximum(fitted, 0.0) / tau)
    observed_vol = np.sqrt(np.maximum(w, 0.0) / tau)
    vol_error = np.abs(fitted_vol - observed_vol) * VOL_POINT

    g_min, g_k = min_durrleman_g(
        params, k_min - DURRLEMAN_MARGIN, k_max + DURRLEMAN_MARGIN, DURRLEMAN_POINTS
    )
    slope = wing_slope(params)
    butterfly_free = g_min >= -CONSTRAINT_TOL
    lee_ok = slope <= LEE_WING_BOUND + CONSTRAINT_TOL

    status = (
        CalibrationStatus.CONVERGED if butterfly_free and lee_ok else CalibrationStatus.DEGRADED
    )
    error = None
    if not butterfly_free:
        error = f"butterfly violation: min g = {g_min:.3e} at k = {g_k:.3f}"
    elif not lee_ok:
        error = f"wing slope {slope:.3f} exceeds Lee's bound of {LEE_WING_BOUND}"

    return SVICalibrationResult(
        parameters=params,
        status=status,
        n_observations=n,
        rmse_total_variance=rmse,
        weighted_rmse=weighted_rmse,
        rmse_vol_points=float(np.sqrt(np.mean(vol_error**2))),
        max_error_vol_points=float(np.max(vol_error)),
        optimizer_message=best_message,
        iterations=best_iterations,
        starts_attempted=len(starts),
        starts_feasible=feasible_starts,
        min_durrleman_g=g_min,
        min_durrleman_k=g_k,
        wing_slope=slope,
        constraints_satisfied=butterfly_free and lee_ok,
        error=error,
    )
