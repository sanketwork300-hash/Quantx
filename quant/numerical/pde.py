"""Crank-Nicolson on ``x = ln S``, with Rannacher start-up.

Crank-Nicolson is second-order accurate and unconditionally stable, and it
oscillates badly against a non-smooth payoff — which every vanilla option has, at
the strike. The oscillation shows up first in gamma, where it matters most.
Rannacher start-up fixes it by replacing the first two steps with four
fully-implicit half-steps: implicit Euler is only first order but strongly
damping, and two steps of it kill the high-frequency error before Crank-Nicolson
takes over, at a cost that vanishes as the grid refines.

The equation solved, backwards in ``tau = T - t``:

    dV/dtau = (1/2) sigma^2(S, tau) d2V/dx2
              + (r - q - (1/2) sigma^2(S, tau)) dV/dx
              - r V

Boundaries are Dirichlet from the payoff's asymptotics rather than from a
zero-gamma guess, because the asymptotic value is exact and the guess is not.

The convergence test is not decoration: with a constant local volatility the
answer is Black-Scholes, and the test measures the **empirical order** rather
than only checking that the final error is small. An implementation that is
accidentally first order passes a small-error check and fails this one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

#: Two Crank-Nicolson steps replaced by four implicit half-steps.
RANNACHER_STEPS = 2

#: How many standard deviations of log-price the grid spans by default. Wide
#: enough that the Dirichlet boundaries are effectively exact.
DEFAULT_WIDTH_IN_STDEV = 6.0


class BoundaryCondition(StrEnum):
    #: Value at the edges taken from the payoff's asymptotic behaviour.
    ASYMPTOTIC_DIRICHLET = "ASYMPTOTIC_DIRICHLET"


class PDEWarning(StrEnum):
    SPOT_OUTSIDE_GRID = "PDE_SPOT_OUTSIDE_GRID"
    COARSE_GRID = "PDE_COARSE_GRID"
    NON_POSITIVE_VOLATILITY = "PDE_NON_POSITIVE_LOCAL_VOLATILITY"


class PDEError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GridSpec:
    """Where the grid lives and how tightly it clusters around the strike.

    ``concentration`` of zero is a uniform grid. Above zero a sinh transform
    packs nodes around ``centre``, which is where the payoff kinks and where the
    accuracy is therefore worth paying for.
    """

    nodes: int = 401
    steps: int = 200
    width_in_stdev: float = DEFAULT_WIDTH_IN_STDEV
    concentration: float = 0.0

    def __post_init__(self) -> None:
        if self.nodes < 5:
            raise PDEError("a finite-difference grid needs at least five nodes")
        if self.steps < 4:
            raise PDEError("a Rannacher start-up needs at least four time steps")
        if self.width_in_stdev <= 0:
            raise PDEError("grid width must be positive")
        if self.concentration < 0:
            raise PDEError("concentration cannot be negative")

    def to_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "steps": self.steps,
            "width_in_stdev": self.width_in_stdev,
            "concentration": self.concentration,
            "uniform": self.concentration == 0.0,
        }


@dataclass(frozen=True, slots=True)
class PDEResult:
    price: float
    delta: float
    gamma: float
    grid: np.ndarray
    values: np.ndarray
    spec: GridSpec
    method: str
    rannacher_steps: int
    boundary: BoundaryCondition
    warnings: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "delta": self.delta,
            "gamma": self.gamma,
            "method": self.method,
            "rannacher_steps": self.rannacher_steps,
            "boundary": str(self.boundary),
            "grid": self.spec.to_dict(),
            "warnings": list(self.warnings),
        }


def build_log_grid(centre: float, half_width: float, spec: GridSpec) -> np.ndarray:
    """Nodes in ``x = ln S``, optionally clustered around ``centre``.

    The sinh transform is smooth, so the second-difference operator stays
    second-order accurate on it — a grid refined by an arbitrary rule would not
    have that property, and the convergence test would show it.
    """
    uniform = np.linspace(-1.0, 1.0, spec.nodes)
    if spec.concentration == 0.0:
        return centre + half_width * uniform
    scaled = np.sinh(spec.concentration * uniform) / np.sinh(spec.concentration)
    return centre + half_width * scaled


def _tridiagonal_operator(
    x: np.ndarray, diffusion: np.ndarray, drift: np.ndarray, rate: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Second-order differences on a possibly non-uniform grid.

    Returns the sub-, main- and super-diagonals of the spatial operator for the
    interior nodes only; the boundaries are Dirichlet and handled separately.
    """
    h_minus = x[1:-1] - x[:-2]
    h_plus = x[2:] - x[1:-1]
    total = h_minus + h_plus

    # First derivative, second-order accurate on a non-uniform grid.
    d1_lower = -h_plus / (h_minus * total)
    d1_diag = (h_plus - h_minus) / (h_minus * h_plus)
    d1_upper = h_minus / (h_plus * total)

    # Second derivative.
    d2_lower = 2.0 / (h_minus * total)
    d2_diag = -2.0 / (h_minus * h_plus)
    d2_upper = 2.0 / (h_plus * total)

    lower = diffusion * d2_lower + drift * d1_lower
    diag = diffusion * d2_diag + drift * d1_diag - rate
    upper = diffusion * d2_upper + drift * d1_upper
    return lower, diag, upper


def _solve_tridiagonal(
    lower: np.ndarray, diag: np.ndarray, upper: np.ndarray, rhs: np.ndarray
) -> np.ndarray:
    """Thomas algorithm. Stable here because the operator is diagonally dominant."""
    n = diag.size
    c = np.empty(n)
    d = np.empty(n)

    c[0] = upper[0] / diag[0]
    d[0] = rhs[0] / diag[0]
    for i in range(1, n):
        denominator = diag[i] - lower[i] * c[i - 1]
        c[i] = upper[i] / denominator if i < n - 1 else 0.0
        d[i] = (rhs[i] - lower[i] * d[i - 1]) / denominator

    solution = np.empty(n)
    solution[-1] = d[-1]
    for i in range(n - 2, -1, -1):
        solution[i] = d[i] - c[i] * solution[i + 1]
    return solution


def solve_local_vol_pde(
    spot: float,
    strike: float,
    tau: float,
    rate: float,
    dividend: float,
    local_volatility: Callable[[np.ndarray, float], np.ndarray],
    is_call: bool = True,
    spec: GridSpec | None = None,
    reference_volatility: float | None = None,
) -> PDEResult:
    """Price one European vanilla under a local volatility function."""
    if spot <= 0 or strike <= 0:
        raise PDEError("spot and strike must be positive")
    if tau <= 0:
        raise PDEError("the PDE needs a positive time to expiry")

    spec = spec or GridSpec()
    warnings: list[str] = []

    anchor = reference_volatility or float(
        np.mean(np.atleast_1d(local_volatility(np.array([spot]), tau)))
    )
    if not np.isfinite(anchor) or anchor <= 0:
        raise PDEError("the local volatility function returned a non-positive value at the spot")

    centre = np.log(strike)
    half_width = spec.width_in_stdev * anchor * np.sqrt(tau) + abs(np.log(spot / strike))
    x = build_log_grid(centre, half_width, spec)
    s = np.exp(x)

    if not (x[0] < np.log(spot) < x[-1]):
        warnings.append(PDEWarning.SPOT_OUTSIDE_GRID)
    if spec.nodes < 51 or spec.steps < 20:
        warnings.append(PDEWarning.COARSE_GRID)

    values = np.maximum(s - strike, 0.0) if is_call else np.maximum(strike - s, 0.0)

    dt = tau / spec.steps
    # Rannacher: the first two steps are each split into two implicit halves.
    schedule: list[tuple[float, float]] = []
    for index in range(spec.steps):
        if index < RANNACHER_STEPS:
            schedule.append((dt / 2.0, 1.0))
            schedule.append((dt / 2.0, 1.0))
        else:
            schedule.append((dt, 0.5))

    elapsed = 0.0
    for step, theta in schedule:
        elapsed += step
        sigma = np.asarray(local_volatility(s, tau - elapsed + step), dtype=float)
        if np.any(~np.isfinite(sigma)) or np.any(sigma <= 0):
            warnings.append(PDEWarning.NON_POSITIVE_VOLATILITY)
            sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, anchor)

        variance = sigma[1:-1] ** 2
        diffusion = 0.5 * variance
        drift = rate - dividend - 0.5 * variance
        lower, diag, upper = _tridiagonal_operator(x, diffusion, drift, rate)

        boundary_low, boundary_high = _boundaries(
            s[0], s[-1], strike, elapsed, rate, dividend, is_call
        )

        interior = values[1:-1]
        explicit = interior + step * (1.0 - theta) * (
            lower * values[:-2] + diag * interior + upper * values[2:]
        )
        explicit[0] += step * (1.0 - theta) * 0.0
        rhs = explicit.copy()
        rhs[0] += step * theta * lower[0] * boundary_low
        rhs[-1] += step * theta * upper[-1] * boundary_high

        solved = _solve_tridiagonal(
            -step * theta * lower,
            1.0 - step * theta * diag,
            -step * theta * upper,
            rhs,
        )
        values = np.concatenate(([boundary_low], solved, [boundary_high]))

    price, delta, gamma = _interpolate(x, values, float(np.log(spot)), spot)
    return PDEResult(
        price=price,
        delta=delta,
        gamma=gamma,
        grid=x,
        values=values,
        spec=spec,
        method="Crank-Nicolson with Rannacher start-up",
        rannacher_steps=RANNACHER_STEPS,
        boundary=BoundaryCondition.ASYMPTOTIC_DIRICHLET,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _boundaries(
    s_low: float,
    s_high: float,
    strike: float,
    elapsed: float,
    rate: float,
    dividend: float,
    is_call: bool,
) -> tuple[float, float]:
    """Asymptotic values, which are exact rather than approximate.

    Deep out of the money a vanilla is worth nothing; deep in the money it is
    worth the discounted forward intrinsic. Both are exact at the edges of a
    wide grid, which is why the grid is made wide rather than the boundary made
    clever.
    """
    discount = np.exp(-rate * elapsed)
    carry = np.exp(-dividend * elapsed)
    if is_call:
        return 0.0, float(s_high * carry - strike * discount)
    return float(strike * discount - s_low * carry), 0.0


#: Nodes used for the interpolant at the spot. Five, fitted by a quartic,
#: because gamma comes from its second derivative: a three-point quadratic gives
#: a second derivative accurate only to first order, which would cap gamma's
#: convergence at one however good the solver underneath is. Rannacher start-up
#: exists to protect gamma, and reading it off a stencil too narrow to see the
#: improvement would waste that.
INTERPOLATION_NODES = 5


def _interpolate(
    x: np.ndarray, values: np.ndarray, x_spot: float, spot: float
) -> tuple[float, float, float]:
    """Price and Greeks at the spot, from a local polynomial fit."""
    width = min(INTERPOLATION_NODES, x.size)
    half = width // 2
    index = int(np.clip(np.searchsorted(x, x_spot), half, x.size - half - 1))
    nodes = x[index - half : index + half + 1]
    local = values[index - half : index + half + 1]

    coefficients = np.polyfit(nodes - x_spot, local, width - 1)
    price = float(coefficients[-1])
    dv_dx = float(coefficients[-2])
    d2v_dx2 = float(2.0 * coefficients[-3])

    delta = dv_dx / spot
    gamma = (d2v_dx2 - dv_dx) / (spot * spot)
    return price, delta, gamma


def convergence_order(errors: list[float], refinements: list[int]) -> float:
    """Empirical order from a refinement sequence, by least squares in log-log.

    Reported rather than asserted point-to-point, because a single ratio of two
    errors is noisy and a fitted slope over several refinements is not.
    """
    if len(errors) != len(refinements) or len(errors) < 2:
        raise PDEError("an order estimate needs at least two refinements")
    if any(error <= 0 for error in errors):
        raise PDEError("cannot take the order of a zero error")

    log_h = np.log(1.0 / np.asarray(refinements, dtype=float))
    log_e = np.log(np.asarray(errors, dtype=float))
    slope, _intercept = np.polyfit(log_h, log_e, 1)
    return float(slope)
