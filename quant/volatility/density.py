"""Breeden-Litzenberger: the risk-neutral density implied by a surface.

    f(K) = e^{rT} d2C/dK2

**This is a risk-neutral density, and it is never a forecast.** It says what
distribution today's option prices are consistent with under the pricing
measure, which differs from the physical measure by a risk premium nobody here
has measured. Every payload from this module says so in its own words, and a
test asserts the word "forecast" appears nowhere near it.

What it is genuinely for is diagnosis. A negative region is not a market view,
it is evidence of butterfly arbitrage in the quotes or over-fitting in the
surface — and finding those is the job. So the density is evaluated on the
**fitted** surface, never on raw quotes, and negative regions are reported as
findings rather than clipped to zero.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from quant.pricing.black76 import black76_price

#: Relative bump in strike for the second difference. Large enough that the
#: fourth-order cancellation error stays well above float64 noise, small enough
#: that the O(h^2) truncation error is negligible on a smooth fitted surface.
DEFAULT_RELATIVE_BUMP = 1e-3

#: A density this far below zero is a finding rather than rounding.
NEGATIVE_TOLERANCE = -1e-10


class DensityFlag(StrEnum):
    NEGATIVE_REGION = "DENSITY_NEGATIVE_REGION"
    POOR_NORMALISATION = "DENSITY_DOES_NOT_INTEGRATE_TO_ONE"
    MEAN_AWAY_FROM_FORWARD = "DENSITY_MEAN_AWAY_FROM_FORWARD"
    NARROW_STRIKE_RANGE = "DENSITY_NARROW_STRIKE_RANGE"


@dataclass(frozen=True, slots=True)
class RiskNeutralDensity:
    """A density over terminal prices, and what is wrong with it."""

    strikes: np.ndarray
    density: np.ndarray
    forward: float
    maturity: float
    discount_factor: float
    #: Trapezoidal integral. One on an admissible surface over a wide range.
    total_mass: float
    #: The density's own mean, which must be the forward under the martingale
    #: property. Divergence measures how much the tails were truncated.
    implied_mean: float
    negative_mass: float
    flags: tuple[str, ...] = ()

    @property
    def is_admissible(self) -> bool:
        """Non-negative **and** normalised.

        Both conditions matter, for different reasons. A negative region means
        the fitted surface implies a distribution that cannot exist. A mass
        materially away from one means the strike range does not contain the
        distribution, so a quantile read off it — which normalises by the mass
        it happens to have — would be a quantile of the *window* rather than of
        the density, and would look perfectly reasonable while being wrong.
        """
        return (
            DensityFlag.NEGATIVE_REGION not in self.flags
            and DensityFlag.POOR_NORMALISATION not in self.flags
        )

    @property
    def mean_error(self) -> float:
        return (self.implied_mean - self.forward) / self.forward if self.forward else 0.0

    def percentile(self, probability: float) -> float | None:
        """A strike below which the risk-neutral mass is ``probability``.

        ``None`` when the density is inadmissible: reading a quantile off a
        curve that goes negative would produce a number with no meaning.
        """
        if not 0.0 < probability < 1.0:
            raise ValueError("a percentile needs a probability in (0, 1)")
        if not self.is_admissible or self.total_mass <= 0:
            return None
        cumulative = np.cumsum(
            np.concatenate(
                ([0.0], 0.5 * (self.density[1:] + self.density[:-1]) * np.diff(self.strikes))
            )
        )
        cumulative = cumulative / cumulative[-1]
        return float(np.interp(probability, cumulative, self.strikes))

    def to_dict(self, include_points: bool = False) -> dict:
        payload = {
            "forward": self.forward,
            "maturity": self.maturity,
            "discount_factor": self.discount_factor,
            "points": int(self.strikes.size),
            "strike_range": [float(self.strikes[0]), float(self.strikes[-1])],
            "total_mass": self.total_mass,
            "implied_mean": self.implied_mean,
            "mean_error": self.mean_error,
            "negative_mass": self.negative_mass,
            "is_admissible": self.is_admissible,
            "flags": list(self.flags),
            "interpretation": (
                "The distribution today's option prices are consistent with under "
                "the risk-neutral measure. It is not a forecast of where the "
                "market will go: the risk-neutral and physical measures differ by "
                "a risk premium this platform has not measured. Negative regions "
                "are evidence of residual arbitrage in the quotes or over-fitting "
                "in the surface, which is what this diagnostic is for."
            ),
        }
        if include_points:
            payload["strikes"] = [float(value) for value in self.strikes]
            payload["density"] = [float(value) for value in self.density]
        return payload


def risk_neutral_density(
    strikes: np.ndarray | list[float],
    implied_volatility: Callable[[np.ndarray], np.ndarray],
    forward: float,
    maturity: float,
    discount_factor: float = 1.0,
    relative_bump: float = DEFAULT_RELATIVE_BUMP,
) -> RiskNeutralDensity:
    """Second strike-derivative of the call surface, evaluated on the fit.

    ``implied_volatility`` must be the *fitted* surface. Passing raw quotes here
    would produce second differences of noise, which is exactly the failure the
    total-variance machinery exists to avoid.
    """
    if forward <= 0:
        raise ValueError("the forward must be positive")
    if maturity <= 0:
        raise ValueError("a density needs a positive maturity")

    grid = np.asarray(strikes, dtype=float)
    if grid.size < 3:
        raise ValueError("a second difference needs at least three strikes")
    if np.any(np.diff(grid) <= 0):
        raise ValueError("strikes must be strictly increasing")

    bump = relative_bump * grid

    def call(at: np.ndarray) -> np.ndarray:
        sigma = np.asarray(implied_volatility(at), dtype=float)
        return black76_price(forward, at, maturity, sigma, True, discount_factor)

    second_difference = (call(grid + bump) - 2.0 * call(grid) + call(grid - bump)) / (bump * bump)
    density = second_difference / discount_factor

    widths = np.diff(grid)
    mass = float(np.sum(0.5 * (density[1:] + density[:-1]) * widths))
    mean = float(np.sum(0.5 * (density[1:] * grid[1:] + density[:-1] * grid[:-1]) * widths))
    negative = float(
        -np.sum(0.5 * (np.minimum(density[1:], 0.0) + np.minimum(density[:-1], 0.0)) * widths)
    )

    flags: list[str] = []
    if np.any(density < NEGATIVE_TOLERANCE):
        flags.append(DensityFlag.NEGATIVE_REGION)
    if abs(mass - 1.0) > 0.02:
        flags.append(DensityFlag.POOR_NORMALISATION)
    if mass > 0 and abs(mean / mass - forward) / forward > 0.02:
        flags.append(DensityFlag.MEAN_AWAY_FROM_FORWARD)
    if grid[-1] / grid[0] < 2.0:
        flags.append(DensityFlag.NARROW_STRIKE_RANGE)

    return RiskNeutralDensity(
        strikes=grid,
        density=density,
        forward=forward,
        maturity=maturity,
        discount_factor=discount_factor,
        total_mass=mass,
        implied_mean=mean / mass if mass > 0 else float("nan"),
        negative_mass=negative,
        flags=tuple(flags),
    )
