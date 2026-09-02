"""Root finding with convergence reporting.

Two solvers, and both tell you how they did:

* :func:`safeguarded_newton` — vectorized Newton with a bisection fallback,
  used for whole option chains at once.
* :func:`brent` — a thin wrapper over ``scipy.optimize.brentq`` returning the
  same report shape, used as the per-element fallback.

Every solver returns iteration counts, the bracket it worked in and whether it
converged, because downstream confidence scoring needs to know *how* a number
was obtained, not just its value. A solver that silently returns its last
iterate is how a non-converged implied volatility becomes a data point on a
volatility surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq

DEFAULT_MAX_ITERATIONS = 100
DEFAULT_ABS_TOL = 1e-10
DEFAULT_X_TOL = 1e-12
#: Below this a derivative is treated as unusable and the step is bisected;
#: dividing by it would overflow rather than produce a Newton step.
_MIN_DERIVATIVE = 1e-250


@dataclass(frozen=True, slots=True)
class RootResult:
    root: float
    converged: bool
    iterations: int
    lower_bound: float
    upper_bound: float
    solver: str
    residual: float


@dataclass(frozen=True, slots=True)
class BatchRootResult:
    roots: np.ndarray
    converged: np.ndarray
    iterations: np.ndarray
    residuals: np.ndarray
    solver: str


def brent(
    f: Callable[[float], float],
    lower: float,
    upper: float,
    *,
    abs_tol: float = DEFAULT_ABS_TOL,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> RootResult:
    """Bracketed Brent. Reports failure rather than raising into a caller."""
    try:
        root, report = brentq(
            f,
            lower,
            upper,
            xtol=DEFAULT_X_TOL,
            rtol=8.9e-16,
            maxiter=max_iterations,
            full_output=True,
        )
    except (ValueError, RuntimeError):
        return RootResult(
            root=float("nan"),
            converged=False,
            iterations=0,
            lower_bound=lower,
            upper_bound=upper,
            solver="brent",
            residual=float("nan"),
        )
    residual = float(f(root))
    return RootResult(
        root=float(root),
        converged=bool(report.converged) and abs(residual) <= max(abs_tol, 1e-8),
        iterations=int(report.iterations),
        lower_bound=lower,
        upper_bound=upper,
        solver="brent",
        residual=residual,
    )


def safeguarded_newton(
    f: Callable[[np.ndarray], np.ndarray],
    fprime: Callable[[np.ndarray], np.ndarray],
    guess: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    abs_tol: float = DEFAULT_ABS_TOL,
    x_tol: float = DEFAULT_X_TOL,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    active: np.ndarray | None = None,
) -> BatchRootResult:
    """Vectorized Newton with a maintained bracket, for a monotone increasing ``f``.

    Newton alone is fast but steps off the flat wings of an option price curve,
    where the derivative underflows. Keeping a bracket and bisecting whenever a
    Newton step escapes it gives Newton's speed with bisection's guarantee, over
    a whole chain at once.

    **Termination is on the bracket width, not on the residual.** That
    distinction matters: for a deep out-of-the-money option the price is almost
    flat in volatility, so a residual of 1e-10 is reached while the volatility
    is still wrong in the sixth decimal. Converging the *argument* is what makes
    the recovered volatility accurate where it is least well determined.

    ``f`` must be increasing in ``x`` and bracketed by ``[lower, upper]``.
    """
    x = np.array(np.broadcast_to(guess, np.shape(guess)), dtype=float, copy=True)
    a = np.array(np.broadcast_to(lower, x.shape), dtype=float, copy=True)
    b = np.array(np.broadcast_to(upper, x.shape), dtype=float, copy=True)

    running = (
        np.ones(x.shape, dtype=bool)
        if active is None
        else np.array(np.broadcast_to(active, x.shape), dtype=bool, copy=True)
    )
    iterations = np.zeros(x.shape, dtype=int)
    converged = np.zeros(x.shape, dtype=bool)
    np.clip(x, a, b, out=x)

    for _ in range(max_iterations):
        if not running.any():
            break

        value = np.where(running, f(x), 0.0)

        exact = running & (value == 0.0)
        converged |= exact
        running &= ~exact
        if not running.any():
            break

        # Tighten the bracket using the sign of the residual (f is increasing).
        a = np.where(running & (value < 0), np.maximum(a, x), a)
        b = np.where(running & (value > 0), np.minimum(b, x), b)

        tolerance = x_tol * (1.0 + np.abs(x))
        tight = running & ((b - a) <= tolerance)
        converged |= tight
        running &= ~tight
        if not running.any():
            break

        derivative = np.where(running, fprime(x), 1.0)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            usable = np.abs(derivative) > _MIN_DERIVATIVE
            step = np.where(usable, value / np.where(usable, derivative, 1.0), np.inf)
        candidate = x - step

        # Bisect wherever Newton left the bracket or the derivative underflowed.
        outside = ~np.isfinite(candidate) | (candidate <= a) | (candidate >= b)
        candidate = np.where(outside, 0.5 * (a + b), candidate)

        x = np.where(running, candidate, x)
        iterations += running.astype(int)

    residuals = f(x)
    # A tight bracket is the convergence criterion; the residual is reported so
    # a caller can see how well determined the answer actually was.
    return BatchRootResult(
        roots=x,
        converged=converged & (np.abs(residuals) <= max(abs_tol, 1e-8)),
        iterations=iterations,
        residuals=residuals,
        solver="safeguarded-newton",
    )
