"""Greeks container with explicit units.

An unlabelled vega is a bug report waiting to happen: 0.42 could be per 1.00 of
volatility or per volatility point, and the two differ by a factor of 100. So
the raw partial derivatives and the display-scaled values are separate,
explicitly named fields, and nothing in the platform prints a bare number.

Scaling conventions (docs/methodology.md section 2.3):

============  ==============================================
Vega          currency change per **+1 volatility point** (+0.01)
Theta         currency change per **calendar day**
Rho           currency change per **+1 basis point**
============  ==============================================

These are *per unit contract*. Position-level Greeks multiply by signed
quantity and the contract multiplier; that scaling belongs to the portfolio
domain, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

VOL_POINT = 0.01
BASIS_POINT = 0.0001
DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, slots=True)
class Greeks:
    """Per-unit-contract sensitivities. Fields may be scalars or arrays."""

    price: np.ndarray
    #: dV/dS, per 1 unit of the underlying.
    delta: np.ndarray
    #: d2V/dS2.
    gamma: np.ndarray
    #: dV/dsigma, per 1.00 of volatility (i.e. per 100 vol points).
    vega: np.ndarray
    #: dV/dt, per year. Negative for a long option, other things equal.
    theta_per_year: np.ndarray
    #: dV/dr, per 1.00 of rate.
    rho: np.ndarray

    @property
    def vega_per_vol_point(self) -> np.ndarray:
        return self.vega * VOL_POINT

    @property
    def theta_per_day(self) -> np.ndarray:
        return self.theta_per_year / DAYS_PER_YEAR

    @property
    def rho_per_bp(self) -> np.ndarray:
        return self.rho * BASIS_POINT

    def to_dict(self) -> dict[str, float]:
        """Display-scaled, scalar. Keys name their units."""
        return {
            "price": float(self.price),
            "delta": float(self.delta),
            "gamma": float(self.gamma),
            "vega_per_vol_point": float(self.vega_per_vol_point),
            "theta_per_day": float(self.theta_per_day),
            "rho_per_bp": float(self.rho_per_bp),
            "vega_raw_per_unit_vol": float(self.vega),
            "theta_raw_per_year": float(self.theta_per_year),
            "rho_raw_per_unit_rate": float(self.rho),
        }


#: Human-readable units, surfaced by the API so a client never has to guess.
GREEK_UNITS: dict[str, str] = {
    "delta": "currency change per +1 unit of the underlying",
    "gamma": "delta change per +1 unit of the underlying",
    "vega_per_vol_point": "currency change per +1 volatility point (+0.01)",
    "theta_per_day": "currency change per calendar day",
    "rho_per_bp": "currency change per +1 basis point",
}
