"""Raw SVI parameterization (Gatheral).

    w(k) = a + b [ rho (k - m) + sqrt((k - m)^2 + sigma^2) ]

where ``k = ln(K / F)`` is log-moneyness and ``w`` is total implied variance
``sigma_BS^2 * tau``.

Reference: J. Gatheral, *The Volatility Surface* (2006); Gatheral & Jacquier,
*Arbitrage-free SVI volatility surfaces* (2014).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class SVIParameters:
    """Raw SVI parameters for one expiry slice.

    a
        Vertical level of total variance.
    b
        Overall slope; controls the angle between the wings. Must be >= 0.
    rho
        Slope asymmetry (skew) in (-1, 1).
    m
        Horizontal shift of the smile minimum.
    sigma
        Smile curvature at the minimum. Must be > 0.
    """

    a: float
    b: float
    rho: float
    m: float
    sigma: float

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.b < 0:
            raise ValueError(f"SVI requires b >= 0, got {self.b}")
        if not -1.0 < self.rho < 1.0:
            raise ValueError(f"SVI requires |rho| < 1, got {self.rho}")
        if self.sigma <= 0:
            raise ValueError(f"SVI requires sigma > 0, got {self.sigma}")
        minimum = self.minimum_total_variance()
        if minimum < 0:
            raise ValueError(
                "SVI parameters imply negative minimum total variance "
                f"({minimum:.6g}); a + b*sigma*sqrt(1 - rho^2) must be >= 0"
            )

    def minimum_total_variance(self) -> float:
        """``a + b sigma sqrt(1 - rho^2)``: the smallest ``w(k)`` the slice attains."""
        return self.a + self.b * self.sigma * math.sqrt(1.0 - self.rho * self.rho)

    def to_dict(self) -> dict:
        return asdict(self)


def raw_svi_total_variance(k: float | np.ndarray, params: SVIParameters) -> np.ndarray:
    k_arr = np.asarray(k, dtype=float)
    shifted = k_arr - params.m
    return np.asarray(
        params.a
        + params.b
        * (params.rho * shifted + np.sqrt(shifted * shifted + params.sigma * params.sigma)),
        dtype=float,
    )


def raw_svi_implied_vol(k: float | np.ndarray, tau: float, params: SVIParameters) -> np.ndarray:
    """Black implied volatility ``sqrt(w(k) / tau)``."""
    if tau <= 0:
        raise ValueError(f"tau must be positive, got {tau}")
    total_variance = raw_svi_total_variance(k, params)
    if np.any(total_variance < 0):
        raise ValueError("SVI produced negative total variance; parameters are inadmissible")
    return np.sqrt(total_variance / tau)


# --------------------------------------------------------------- derivatives
def raw_svi_derivatives(
    k: float | np.ndarray, params: SVIParameters
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(w, dw/dk, d2w/dk2)`` analytically.

    Analytic rather than finite-difference because the Durrleman condition
    divides by ``w`` and squares ``w'``: differencing noise there turns a
    perfectly admissible slice into a spurious butterfly violation.
    """
    k_arr = np.asarray(k, dtype=float)
    shifted = k_arr - params.m
    root = np.sqrt(shifted * shifted + params.sigma * params.sigma)

    w = params.a + params.b * (params.rho * shifted + root)
    dw = params.b * (params.rho + shifted / root)
    d2w = params.b * params.sigma * params.sigma / (root**3)
    return (
        np.asarray(w, dtype=float),
        np.asarray(dw, dtype=float),
        np.asarray(d2w, dtype=float),
    )


def durrleman_g(k: float | np.ndarray, params: SVIParameters) -> np.ndarray:
    """Durrleman's function. ``g(k) >= 0`` everywhere means no butterfly arbitrage.

        g(k) = (1 - k w'/(2w))^2 - (w'^2/4)(1/w + 1/4) + w''/2

    ``g`` is proportional to the risk-neutral density implied by the slice, so a
    negative region is literally a negative probability density. That is why
    this is the butterfly test rather than a discrete second difference: it is
    exact, independent of the strike grid, and interpretable.

    Reference: Gatheral & Jacquier, *Arbitrage-free SVI volatility surfaces*
    (2014), Lemma 2.2.
    """
    k_arr = np.asarray(k, dtype=float)
    w, dw, d2w = raw_svi_derivatives(k_arr, params)
    if np.any(w <= 0):
        raise ValueError("Durrleman's condition is undefined where total variance is <= 0")

    term = 1.0 - k_arr * dw / (2.0 * w)
    return np.asarray(term * term - (dw * dw / 4.0) * (1.0 / w + 0.25) + d2w / 2.0, dtype=float)


#: Lee's moment formula bounds the asymptotic slope of total variance in
#: log-moneyness at 2. For raw SVI the wing slopes are ``b(1 + rho)`` and
#: ``b(1 - rho)``, so an admissible slice needs ``b(1 + |rho|) <= 2``.
#: Reference: R. Lee, *The moment formula for implied volatility at extreme
#: strikes* (2004).
LEE_WING_BOUND = 2.0


def wing_slope(params: SVIParameters) -> float:
    """``b (1 + |rho|)``: the steeper of the two asymptotic wing slopes."""
    return float(params.b * (1.0 + abs(params.rho)))


def satisfies_lee_bound(params: SVIParameters, tolerance: float = 1e-9) -> bool:
    return wing_slope(params) <= LEE_WING_BOUND + tolerance


def is_butterfly_free(
    params: SVIParameters,
    k_min: float = -3.0,
    k_max: float = 3.0,
    points: int = 601,
) -> bool:
    """Check ``g(k) >= 0`` on a grid, together with the wing bound.

    A grid check, not a proof: ``g`` is smooth and a fine grid over a wide
    log-moneyness range is what practitioners use, but a violation confined
    between two grid points would be missed. The grid is a parameter so a
    caller can tighten it, and the *magnitude* of any violation found is
    reported rather than a bare boolean.
    """
    if not satisfies_lee_bound(params):
        return False
    grid = np.linspace(k_min, k_max, points)
    return bool(np.all(durrleman_g(grid, params) >= -1e-12))


def min_durrleman_g(
    params: SVIParameters,
    k_min: float = -3.0,
    k_max: float = 3.0,
    points: int = 601,
) -> tuple[float, float]:
    """Return ``(min g, k at the minimum)`` over the grid."""
    grid = np.linspace(k_min, k_max, points)
    values = durrleman_g(grid, params)
    index = int(np.argmin(values))
    return float(values[index]), float(grid[index])


def raw_svi_vol_derivatives(
    k: float | np.ndarray, tau: float, params: SVIParameters
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(sigma, dsigma/dk, d2sigma/dk2)`` analytically.

    Calibration happens in total variance, but the level, skew and curvature a
    reader recognises are properties of *volatility*. Converting:

        sigma^2 = w / tau
        sigma'  = w' / (2 tau sigma)
        sigma'' = (w''/tau - 2 sigma'^2) / (2 sigma)

    Analytic rather than differencing the fitted curve, so a slice's
    characteristics are exact functions of its five parameters and reproduce
    from storage like everything else on a surface.
    """
    if tau <= 0:
        raise ValueError(f"tau must be positive, got {tau}")

    w, dw, d2w = raw_svi_derivatives(k, params)
    if np.any(w <= 0):
        raise ValueError("volatility derivatives are undefined where total variance is <= 0")

    sigma = np.sqrt(w / tau)
    dsigma = dw / (2.0 * tau * sigma)
    d2sigma = (d2w / tau - 2.0 * dsigma * dsigma) / (2.0 * sigma)
    return (
        np.asarray(sigma, dtype=float),
        np.asarray(dsigma, dtype=float),
        np.asarray(d2sigma, dtype=float),
    )
