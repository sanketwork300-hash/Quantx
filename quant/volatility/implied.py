"""Implied-volatility inversion.

The arithmetic here is the easy part. What the platform actually needs is a
solver that reports *how* every number was obtained — bracket, iterations,
convergence, and a named reason when there is no answer — because the
confidence and quality machinery downstream has to distinguish "0.184, solved
cleanly on a liquid quote" from "0.184, last iterate of a solver that never
converged on a stale one-sided market".

That reporting requirement, not the inversion, is why this exists rather than a
call into ``vollib``. ``vollib``'s Let's-Be-Rational implementation is the
oracle these results are checked against in ``tests/quant_validation/``.

Work is done in **undiscounted forward space**: the target is ``price / DF``,
priced by Black-76 on the forward. Black-76 and Black-Scholes-Merton implied
volatilities are the same number, so :func:`implied_vol_bsm` simply converts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from quant.numerical.roots import DEFAULT_MAX_ITERATIONS, brent, safeguarded_newton
from quant.pricing.black76 import black76_bounds, black76_price, black76_vega

#: Solver bracket. Volatilities outside this are not a market observation, and a
#: solver that "finds" one has been handed a bad price.
MIN_VOL = 1e-8
MAX_VOL = 5.0
#: One expansion attempt for genuinely extreme quotes before giving up.
EXPANDED_MAX_VOL = 10.0
#: Absolute tolerance on the *price* residual, in undiscounted price units.
PRICE_ABS_TOL = 1e-10
#: A quote within this of its intrinsic bound has no time value to invert.
TIME_VALUE_EPS = 1e-12


class IVFailure(StrEnum):
    """Why an implied volatility does not exist for a quote.

    A structured non-result, never ``nan`` and never a clipped value: a caller
    can render the reason to a user, and the quality engine can count them.
    """

    OPTION_EXPIRED = "OPTION_EXPIRED"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    PRICE_BELOW_INTRINSIC = "PRICE_BELOW_INTRINSIC"
    PRICE_ABOVE_BOUND = "PRICE_ABOVE_BOUND"
    NO_TIME_VALUE = "NO_TIME_VALUE"
    NO_CONVERGENCE = "NO_CONVERGENCE"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True)
class ImpliedVolResult:
    implied_volatility: float | None
    converged: bool
    iterations: int
    lower_bound: float
    upper_bound: float
    solver: str
    error: IVFailure | None = None
    price_used: float | None = None
    residual: float | None = None
    #: dPrice/dsigma at the solution. Near zero means the price carries almost
    #: no information about volatility at this strike.
    vega: float | None = None
    #: How well determined the answer is: one unit of **price resolution**
    #: divided by vega. Resolution defaults to a float64 ulp, so with no further
    #: information this is pure numerical conditioning — a deep in-the-money
    #: option can invert its price exactly and still be uncertain in the fifth
    #: decimal, because many volatilities reproduce that price bit-for-bit.
    #:
    #: A caller that knows the *economic* resolution should supply it. A quote
    #: resting on the tick floor is known only to plus or minus half a tick, and
    #: a wide market only to plus or minus half its spread; in both cases the
    #: implied volatility is far less determined than float precision suggests,
    #: and the difference decides whether the quote should inform a surface.
    uncertainty: float | None = None

    @property
    def ok(self) -> bool:
        return self.implied_volatility is not None and self.converged

    @property
    def is_well_conditioned(self) -> bool:
        """Whether the price determines the volatility to better than 1e-6."""
        return self.uncertainty is not None and self.uncertainty <= 1e-6

    def to_dict(self) -> dict:
        return {
            "implied_volatility": self.implied_volatility,
            "converged": self.converged,
            "iterations": self.iterations,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "solver": self.solver,
            "error": str(self.error) if self.error else None,
            "price_used": self.price_used,
            "residual": self.residual,
            "vega": self.vega,
            "uncertainty": self.uncertainty,
            "well_conditioned": self.is_well_conditioned,
        }


@dataclass(frozen=True, slots=True)
class BatchImpliedVolResult:
    """Per-element results for a whole chain."""

    implied_volatility: np.ndarray  # nan where there is no answer
    converged: np.ndarray
    iterations: np.ndarray
    errors: list[IVFailure | None]
    solver: np.ndarray  # per element, since some fall back to Brent
    residual: np.ndarray
    vega: np.ndarray
    uncertainty: np.ndarray

    def __len__(self) -> int:
        return int(self.implied_volatility.size)

    @property
    def success_rate(self) -> float:
        if len(self) == 0:
            return 1.0
        return float(np.count_nonzero(self.converged) / len(self))

    def at(self, index: int) -> ImpliedVolResult:
        vol = self.implied_volatility.flat[index]
        uncertainty = self.uncertainty.flat[index]
        vega = self.vega.flat[index]
        return ImpliedVolResult(
            implied_volatility=None if np.isnan(vol) else float(vol),
            converged=bool(self.converged.flat[index]),
            iterations=int(self.iterations.flat[index]),
            lower_bound=MIN_VOL,
            upper_bound=MAX_VOL,
            solver=str(self.solver.flat[index]),
            error=self.errors[index],
            residual=float(self.residual.flat[index]),
            vega=None if np.isnan(vega) else float(vega),
            uncertainty=None if np.isnan(uncertainty) else float(uncertainty),
        )


def _initial_guess(target: np.ndarray, forward: np.ndarray, tau: np.ndarray) -> np.ndarray:
    """Brenner-Subrahmanyam at-the-money approximation, clipped into the bracket.

    ``sigma ~ sqrt(2 pi / tau) * price / forward`` is exact only at the money,
    but the safeguarded solver bisects whenever a Newton step escapes its
    bracket, so a rough guess costs iterations rather than correctness.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        guess = np.sqrt(2.0 * np.pi / np.maximum(tau, 1e-12)) * target / np.maximum(forward, 1e-12)
    guess = np.where(np.isfinite(guess), guess, 0.2)
    return np.clip(guess, 0.01, 3.0)


def _price_residual(forward: float, strike: float, tau: float, is_call: bool, target: float):
    """Build a scalar objective bound to these values.

    A closure defined inside the fallback loop would capture the loop variables
    by reference; binding them here makes the function safe to hold on to.
    """

    def residual(sigma: float) -> float:
        return float(black76_price(forward, strike, tau, sigma, is_call)) - target

    return residual


def implied_vol_black76_batch(
    price: np.ndarray,
    forward: np.ndarray,
    strike: np.ndarray,
    tau: np.ndarray,
    is_call: np.ndarray,
    discount_factor: np.ndarray | float = 1.0,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    price_abs_tol: float = PRICE_ABS_TOL,
    price_resolution: np.ndarray | float | None = None,
) -> BatchImpliedVolResult:
    """Solve a whole option chain at once.

    Vectorized safeguarded Newton for the bulk, with a scalar bracketed Brent
    fallback for any element that has not converged. The fallback matters: a
    handful of deep-wing quotes per chain sit where vega underflows, and they
    are exactly the quotes whose implied volatility a naive solver reports
    confidently and wrongly.

    ``price_resolution`` is how finely the input price is actually known — half
    a spread, or a tick for a locked market. Supplying it turns ``uncertainty``
    from a statement about float64 into a statement about the market.
    """
    price_arr, forward_arr, strike_arr, tau_arr, discount = np.broadcast_arrays(
        np.asarray(price, dtype=float),
        np.asarray(forward, dtype=float),
        np.asarray(strike, dtype=float),
        np.asarray(tau, dtype=float),
        np.asarray(discount_factor, dtype=float),
    )
    call_mask = np.broadcast_to(np.asarray(is_call, dtype=bool), price_arr.shape)
    size = price_arr.size

    vols = np.full(price_arr.shape, np.nan)
    converged = np.zeros(price_arr.shape, dtype=bool)
    iterations = np.zeros(price_arr.shape, dtype=int)
    residuals = np.full(price_arr.shape, np.nan)
    vegas = np.full(price_arr.shape, np.nan)
    uncertainties = np.full(price_arr.shape, np.nan)
    solvers = np.full(price_arr.shape, "", dtype=object)
    errors: list[IVFailure | None] = [None] * size

    def mark(mask: np.ndarray, failure: IVFailure) -> None:
        for index in np.flatnonzero(mask.reshape(-1)):
            if errors[index] is None:
                errors[index] = failure

    # ---------------------------------------------------------- screening
    with np.errstate(divide="ignore", invalid="ignore"):
        target = price_arr / np.where(discount > 0, discount, np.nan)
    lower_bound, upper_bound = black76_bounds(forward_arr, strike_arr, call_mask)

    invalid = ~np.isfinite(target) | ~np.isfinite(forward_arr) | (forward_arr <= 0)
    invalid |= ~np.isfinite(strike_arr) | (strike_arr <= 0)
    mark(invalid, IVFailure.INVALID_INPUT)

    expired = ~invalid & (tau_arr <= 0)
    mark(expired, IVFailure.OPTION_EXPIRED)

    remaining = ~invalid & ~expired
    non_positive = remaining & (target <= 0)
    mark(non_positive, IVFailure.NON_POSITIVE_PRICE)
    remaining &= ~non_positive

    below = remaining & (target < lower_bound - price_abs_tol)
    mark(below, IVFailure.PRICE_BELOW_INTRINSIC)
    remaining &= ~below

    above = remaining & (target > upper_bound + price_abs_tol)
    mark(above, IVFailure.PRICE_ABOVE_BOUND)
    remaining &= ~above

    # A price sitting on its intrinsic bound has no time value; the implied
    # volatility is zero in the limit and any positive answer is fabricated.
    no_time_value = remaining & (target <= lower_bound + TIME_VALUE_EPS)
    mark(no_time_value, IVFailure.NO_TIME_VALUE)
    remaining &= ~no_time_value

    if not remaining.any():
        return BatchImpliedVolResult(
            vols, converged, iterations, errors, solvers, residuals, vegas, uncertainties
        )

    # ------------------------------------------------------------- solve
    def objective(sigma: np.ndarray) -> np.ndarray:
        return black76_price(forward_arr, strike_arr, tau_arr, sigma, call_mask) - target

    def derivative(sigma: np.ndarray) -> np.ndarray:
        return black76_vega(forward_arr, strike_arr, tau_arr, sigma)

    newton = safeguarded_newton(
        objective,
        derivative,
        _initial_guess(target, forward_arr, tau_arr),
        np.full(price_arr.shape, MIN_VOL),
        np.full(price_arr.shape, MAX_VOL),
        abs_tol=price_abs_tol,
        max_iterations=max_iterations,
        active=remaining,
    )

    solved = remaining & newton.converged
    vols = np.where(solved, newton.roots, vols)
    converged |= solved
    iterations = np.where(remaining, newton.iterations, iterations)
    residuals = np.where(solved, newton.residuals, residuals)
    solvers = np.where(solved, "safeguarded-newton", solvers)

    # --------------------------------------------------------- fallback
    stubborn = remaining & ~solved
    for index in np.flatnonzero(stubborn.reshape(-1)):
        flat = np.unravel_index(index, price_arr.shape)
        f_scalar = float(forward_arr[flat])
        k_scalar = float(strike_arr[flat])
        t_scalar = float(tau_arr[flat])
        call_scalar = bool(call_mask[flat])
        target_scalar = float(target[flat])

        scalar_objective = _price_residual(f_scalar, k_scalar, t_scalar, call_scalar, target_scalar)

        upper = MAX_VOL
        result = brent(scalar_objective, MIN_VOL, upper, abs_tol=price_abs_tol)
        if not result.converged:
            upper = EXPANDED_MAX_VOL
            result = brent(scalar_objective, MIN_VOL, upper, abs_tol=price_abs_tol)

        iterations[flat] += result.iterations
        if result.converged:
            vols[flat] = result.root
            converged[flat] = True
            residuals[flat] = result.residual
            solvers[flat] = "brent"
        else:
            errors[index] = IVFailure.NO_CONVERGENCE
            solvers[flat] = "brent"

    # -------------------------------------------------- conditioning report
    # How much volatility one unit of price resolution would move. The floor is
    # a float64 ulp taken at the scale of the option's upper bound, because that
    # is where the cancellation in F*N(d1) - K*N(d2) actually happens. A caller
    # that knows the tick or the spread supplies the larger, economic figure,
    # which is usually many orders of magnitude bigger and is what decides
    # whether a quote should inform a surface.
    solved_any = converged & np.isfinite(vols)
    if solved_any.any():
        vegas = np.where(
            solved_any,
            black76_vega(forward_arr, strike_arr, tau_arr, np.nan_to_num(vols, nan=0.1)),
            np.nan,
        )
        resolution = np.spacing(np.maximum(np.abs(target), upper_bound))
        if price_resolution is not None:
            supplied = np.broadcast_to(np.asarray(price_resolution, dtype=float), price_arr.shape)
            with np.errstate(divide="ignore", invalid="ignore"):
                # Undiscounted, to match the space the solve happens in.
                supplied = np.where(discount > 0, supplied / discount, supplied)
            resolution = np.maximum(resolution, np.where(np.isfinite(supplied), supplied, 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            uncertainties = np.where(solved_any & (vegas > 0), resolution / vegas, np.inf)
        uncertainties = np.where(solved_any, uncertainties, np.nan)

    return BatchImpliedVolResult(
        vols, converged, iterations, errors, solvers, residuals, vegas, uncertainties
    )


def implied_vol_black76(
    price: float,
    forward: float,
    strike: float,
    tau: float,
    is_call: bool,
    discount_factor: float = 1.0,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    price_abs_tol: float = PRICE_ABS_TOL,
) -> ImpliedVolResult:
    """Single-quote inversion. Same code path as the batch solver."""
    batch = implied_vol_black76_batch(
        np.array([price]),
        np.array([forward]),
        np.array([strike]),
        np.array([tau]),
        np.array([is_call]),
        np.array([discount_factor]),
        max_iterations=max_iterations,
        price_abs_tol=price_abs_tol,
    )
    result = batch.at(0)
    return ImpliedVolResult(
        implied_volatility=result.implied_volatility,
        converged=result.converged,
        iterations=result.iterations,
        lower_bound=MIN_VOL,
        upper_bound=MAX_VOL,
        solver=result.solver or "none",
        error=result.error,
        price_used=price,
        residual=result.residual,
        vega=result.vega,
        uncertainty=result.uncertainty,
    )


def implied_vol_bsm(
    price: float,
    spot: float,
    strike: float,
    tau: float,
    rate: float,
    dividend: float,
    is_call: bool,
) -> ImpliedVolResult:
    """Spot-parameterized inversion.

    Converts to the forward and defers: Black-76 and Black-Scholes-Merton imply
    the *same* volatility for the same price, which is asserted as a test rather
    than assumed.
    """
    import math

    if tau <= 0:
        return ImpliedVolResult(
            implied_volatility=None,
            converged=False,
            iterations=0,
            lower_bound=MIN_VOL,
            upper_bound=MAX_VOL,
            solver="none",
            error=IVFailure.OPTION_EXPIRED,
            price_used=price,
        )
    forward = spot * math.exp((rate - dividend) * tau)
    discount = math.exp(-rate * tau)
    return implied_vol_black76(price, forward, strike, tau, is_call, discount)
