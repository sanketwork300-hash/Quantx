"""Second- and third-order Greeks: vanna, volga and charm.

These live apart from `bsm_greeks` rather than swelling it, because they answer
a different question. Delta, gamma, vega, theta and rho say how a price moves;
these say how a *hedge* decays — vanna is how delta drifts when volatility
moves, volga is how vega drifts when volatility moves, and charm is how delta
drifts as time passes with nothing else changing.

Each is validated against a central finite difference of the platform's own
price and first-order Greeks, which catches the error a closed form shares with
nothing else: a sign or a factor that is internally consistent and wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

from quant.pricing.black_scholes import DEGENERATE, d1_d2
from quant.pricing.greeks import BASIS_POINT, DAYS_PER_YEAR, VOL_POINT

ArrayLike = float | np.ndarray


HIGHER_ORDER_UNITS: dict[str, str] = {
    "vanna_per_vol_point": "delta change per +1 volatility point (+0.01)",
    "volga_per_vol_point": "vega change per +1 volatility point (+0.01)",
    "charm_per_day": "delta change per calendar day, with nothing else moving",
}


@dataclass(frozen=True, slots=True)
class HigherOrderGreeks:
    """Raw partials, with the scaled readings alongside them.

    Both are kept for the same reason the first-order engine keeps both: nothing
    downstream should have to back out a scaling factor to recover the partial,
    and nobody should have to remember what unit a number is in.
    """

    #: d2V/dS dsigma — how delta moves per unit of volatility.
    vanna: np.ndarray
    #: d2V/dsigma^2 — how vega moves per unit of volatility.
    volga: np.ndarray
    #: dDelta/dt — how delta moves per year as time passes.
    charm_per_year: np.ndarray

    @property
    def vanna_per_vol_point(self) -> np.ndarray:
        """Delta change per +1 volatility point (+0.01)."""
        return self.vanna * VOL_POINT

    @property
    def volga_per_vol_point(self) -> np.ndarray:
        """Vega change per +1 volatility point, in the same units as vega."""
        return self.volga * VOL_POINT * VOL_POINT

    @property
    def charm_per_day(self) -> np.ndarray:
        """Delta change per calendar day."""
        return self.charm_per_year / DAYS_PER_YEAR

    def to_dict(self) -> dict:
        return {
            "vanna": float(np.asarray(self.vanna).reshape(-1)[0]),
            "vanna_per_vol_point": float(np.asarray(self.vanna_per_vol_point).reshape(-1)[0]),
            "volga": float(np.asarray(self.volga).reshape(-1)[0]),
            "volga_per_vol_point": float(np.asarray(self.volga_per_vol_point).reshape(-1)[0]),
            "charm_per_year": float(np.asarray(self.charm_per_year).reshape(-1)[0]),
            "charm_per_day": float(np.asarray(self.charm_per_day).reshape(-1)[0]),
            # Carried in the payload rather than only in the module, the same
            # way the first-order engine carries GREEK_UNITS: a number whose
            # scaling a reader has to remember is a number they will misread.
            "units": dict(HIGHER_ORDER_UNITS),
        }


def bsm_higher_order_greeks(
    spot: ArrayLike,
    strike: ArrayLike,
    tau: ArrayLike,
    rate: ArrayLike,
    dividend: ArrayLike,
    sigma: ArrayLike,
    is_call: bool | np.ndarray = True,
) -> HigherOrderGreeks:
    """Analytic vanna, volga and charm under Black-Scholes-Merton.

        vanna = -e^{-q tau} phi(d1) d2 / sigma
        volga = vega * d1 * d2 / sigma
        charm = q e^{-q tau} N(d1)
                - e^{-q tau} phi(d1) (2(r-q)tau - d2 sigma sqrt(tau))
                  / (2 tau sigma sqrt(tau))         [call; put differs by the
                                                     sign of the first term]

    Vanna and volga are identical for a call and a put — both are second
    derivatives of a price pair that differs by a term linear in spot and
    constant in volatility, so the difference vanishes. Charm is not, because
    put-call parity's spot term carries the dividend.
    """
    arrays = np.broadcast_arrays(
        *[np.asarray(value, dtype=float) for value in (spot, strike, tau, rate, dividend, sigma)]
    )
    spot, strike, tau, rate, dividend, sigma = arrays
    call_mask = np.broadcast_to(np.asarray(is_call, dtype=bool), spot.shape)

    d1, d2, std = d1_d2(spot, strike, tau, rate, dividend, sigma)
    carry = np.exp(-dividend * tau)
    pdf_d1 = norm.pdf(d1)
    root_tau = np.sqrt(tau)

    with np.errstate(divide="ignore", invalid="ignore"):
        vanna = -carry * pdf_d1 * d2 / sigma
        vega = spot * carry * pdf_d1 * root_tau
        volga = vega * d1 * d2 / sigma

        common = carry * pdf_d1 * (2.0 * (rate - dividend) * tau - d2 * std) / (2.0 * tau * std)
        charm_call = dividend * carry * norm.cdf(d1) - common
        charm_put = -dividend * carry * norm.cdf(-d1) - common
        charm = np.where(call_mask, charm_call, charm_put)

    degenerate = std <= DEGENERATE
    if np.any(degenerate):
        # With no optionality left there is no curvature to report, and delta is
        # a step function whose time derivative is zero away from the strike.
        zeros = np.zeros_like(spot)
        vanna = np.where(degenerate, zeros, vanna)
        volga = np.where(degenerate, zeros, volga)
        charm = np.where(degenerate, zeros, charm)

    return HigherOrderGreeks(
        vanna=np.asarray(vanna, dtype=float),
        volga=np.asarray(volga, dtype=float),
        charm_per_year=np.asarray(charm, dtype=float),
    )


def scaled_rho_per_basis_point(rho: np.ndarray) -> np.ndarray:
    return np.asarray(rho, dtype=float) * BASIS_POINT
