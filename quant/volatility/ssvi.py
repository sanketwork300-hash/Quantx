"""SSVI: one arbitrage-aware surface instead of a slice per expiry.

Raw SVI (Phase 2) fits each expiry independently, which means five parameters
per slice and no structural reason for two neighbouring expiries to be
consistent with each other. Calendar arbitrage is then something to *detect*.

SSVI (Gatheral & Jacquier, *Arbitrage-free SVI volatility surfaces*, 2014)
replaces that with

    w(k, theta) = (theta / 2) * { 1 + rho * phi(theta) * k
                                  + sqrt[ (phi(theta) k + rho)^2 + (1 - rho^2) ] }

where ``theta(T)`` is the at-the-money total variance term structure and ``phi``
is a smooth function of it. Three global parameters plus a term structure, and
two structural properties fall out:

* **Calendar arbitrage is impossible when ``theta`` is non-decreasing in T.**
  That is enforced by construction, not checked afterwards.
* **Butterfly arbitrage has sufficient conditions in closed form** (Theorem 4.2):
  ``theta*phi*(1+|rho|) < 4`` and ``theta*phi^2*(1+|rho|) <= 4``.

Those conditions are *sufficient*, not necessary, so this module also evaluates
Durrleman's ``g(k) >= 0`` numerically — the actual condition — and reports both.
Trusting a sufficient condition alone would mean silently rejecting admissible
surfaces and, worse, believing a proof rather than the surface in front of us.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

#: Total variance below this is treated as zero: a slice with no variance in it
#: has no smile, and dividing by it produces confident nonsense.
MIN_TOTAL_VARIANCE = 1e-12

#: The butterfly bound from Gatheral & Jacquier Theorem 4.2.
BUTTERFLY_BOUND = 4.0

#: How far outside the fitted maturity range a lookup may go before it is
#: reported as extrapolation rather than interpolation.
MATURITY_TOLERANCE = 1e-9


class SSVIError(ValueError):
    pass


class SSVIFlag(StrEnum):
    EXTRAPOLATED_MATURITY = "SSVI_EXTRAPOLATED_MATURITY"
    NON_MONOTONE_THETA = "SSVI_NON_MONOTONE_ATM_VARIANCE"
    BUTTERFLY_BOUND_VIOLATED = "SSVI_BUTTERFLY_BOUND_VIOLATED"
    DURRLEMAN_NEGATIVE = "SSVI_DURRLEMAN_NEGATIVE"
    DEGENERATE_SLICE = "SSVI_DEGENERATE_SLICE"


@dataclass(frozen=True, slots=True)
class SSVIParameters:
    """The three global shape parameters.

    ``rho`` tilts the smile, ``eta`` scales its curvature and ``gamma`` governs
    how that curvature decays with maturity. There is one set of these for the
    whole surface, which is the point: a wing that steepens with maturity is a
    statement the surface makes once, not five parameters per expiry that happen
    to line up.
    """

    rho: float
    eta: float
    gamma: float

    def __post_init__(self) -> None:
        if not -1.0 < self.rho < 1.0:
            raise SSVIError(f"rho must lie in (-1, 1), got {self.rho}")
        if self.eta <= 0.0:
            raise SSVIError(f"eta must be positive, got {self.eta}")
        if not 0.0 < self.gamma < 1.0:
            raise SSVIError(f"gamma must lie in (0, 1), got {self.gamma}")

    def phi(self, theta: float | np.ndarray) -> np.ndarray:
        """Power-law ``phi``: eta / (theta^gamma (1 + theta)^(1 - gamma)).

        The ``(1 + theta)`` factor is what keeps ``theta * phi`` bounded as
        maturity grows, which is what keeps the butterfly condition satisfiable
        at the long end rather than only near the front.
        """
        theta = np.maximum(np.asarray(theta, dtype=float), MIN_TOTAL_VARIANCE)
        return self.eta / (theta**self.gamma * (1.0 + theta) ** (1.0 - self.gamma))

    def phi_derivative(self, theta: float | np.ndarray) -> np.ndarray:
        """d(phi)/d(theta), needed for the maturity derivative of w."""
        theta = np.maximum(np.asarray(theta, dtype=float), MIN_TOTAL_VARIANCE)
        return self.phi(theta) * (-self.gamma / theta - (1.0 - self.gamma) / (1.0 + theta))

    def to_dict(self) -> dict:
        return {"rho": self.rho, "eta": self.eta, "gamma": self.gamma}


@dataclass(frozen=True, slots=True)
class ThetaTermStructure:
    """At-the-money total variance as a function of maturity.

    Stored as observed knots and interpolated **monotonically**, because a
    non-decreasing ``theta`` is exactly the no-calendar-arbitrage condition for
    SSVI. Monotone interpolation between non-decreasing knots cannot introduce a
    dip, so the property holds everywhere and not merely at the knots.
    """

    maturities: tuple[float, ...]
    thetas: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.maturities) != len(self.thetas):
            raise SSVIError("theta term structure needs one variance per maturity")
        if len(self.maturities) < 1:
            raise SSVIError("a term structure needs at least one maturity")
        if any(t <= 0 for t in self.maturities):
            raise SSVIError("maturities must be positive")
        if any(theta <= 0 for theta in self.thetas):
            raise SSVIError("at-the-money total variance must be positive")
        if list(self.maturities) != sorted(self.maturities):
            raise SSVIError("maturities must be supplied in increasing order")

    @property
    def is_monotone(self) -> bool:
        return all(
            later >= earlier - 1e-12
            for earlier, later in zip(self.thetas, self.thetas[1:], strict=False)
        )

    @property
    def start(self) -> float:
        return self.maturities[0]

    @property
    def end(self) -> float:
        return self.maturities[-1]

    def _interpolator(self):
        from scipy.interpolate import PchipInterpolator

        return PchipInterpolator(
            np.asarray(self.maturities), np.asarray(self.thetas), extrapolate=False
        )

    def theta(self, maturity: float | np.ndarray) -> np.ndarray:
        """Monotone piecewise-cubic between the knots; the two ends differ.

        **Before the first expiry** total variance is taken proportional to
        maturity, running down to ``theta(0) = 0``. That is not an
        extrapolation of a fitted slope: a zero-length period has zero variance,
        so the origin is a boundary condition, and a straight line to it is the
        flat-implied-volatility reading of the front slice. Clamping flat
        instead would make ``dtheta/dT`` zero over the whole front, which Dupire
        divides into and which would report the front of the surface as having
        no local volatility at all.

        **After the last expiry** it is flat, and that *is* a refusal to
        extrapolate: continuing a fitted slope past the last observed expiry
        invents a term structure, whereas a flat one cannot introduce calendar
        arbitrage. Local volatility past the end is consequently undefined and
        is reported as such rather than guessed.

        Both ends are flagged as extrapolation regardless.
        """
        maturity = np.asarray(maturity, dtype=float)
        if len(self.maturities) == 1:
            # One expiry pins a level and says nothing about the term structure,
            # so total variance is taken proportional to time, which is the flat
            # implied-volatility reading of a single slice.
            scale = self.thetas[0] / self.maturities[0]
            return np.maximum(maturity, 0.0) * scale

        inside = self._interpolator()(np.clip(maturity, self.start, self.end))
        front_slope = self.thetas[0] / self.maturities[0]
        return np.where(
            maturity < self.start,
            np.maximum(maturity, 0.0) * front_slope,
            np.asarray(inside, dtype=float),
        )

    def theta_derivative(self, maturity: float | np.ndarray) -> np.ndarray:
        """``dtheta/dT``: the front slope before the first expiry, zero after
        the last, and the interpolant's own derivative in between."""
        maturity = np.asarray(maturity, dtype=float)
        front_slope = self.thetas[0] / self.maturities[0]
        if len(self.maturities) == 1:
            return np.full_like(maturity, front_slope, dtype=float)

        derivative = self._interpolator().derivative()(np.clip(maturity, self.start, self.end))
        derivative = np.asarray(derivative, dtype=float)
        derivative = np.where(maturity < self.start, front_slope, derivative)
        return np.where(maturity > self.end, 0.0, derivative)

    def is_extrapolated(self, maturity: float) -> bool:
        return (
            maturity < self.start - MATURITY_TOLERANCE or maturity > self.end + MATURITY_TOLERANCE
        )

    def to_dict(self) -> dict:
        return {
            "maturities": list(self.maturities),
            "thetas": list(self.thetas),
            "is_monotone": self.is_monotone,
        }


def ssvi_total_variance(
    k: float | np.ndarray, theta: float | np.ndarray, parameters: SSVIParameters
) -> np.ndarray:
    """The SSVI surface in total-variance coordinates."""
    k = np.asarray(k, dtype=float)
    theta = np.maximum(np.asarray(theta, dtype=float), MIN_TOTAL_VARIANCE)
    phi = parameters.phi(theta)
    rho = parameters.rho

    shifted = phi * k + rho
    root = np.sqrt(shifted * shifted + (1.0 - rho * rho))
    return 0.5 * theta * (1.0 + rho * phi * k + root)


def ssvi_derivatives(
    k: float | np.ndarray, theta: float | np.ndarray, parameters: SSVIParameters
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(w, dw/dk, d2w/dk2)``, all analytic.

    Analytic rather than bumped because these feed Dupire's denominator, where a
    finite-difference error in the second derivative turns into a local
    volatility that is wrong by an amount nothing downstream can detect.
    """
    k = np.asarray(k, dtype=float)
    theta = np.maximum(np.asarray(theta, dtype=float), MIN_TOTAL_VARIANCE)
    phi = parameters.phi(theta)
    rho = parameters.rho

    shifted = phi * k + rho
    root = np.sqrt(shifted * shifted + (1.0 - rho * rho))

    w = 0.5 * theta * (1.0 + rho * phi * k + root)
    dw_dk = 0.5 * theta * phi * (rho + shifted / root)
    d2w_dk2 = 0.5 * theta * phi * phi * (1.0 - rho * rho) / root**3
    return w, dw_dk, d2w_dk2


def ssvi_theta_derivative(
    k: float | np.ndarray, theta: float | np.ndarray, parameters: SSVIParameters
) -> np.ndarray:
    """``dw/dtheta`` at fixed ``k``, which carries the maturity dependence."""
    k = np.asarray(k, dtype=float)
    theta = np.maximum(np.asarray(theta, dtype=float), MIN_TOTAL_VARIANCE)
    phi = parameters.phi(theta)
    phi_prime = parameters.phi_derivative(theta)
    rho = parameters.rho

    shifted = phi * k + rho
    root = np.sqrt(shifted * shifted + (1.0 - rho * rho))

    level = 0.5 * (1.0 + rho * phi * k + root)
    shape = 0.5 * theta * phi_prime * k * (rho + shifted / root)
    return level + shape


def ssvi_maturity_derivative(
    k: float | np.ndarray,
    maturity: float | np.ndarray,
    parameters: SSVIParameters,
    term_structure: ThetaTermStructure,
) -> np.ndarray:
    """``dw/dT`` by the chain rule through ``theta(T)``. Dupire's numerator."""
    theta = term_structure.theta(maturity)
    return ssvi_theta_derivative(k, theta, parameters) * term_structure.theta_derivative(maturity)


def butterfly_bounds(theta: float, parameters: SSVIParameters) -> tuple[float, float]:
    """The two Gatheral-Jacquier quantities, both of which must stay under 4."""
    phi = float(parameters.phi(theta))
    factor = 1.0 + abs(parameters.rho)
    return theta * phi * factor, theta * phi * phi * factor


def satisfies_butterfly_bounds(theta: float, parameters: SSVIParameters) -> bool:
    """Theorem 4.2's *sufficient* condition. Not the whole story, by design."""
    first, second = butterfly_bounds(theta, parameters)
    return first < BUTTERFLY_BOUND and second <= BUTTERFLY_BOUND


def durrleman_g(
    k: float | np.ndarray, theta: float | np.ndarray, parameters: SSVIParameters
) -> np.ndarray:
    """Durrleman's function. Non-negative everywhere is butterfly-free.

        g(k) = (1 - k w'/(2w))^2 - (w'/2)^2 (1/w + 1/4) + w''/2

    This is the actual condition, evaluated numerically, and it is checked
    alongside the closed-form bounds rather than instead of them: the bounds are
    sufficient, so a surface can fail them and still be admissible.
    """
    w, dw, d2w = ssvi_derivatives(k, theta, parameters)
    w = np.maximum(w, MIN_TOTAL_VARIANCE)

    first = (1.0 - np.asarray(k, dtype=float) * dw / (2.0 * w)) ** 2
    second = (dw * dw / 4.0) * (1.0 / w + 0.25)
    return first - second + d2w / 2.0


def min_durrleman_g(
    theta: float, parameters: SSVIParameters, k_range: float = 2.0, points: int = 401
) -> float:
    grid = np.linspace(-k_range, k_range, points)
    return float(np.min(durrleman_g(grid, theta, parameters)))


@dataclass(frozen=True, slots=True)
class SSVISurface:
    """A calibrated global surface, addressable at any (k, T)."""

    parameters: SSVIParameters
    term_structure: ThetaTermStructure

    def total_variance(self, k: float | np.ndarray, maturity: float | np.ndarray) -> np.ndarray:
        return ssvi_total_variance(k, self.term_structure.theta(maturity), self.parameters)

    def implied_volatility(self, k: float | np.ndarray, maturity: float | np.ndarray) -> np.ndarray:
        maturity = np.maximum(np.asarray(maturity, dtype=float), MIN_TOTAL_VARIANCE)
        return np.sqrt(np.maximum(self.total_variance(k, maturity), 0.0) / maturity)

    def derivatives(
        self, k: float | np.ndarray, maturity: float | np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """``(w, dw/dk, d2w/dk2, dw/dT)`` — everything Dupire needs, analytic."""
        theta = self.term_structure.theta(maturity)
        w, dw_dk, d2w_dk2 = ssvi_derivatives(k, theta, self.parameters)
        dw_dt = ssvi_maturity_derivative(k, maturity, self.parameters, self.term_structure)
        return w, dw_dk, d2w_dk2, dw_dt

    def flags(self, maturity: float, k_range: float = 2.0) -> tuple[str, ...]:
        """Everything wrong with the surface at this maturity, named."""
        found: list[str] = []
        if self.term_structure.is_extrapolated(maturity):
            found.append(SSVIFlag.EXTRAPOLATED_MATURITY)
        if not self.term_structure.is_monotone:
            found.append(SSVIFlag.NON_MONOTONE_THETA)

        theta = float(self.term_structure.theta(maturity))
        if theta <= MIN_TOTAL_VARIANCE:
            found.append(SSVIFlag.DEGENERATE_SLICE)
            return tuple(found)
        if not satisfies_butterfly_bounds(theta, self.parameters):
            found.append(SSVIFlag.BUTTERFLY_BOUND_VIOLATED)
        if min_durrleman_g(theta, self.parameters, k_range=k_range) < -1e-10:
            found.append(SSVIFlag.DURRLEMAN_NEGATIVE)
        return tuple(found)

    def is_arbitrage_free(self, maturities: Sequence[float], k_range: float = 2.0) -> bool:
        """Butterfly-free at every requested maturity, and calendar-free by shape."""
        if not self.term_structure.is_monotone:
            return False
        return all(
            min_durrleman_g(float(self.term_structure.theta(t)), self.parameters, k_range) >= -1e-10
            for t in maturities
        )

    def to_dict(self) -> dict:
        return {
            "model": "SSVI",
            "parameters": self.parameters.to_dict(),
            "term_structure": self.term_structure.to_dict(),
            "phi": (
                "power law: eta / (theta^gamma (1 + theta)^(1 - gamma)), which "
                "keeps theta*phi bounded as maturity grows"
            ),
        }


def implied_from_total_variance(w: np.ndarray, maturity: float) -> np.ndarray:
    if maturity <= 0:
        raise SSVIError("implied volatility needs a positive maturity")
    return np.sqrt(np.maximum(w, 0.0) / maturity)


def total_variance_from_implied(sigma: np.ndarray, maturity: float) -> np.ndarray:
    if maturity <= 0:
        raise SSVIError("total variance needs a positive maturity")
    return np.asarray(sigma, dtype=float) ** 2 * maturity


def atm_total_variance(parameters: SSVIParameters, theta: float) -> float:
    """SSVI is constructed so that ``w(0) = theta`` exactly. A useful check."""
    del parameters
    return theta
