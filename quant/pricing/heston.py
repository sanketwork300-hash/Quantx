"""Heston (1993) by characteristic function, with the little-trap branch.

    dS = (r - q) S dt + sqrt(v) S dW1
    dv = kappa (theta - v) dt + xi sqrt(v) dW2,   d<W1, W2> = rho dt

`docs/pricing.md` listed this pricer as QuantLib-wrapped. It is implemented
directly instead, because the standing rule for core numerics is to implement
from the specification and validate against the library in tests — two
independent implementations that agree is a stronger guarantee than a wrapper,
and it keeps QuantLib a test dependency rather than a runtime one. The
cross-check against QuantLib is the phase's own acceptance criterion, so the
decision is testable rather than a matter of taste.

**The little Heston trap.** The naive branch of the complex logarithm in the
characteristic function crosses a discontinuity for long maturities, and the
price silently becomes wrong — not noisy, wrong, and smoothly so. Albrecher et
al. (2007) showed that writing the exponent with ``(b - rho*xi*i*u - d)`` and
``c = 1/g`` keeps the logarithm on its principal branch for every maturity. That
is the formulation here, and a test prices a ten-year option to make sure the
trap stays sprung.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

import numpy as np

#: Floor for the upper limit of the numerical integration. The integrand decays
#: like ``exp(-u^2 v tau / 2)``, so how far it must be carried depends on the
#: *maturity*: a fixed truncation is an unstated assumption that the tail has
#: already died, and for a one-week option under a 20% variance it has not.
#: Measured against QuantLib: truncating a seven-day call at 200 is wrong in the
#: seventh significant figure, and at the adaptive limit below it is right to
#: fifteen.
INTEGRATION_LIMIT = 200.0

#: Multiples of ``1 / sqrt(v tau)`` carried before truncating. At 14 the
#: Gaussian factor is ``exp(-98)``, far below float64 resolution.
INTEGRATION_DECAY_MULTIPLE = 14.0

#: The adaptive limit is rounded up to a multiple of this, so that a chain of
#: contracts at similar maturities shares one cached quadrature rather than
#: rebuilding it per strike.
INTEGRATION_LIMIT_QUANTUM = 50.0

#: Beyond this the contract is seconds from expiry and the integral is not the
#: thing that is wrong with the number.
MAX_INTEGRATION_LIMIT = 5_000.0

#: Gauss-Legendre nodes. Enough that doubling them changes nothing at 1e-12,
#: which a test asserts rather than assumes.
INTEGRATION_NODES = 256

#: Below this the variance process is treated as deterministic and the result
#: says so. The characteristic function carries ``1 / xi^2``, so a vanishing
#: vol-of-vol is ill-conditioned rather than merely uninteresting: measured
#: against Black-Scholes at the same variance, the price converges as ``xi^2``
#: down to about ``xi = 1e-4`` — the error there is 2e-8 — and then *worsens*,
#: reaching 8e-4 at ``xi = 1e-7``. A caller who wants the deterministic-variance
#: limit should price it with Black-Scholes, which is what the limit is.
MIN_VOL_OF_VOL = 1e-8


class HestonError(ValueError):
    pass


class HestonWarning(StrEnum):
    FELLER_VIOLATED = "HESTON_FELLER_CONDITION_VIOLATED"
    NEAR_DETERMINISTIC = "HESTON_NEAR_DETERMINISTIC_VARIANCE"
    EXTREME_CORRELATION = "HESTON_EXTREME_CORRELATION"


@dataclass(frozen=True, slots=True)
class HestonParameters:
    """The five parameters, with the Feller condition reported not enforced.

    ``2 kappa theta > xi^2`` keeps the variance process strictly positive. Real
    calibrations routinely violate it, and refusing those fits would mean
    refusing the market's own answer; the condition is therefore reported as a
    warning on every result computed from parameters that break it, and the
    characteristic function is valid either way.
    """

    v0: float
    kappa: float
    theta: float
    xi: float
    rho: float

    def __post_init__(self) -> None:
        if self.v0 <= 0:
            raise HestonError(f"initial variance must be positive, got {self.v0}")
        if self.kappa <= 0:
            raise HestonError(f"mean reversion must be positive, got {self.kappa}")
        if self.theta <= 0:
            raise HestonError(f"long-run variance must be positive, got {self.theta}")
        if self.xi <= 0:
            raise HestonError(f"volatility of variance must be positive, got {self.xi}")
        if not -1.0 < self.rho < 1.0:
            raise HestonError(f"correlation must lie in (-1, 1), got {self.rho}")

    @property
    def feller(self) -> float:
        """``2 kappa theta - xi^2``. Positive means the variance stays positive."""
        return 2.0 * self.kappa * self.theta - self.xi * self.xi

    @property
    def satisfies_feller(self) -> bool:
        return self.feller > 0.0

    def warnings(self) -> tuple[str, ...]:
        found: list[str] = []
        if not self.satisfies_feller:
            found.append(HestonWarning.FELLER_VIOLATED)
        if self.xi < MIN_VOL_OF_VOL:
            found.append(HestonWarning.NEAR_DETERMINISTIC)
        if abs(self.rho) > 0.95:
            found.append(HestonWarning.EXTREME_CORRELATION)
        return tuple(found)

    def to_dict(self) -> dict:
        return {
            "v0": self.v0,
            "kappa": self.kappa,
            "theta": self.theta,
            "xi": self.xi,
            "rho": self.rho,
            "feller": self.feller,
            "satisfies_feller": self.satisfies_feller,
            "feller_note": (
                "2*kappa*theta - xi^2. Positive keeps the variance process "
                "strictly positive. A negative value is reported, not refused: "
                "real calibrations violate it routinely and refusing them would "
                "mean refusing the market's own answer."
            ),
        }


def integration_limit(tau: float, parameters: HestonParameters) -> float:
    """How far the characteristic-function integral must be carried.

    The integrand's Gaussian factor is ``exp(-u^2 v tau / 2)``, so the useful
    range scales as ``1 / sqrt(v tau)``. The larger of ``v0`` and ``theta`` is
    used because the variance travels between them over the option's life and
    the *smaller* one would truncate too early.
    """
    scale = math.sqrt(max(parameters.v0, parameters.theta) * max(tau, 1e-12))
    raw = INTEGRATION_DECAY_MULTIPLE / max(scale, 1e-12)
    rounded = math.ceil(raw / INTEGRATION_LIMIT_QUANTUM) * INTEGRATION_LIMIT_QUANTUM
    return float(min(max(rounded, INTEGRATION_LIMIT), MAX_INTEGRATION_LIMIT))


@lru_cache(maxsize=32)
def _quadrature(nodes: int, limit: float) -> tuple[np.ndarray, np.ndarray]:
    """Gauss-Legendre nodes and weights, cached because they never change."""
    points, weights = np.polynomial.legendre.leggauss(nodes)
    half = limit / 2.0
    return half * (points + 1.0), half * weights


def characteristic_function(
    u: np.ndarray,
    tau: float,
    rate: float,
    dividend: float,
    parameters: HestonParameters,
    index: int,
) -> np.ndarray:
    """``f_1`` or ``f_2`` from Heston (1993), in the little-trap form.

    ``index`` selects the measure: 1 is the share measure that gives ``P1``, 2
    the risk-neutral one that gives ``P2``.
    """
    if index not in (1, 2):
        raise HestonError("the characteristic function index must be 1 or 2")

    u = np.asarray(u, dtype=complex)
    kappa, theta, xi, rho, v0 = (
        parameters.kappa,
        parameters.theta,
        parameters.xi,
        parameters.rho,
        parameters.v0,
    )
    u_j = 0.5 if index == 1 else -0.5
    b_j = kappa - rho * xi if index == 1 else kappa

    iu = 1j * u
    rho_xi_iu = rho * xi * iu
    d = np.sqrt((rho_xi_iu - b_j) ** 2 - xi * xi * (2.0 * u_j * iu - u * u))

    # The little-trap branch: the minus root and c = 1/g together keep the
    # logarithm below on its principal branch for every maturity.
    numerator = b_j - rho_xi_iu - d
    denominator = b_j - rho_xi_iu + d
    c = numerator / denominator

    exp_dt = np.exp(-d * tau)
    log_term = np.log((1.0 - c * exp_dt) / (1.0 - c))

    big_c = (rate - dividend) * iu * tau + (kappa * theta / (xi * xi)) * (
        numerator * tau - 2.0 * log_term
    )
    big_d = (numerator / (xi * xi)) * (1.0 - exp_dt) / (1.0 - c * exp_dt)
    return np.exp(big_c + big_d * v0)


def _probability(
    spot: float,
    strike: float,
    tau: float,
    rate: float,
    dividend: float,
    parameters: HestonParameters,
    index: int,
    nodes: int,
    limit: float,
) -> float:
    """``P_j``, the in-the-money probability under measure ``j``."""
    u, weights = _quadrature(nodes, limit)
    log_spot = math.log(spot)
    log_strike = math.log(strike)

    phi = characteristic_function(u, tau, rate, dividend, parameters, index)
    integrand = np.real(np.exp(-1j * u * log_strike + 1j * u * log_spot) * phi / (1j * u))
    return 0.5 + float(np.sum(integrand * weights)) / math.pi


def heston_price(
    spot: float,
    strike: float,
    tau: float,
    rate: float,
    dividend: float,
    parameters: HestonParameters,
    is_call: bool = True,
    nodes: int = INTEGRATION_NODES,
    limit: float | None = None,
) -> float:
    """European vanilla under Heston.

    Puts come from put-call parity rather than a second integration: parity is
    exact for European options under any risk-neutral model, and using it means
    a call and a put priced from the same parameters cannot drift apart through
    two separate quadratures.

    ``limit`` defaults to :func:`integration_limit`, which scales the truncation
    with the maturity. Passing one fixes it, which is useful for showing that
    the default is doing something.
    """
    if spot <= 0 or strike <= 0:
        raise HestonError("spot and strike must be positive")
    if tau < 0:
        raise HestonError("time to expiry cannot be negative")
    if tau == 0:
        intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
        return intrinsic

    ceiling = integration_limit(tau, parameters) if limit is None else float(limit)
    p1 = _probability(spot, strike, tau, rate, dividend, parameters, 1, nodes, ceiling)
    p2 = _probability(spot, strike, tau, rate, dividend, parameters, 2, nodes, ceiling)

    carry = math.exp(-dividend * tau)
    discount = math.exp(-rate * tau)
    call = spot * carry * p1 - strike * discount * p2
    call = max(call, 0.0)

    if is_call:
        return call
    return call - spot * carry + strike * discount


def heston_implied_volatility(
    spot: float,
    strike: float,
    tau: float,
    rate: float,
    dividend: float,
    parameters: HestonParameters,
    is_call: bool = True,
) -> float | None:
    """The Black-Scholes volatility that reproduces the Heston price.

    ``None`` when the price is outside the invertible range rather than a
    clipped number, which is the same contract the Phase 1 inverter honours.
    """
    from quant.volatility.implied import implied_vol_black76

    price = heston_price(spot, strike, tau, rate, dividend, parameters, is_call)
    forward = spot * math.exp((rate - dividend) * tau)
    discount = math.exp(-rate * tau)

    result = implied_vol_black76(price, forward, strike, tau, is_call, discount)
    return result.implied_volatility


def heston_smile(
    spot: float,
    strikes: np.ndarray,
    tau: float,
    rate: float,
    dividend: float,
    parameters: HestonParameters,
) -> np.ndarray:
    """Implied volatilities across strikes. ``nan`` where none exists."""
    return np.array(
        [
            (
                value
                if (
                    value := heston_implied_volatility(
                        spot, float(strike), tau, rate, dividend, parameters, True
                    )
                )
                is not None
                else np.nan
            )
            for strike in np.asarray(strikes, dtype=float)
        ],
        dtype=float,
    )
