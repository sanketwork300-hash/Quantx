"""SSVI calibration: one fit for the whole surface.

Raw SVI calibrates five parameters per expiry. SSVI calibrates *three* for the
whole surface plus one at-the-money total variance per expiry, and the
difference is not economy for its own sake:

* ``theta`` non-decreasing in maturity **is** the no-calendar-arbitrage
  condition for SSVI, so imposing it as a constraint here removes calendar
  arbitrage structurally. Phase 2's per-slice fit could only detect it.
* The butterfly conditions of Gatheral & Jacquier Theorem 4.2 are closed-form in
  ``(theta, phi, rho)``, so they enter the feasible set directly.

The optimizer is given the sufficient bounds *and* Durrleman's ``g >= 0``
evaluated on a grid, exactly as :mod:`quant.volatility.svi_calibration` does,
because the closed-form bounds are sufficient and not necessary: a surface can
fail them and still have a non-negative density everywhere. Both are reported.

Multi-start is deterministic — a fixed seed list plus seeded perturbations — so
the same quotes always produce the same surface. A surface that re-fitted
differently on each run could not be reproduced, and reproducibility is why the
parameters are stored at all.

Reference: J. Gatheral and A. Jacquier, *Arbitrage-free SVI volatility
surfaces*, Quantitative Finance 14(1), 2014, §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

from quant.volatility.ssvi import (
    BUTTERFLY_BOUND,
    MIN_TOTAL_VARIANCE,
    SSVIError,
    SSVIParameters,
    ThetaTermStructure,
    durrleman_g,
    ssvi_total_variance,
)
from quant.volatility.svi_calibration import CONSTRAINT_TOL, VOL_POINT, CalibrationStatus

#: Three global parameters plus one theta per expiry. A single expiry cannot
#: identify ``gamma`` — the maturity decay of the curvature — but it can still
#: identify a level, a tilt and a curvature, so one slice is permitted and the
#: unidentifiability is reported rather than the fit refused.
MIN_SLICES = 1

#: Points per slice, over the observed strikes plus a margin, at which each
#: admissibility condition is imposed inside the optimizer.
DURRLEMAN_MARGIN = 1.0
DURRLEMAN_CONSTRAINT_POINTS = 41

#: Finer grid used after the fit, so a violation that slipped between the
#: constraint points is still caught and reported.
DURRLEMAN_CHECK_POINTS = 401
DURRLEMAN_CHECK_RANGE = 2.0

#: A gap left under the strict bound ``theta phi (1 + |rho|) < 4``, which an
#: inequality constraint cannot express strictly.
STRICT_BOUND_MARGIN = 1e-6


@dataclass(frozen=True, slots=True)
class SSVISliceObservations:
    """One expiry's quotes, in the coordinates SSVI is fitted in."""

    maturity: float
    log_moneyness: np.ndarray
    total_variance: np.ndarray
    weights: np.ndarray | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if self.maturity <= 0:
            raise SSVIError("an SSVI slice needs a positive maturity")
        if self.log_moneyness.shape != self.total_variance.shape:
            raise SSVIError("log-moneyness and total variance must have the same shape")
        if self.log_moneyness.size == 0:
            raise SSVIError("an SSVI slice needs at least one observation")


@dataclass(frozen=True, slots=True)
class SSVISliceDiagnostics:
    """What the global fit did to one expiry.

    Reported per slice because a global surface can fit the term structure well
    and one expiry badly, and averaging that away is how a bad slice becomes
    invisible.
    """

    maturity: float
    label: str | None
    n_observations: int
    theta: float
    atm_volatility: float
    rmse_vol_points: float
    max_error_vol_points: float
    k_min: float
    k_max: float
    butterfly_first: float
    butterfly_second: float
    butterfly_bounds_satisfied: bool
    min_durrleman_g: float

    def to_dict(self) -> dict:
        return {
            "maturity": self.maturity,
            "label": self.label,
            "n_observations": self.n_observations,
            "theta": self.theta,
            "atm_volatility": self.atm_volatility,
            "rmse_vol_points": self.rmse_vol_points,
            "max_error_vol_points": self.max_error_vol_points,
            "k_min": self.k_min,
            "k_max": self.k_max,
            "butterfly_first": self.butterfly_first,
            "butterfly_second": self.butterfly_second,
            "butterfly_bounds_satisfied": self.butterfly_bounds_satisfied,
            "min_durrleman_g": self.min_durrleman_g,
        }


@dataclass(frozen=True, slots=True)
class SSVICalibrationResult:
    parameters: SSVIParameters | None
    term_structure: ThetaTermStructure | None
    status: CalibrationStatus
    n_observations: int
    n_slices: int
    rmse_total_variance: float | None = None
    weighted_rmse: float | None = None
    rmse_vol_points: float | None = None
    max_error_vol_points: float | None = None
    optimizer: str = "SLSQP"
    optimizer_message: str = ""
    iterations: int = 0
    starts_attempted: int = 0
    starts_feasible: int = 0
    #: Minimum of Durrleman's g over every slice's check grid.
    min_durrleman_g: float | None = None
    #: The largest of the two Theorem 4.2 quantities across the slices.
    max_butterfly_quantity: float | None = None
    butterfly_bounds_satisfied: bool = False
    #: True when theta is non-decreasing, which for SSVI *is* calendar freedom.
    calendar_arbitrage_free: bool = False
    slices: tuple[SSVISliceDiagnostics, ...] = field(default=())
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.parameters is not None and self.status is CalibrationStatus.CONVERGED

    def surface(self):
        from quant.volatility.ssvi import SSVISurface

        if self.parameters is None or self.term_structure is None:
            return None
        return SSVISurface(parameters=self.parameters, term_structure=self.term_structure)

    def to_dict(self) -> dict:
        return {
            "model": "SSVI",
            "parameters": self.parameters.to_dict() if self.parameters else None,
            "term_structure": (
                self.term_structure.to_dict() if self.term_structure is not None else None
            ),
            "status": str(self.status),
            "n_observations": self.n_observations,
            "n_slices": self.n_slices,
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
            "max_butterfly_quantity": self.max_butterfly_quantity,
            "butterfly_bounds_satisfied": self.butterfly_bounds_satisfied,
            "calendar_arbitrage_free": self.calendar_arbitrage_free,
            "slices": [slice_.to_dict() for slice_ in self.slices],
            "error": self.error,
        }


def _phi_from_vector(vector: np.ndarray, theta: np.ndarray) -> np.ndarray:
    """``phi`` straight from the raw vector.

    Written against the vector rather than :class:`SSVIParameters` because the
    optimizer evaluates constraints at infeasible points where constructing a
    validated parameter object would raise.
    """
    _, eta, gamma = vector[0], vector[1], vector[2]
    theta = np.maximum(theta, MIN_TOTAL_VARIANCE)
    return eta / (theta**gamma * (1.0 + theta) ** (1.0 - gamma))


def _w_from_vector(vector: np.ndarray, k: np.ndarray, theta: float) -> np.ndarray:
    rho = vector[0]
    phi = float(_phi_from_vector(vector, np.array([theta]))[0])
    shifted = phi * k + rho
    root = np.sqrt(shifted * shifted + (1.0 - rho * rho))
    return 0.5 * theta * (1.0 + rho * phi * k + root)


def _durrleman_from_vector(vector: np.ndarray, k: np.ndarray, theta: float) -> np.ndarray:
    rho = vector[0]
    phi = float(_phi_from_vector(vector, np.array([theta]))[0])
    shifted = phi * k + rho
    root = np.sqrt(shifted * shifted + (1.0 - rho * rho))

    with np.errstate(divide="ignore", invalid="ignore"):
        w = 0.5 * theta * (1.0 + rho * phi * k + root)
        dw = 0.5 * theta * phi * (rho + shifted / root)
        d2w = 0.5 * theta * phi * phi * (1.0 - rho * rho) / root**3

        first = (1.0 - k * dw / (2.0 * w)) ** 2
        g = first - (dw * dw / 4.0) * (1.0 / w + 0.25) + d2w / 2.0

    return np.where((w > 0) & np.isfinite(g), g, -1e3)


def _atm_total_variance(k: np.ndarray, w: np.ndarray) -> float:
    """The observed total variance at ``k = 0``.

    Linear between the two quotes bracketing the money, and the nearest quote
    when the money is outside the observed strikes. Not a fitted value: this is
    only the starting point for ``theta``, which the optimizer then moves.
    """
    order = np.argsort(k)
    ks, ws = k[order], w[order]
    if ks[0] > 0.0:
        return float(ws[0])
    if ks[-1] < 0.0:
        return float(ws[-1])
    return float(np.interp(0.0, ks, ws))


def _seeds(
    theta_guess: np.ndarray, slices: list[SSVISliceObservations], rng: np.random.Generator
) -> list[np.ndarray]:
    """Fixed starts plus seeded perturbations, in ``[rho, eta, gamma, theta...]``.

    The first start reads ``rho`` off the observed skew: SSVI's ``dw/dk`` at the
    money has the sign of ``rho``, so a downward-sloping smile — the usual index
    shape — starts the optimizer with a negative correlation rather than at
    zero, where the objective is flat in the tilt direction.
    """
    slopes = []
    for slice_ in slices:
        k, w = slice_.log_moneyness, slice_.total_variance
        if k.size >= 2 and float(np.max(k) - np.min(k)) > 1e-6:
            slopes.append(float(np.polyfit(k, w, 1)[0]) / max(float(np.mean(w)), 1e-8))
    skew = float(np.mean(slopes)) if slopes else 0.0
    rho_guess = float(np.clip(skew, -0.85, 0.85))

    fixed = [
        np.concatenate([[rho_guess, 1.0, 0.4], theta_guess]),
        np.concatenate([[-0.7, 1.0, 0.5], theta_guess]),
        np.concatenate([[-0.3, 0.5, 0.3], theta_guess]),
        np.concatenate([[0.0, 2.0, 0.5], theta_guess]),
        np.concatenate([[rho_guess, 0.2, 0.7], theta_guess]),
    ]
    perturbed = [
        np.concatenate(
            [
                [
                    float(rng.uniform(-0.9, 0.5)),
                    float(rng.uniform(0.1, 3.0)),
                    float(rng.uniform(0.05, 0.95)),
                ],
                theta_guess * rng.uniform(0.7, 1.3, size=theta_guess.size),
            ]
        )
        for _ in range(5)
    ]
    return fixed + perturbed


def calibrate_ssvi(
    slices: list[SSVISliceObservations],
    *,
    seed: int = 20_260_924,
    max_iterations: int = 400,
    enforce_butterfly_bounds: bool = True,
) -> SSVICalibrationResult:
    """Fit one SSVI surface to every expiry at once.

    ``enforce_butterfly_bounds`` puts Theorem 4.2's *sufficient* conditions in
    the feasible set. They are strong: a market with a very steep short-dated
    smile can be admissible — Durrleman non-negative everywhere — and still fail
    them. Turning them off keeps Durrleman's condition, which is the real one,
    and the resulting surface is reported as satisfying one and not the other.
    """
    ordered = sorted(slices, key=lambda item: item.maturity)
    n_slices = len(ordered)
    n_observations = sum(int(item.log_moneyness.size) for item in ordered)

    if n_slices < MIN_SLICES:
        return SSVICalibrationResult(
            parameters=None,
            term_structure=None,
            status=CalibrationStatus.INSUFFICIENT_OBSERVATIONS,
            n_observations=n_observations,
            n_slices=n_slices,
            error="an SSVI surface needs at least one expiry with usable quotes",
        )
    if any(item.maturity <= 0 for item in ordered):
        return SSVICalibrationResult(
            parameters=None,
            term_structure=None,
            status=CalibrationStatus.FAILED,
            n_observations=n_observations,
            n_slices=n_slices,
            error="every slice needs a positive maturity",
        )
    if n_observations < 3 * n_slices and n_observations < 5:
        return SSVICalibrationResult(
            parameters=None,
            term_structure=None,
            status=CalibrationStatus.INSUFFICIENT_OBSERVATIONS,
            n_observations=n_observations,
            n_slices=n_slices,
            error=f"{n_observations} observations across {n_slices} expiries is too few",
        )

    maturities = np.array([item.maturity for item in ordered])
    if np.any(np.diff(maturities) <= 0):
        return SSVICalibrationResult(
            parameters=None,
            term_structure=None,
            status=CalibrationStatus.FAILED,
            n_observations=n_observations,
            n_slices=n_slices,
            error="two slices share a maturity; SSVI needs one theta per expiry",
        )

    weights: list[np.ndarray] = []
    for item in ordered:
        omega = (
            np.ones_like(item.log_moneyness)
            if item.weights is None
            else np.asarray(item.weights, dtype=float)
        )
        omega = np.where(np.isfinite(omega) & (omega > 0), omega, 0.0)
        if not np.any(omega > 0):
            omega = np.ones_like(item.log_moneyness)
        weights.append(omega / omega.sum() / n_slices)

    # An isotonic start: theta must be non-decreasing, so the observed ATM
    # variances are made non-decreasing before the optimizer sees them rather
    # than handing it an infeasible point.
    raw_theta = np.array(
        [_atm_total_variance(item.log_moneyness, item.total_variance) for item in ordered]
    )
    theta_guess = np.maximum.accumulate(np.maximum(raw_theta, 1e-6))

    theta_ceiling = float(max(4.0, 4.0 * np.max(theta_guess)))
    bounds = [(-0.999, 0.999), (1e-4, 10.0), (1e-3, 0.999)] + [
        (1e-8, theta_ceiling) for _ in range(n_slices)
    ]

    constraint_grids = [
        np.linspace(
            float(np.min(item.log_moneyness)) - DURRLEMAN_MARGIN,
            float(np.max(item.log_moneyness)) + DURRLEMAN_MARGIN,
            DURRLEMAN_CONSTRAINT_POINTS,
        )
        for item in ordered
    ]
    check_grid = np.linspace(-DURRLEMAN_CHECK_RANGE, DURRLEMAN_CHECK_RANGE, DURRLEMAN_CHECK_POINTS)

    def objective(vector: np.ndarray) -> float:
        total = 0.0
        for index, item in enumerate(ordered):
            theta = float(vector[3 + index])
            residual = _w_from_vector(vector, item.log_moneyness, theta) - item.total_variance
            total += float(np.sum(weights[index] * residual * residual))
        return total

    def monotone(vector: np.ndarray) -> np.ndarray:
        thetas = vector[3:]
        if thetas.size < 2:
            return np.array([1.0])
        return np.diff(thetas)

    def durrleman(vector: np.ndarray) -> np.ndarray:
        return np.concatenate(
            [
                _durrleman_from_vector(vector, constraint_grids[index], float(vector[3 + index]))
                for index in range(n_slices)
            ]
        )

    def butterfly(vector: np.ndarray) -> np.ndarray:
        thetas = np.asarray(vector[3:], dtype=float)
        phi = _phi_from_vector(vector, thetas)
        factor = 1.0 + abs(vector[0])
        first = BUTTERFLY_BOUND - STRICT_BOUND_MARGIN - thetas * phi * factor
        second = BUTTERFLY_BOUND - thetas * phi * phi * factor
        return np.concatenate([first, second])

    constraints: list[dict] = [
        {"type": "ineq", "fun": monotone},
        {"type": "ineq", "fun": durrleman},
    ]
    if enforce_butterfly_bounds:
        constraints.append({"type": "ineq", "fun": butterfly})

    def is_feasible(vector: np.ndarray) -> bool:
        return all(
            np.all(np.asarray(constraint["fun"](vector)) >= -CONSTRAINT_TOL)
            for constraint in constraints
        )

    rng = np.random.default_rng(seed)
    starts = _seeds(theta_guess, ordered, rng)

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
        if not np.all(np.isfinite(vector)) or np.any(vector[3:] <= 0.0):
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
        return SSVICalibrationResult(
            parameters=None,
            term_structure=None,
            status=CalibrationStatus.FAILED,
            n_observations=n_observations,
            n_slices=n_slices,
            starts_attempted=len(starts),
            starts_feasible=0,
            error="no admissible SSVI parameters were found from any starting point",
        )

    try:
        parameters = SSVIParameters(
            rho=float(best_vector[0]), eta=float(best_vector[1]), gamma=float(best_vector[2])
        )
    except SSVIError as exc:  # a bound landed exactly on the boundary
        return SSVICalibrationResult(
            parameters=None,
            term_structure=None,
            status=CalibrationStatus.FAILED,
            n_observations=n_observations,
            n_slices=n_slices,
            starts_attempted=len(starts),
            starts_feasible=feasible_starts,
            error=str(exc),
        )

    thetas = np.asarray(best_vector[3:], dtype=float)
    # The optimizer's monotone constraint holds to its own tolerance; the term
    # structure's contract is exact, so the accumulation makes it exact.
    thetas = np.maximum.accumulate(thetas)
    term_structure = ThetaTermStructure(
        maturities=tuple(float(value) for value in maturities),
        thetas=tuple(float(value) for value in thetas),
    )

    diagnostics: list[SSVISliceDiagnostics] = []
    squared = []
    weighted_squared = 0.0
    vol_errors: list[np.ndarray] = []
    worst_g = np.inf
    worst_butterfly = 0.0
    bounds_ok = True

    for index, item in enumerate(ordered):
        theta = float(thetas[index])
        fitted = ssvi_total_variance(item.log_moneyness, theta, parameters)
        residual = np.asarray(fitted) - item.total_variance
        squared.append(residual * residual)
        weighted_squared += float(np.sum(weights[index] * residual * residual))

        tau = item.maturity
        fitted_vol = np.sqrt(np.maximum(np.asarray(fitted), 0.0) / tau)
        observed_vol = np.sqrt(np.maximum(item.total_variance, 0.0) / tau)
        vol_error = np.abs(fitted_vol - observed_vol) * VOL_POINT
        vol_errors.append(vol_error)

        phi = float(parameters.phi(theta))
        factor = 1.0 + abs(parameters.rho)
        first = theta * phi * factor
        second = theta * phi * phi * factor
        slice_bounds_ok = first < BUTTERFLY_BOUND and second <= BUTTERFLY_BOUND
        bounds_ok = bounds_ok and slice_bounds_ok
        worst_butterfly = max(worst_butterfly, first, second)

        g_min = float(np.min(durrleman_g(check_grid, theta, parameters)))
        worst_g = min(worst_g, g_min)

        diagnostics.append(
            SSVISliceDiagnostics(
                maturity=tau,
                label=item.label,
                n_observations=int(item.log_moneyness.size),
                theta=theta,
                atm_volatility=float(np.sqrt(theta / tau)),
                rmse_vol_points=float(np.sqrt(np.mean(vol_error**2))),
                max_error_vol_points=float(np.max(vol_error)),
                k_min=float(np.min(item.log_moneyness)),
                k_max=float(np.max(item.log_moneyness)),
                butterfly_first=first,
                butterfly_second=second,
                butterfly_bounds_satisfied=slice_bounds_ok,
                min_durrleman_g=g_min,
            )
        )

    all_squared = np.concatenate(squared)
    all_vol_errors = np.concatenate(vol_errors)
    butterfly_free = worst_g >= -CONSTRAINT_TOL
    calendar_free = term_structure.is_monotone

    status = (
        CalibrationStatus.CONVERGED
        if butterfly_free and calendar_free
        else CalibrationStatus.DEGRADED
    )
    error = None
    if not butterfly_free:
        error = f"butterfly violation: min Durrleman g = {worst_g:.3e} across the surface"
    elif not calendar_free:
        error = "the fitted at-the-money variance term structure is not non-decreasing"

    return SSVICalibrationResult(
        parameters=parameters,
        term_structure=term_structure,
        status=status,
        n_observations=n_observations,
        n_slices=n_slices,
        rmse_total_variance=float(np.sqrt(np.mean(all_squared))),
        weighted_rmse=float(np.sqrt(weighted_squared)),
        rmse_vol_points=float(np.sqrt(np.mean(all_vol_errors**2))),
        max_error_vol_points=float(np.max(all_vol_errors)),
        optimizer_message=best_message,
        iterations=best_iterations,
        starts_attempted=len(starts),
        starts_feasible=feasible_starts,
        min_durrleman_g=worst_g,
        max_butterfly_quantity=worst_butterfly,
        butterfly_bounds_satisfied=bounds_ok,
        calendar_arbitrage_free=calendar_free,
        slices=tuple(diagnostics),
        error=error,
    )
