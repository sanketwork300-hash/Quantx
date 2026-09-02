"""Black-Scholes-Merton: European options on a spot with continuous carry.

This is the platform's canonical **Greeks** engine. Black-76 is the canonical
*pricing* parameterization (listed markets quote against a forward), but Greeks
are naturally expressed in spot terms: a forward-space theta is ambiguous
because it depends on whether the forward and the discount factor are held
fixed as time passes, and an ambiguous theta is worse than none.

    d1 = (ln(S/K) + (r - q + sigma^2/2) T) / (sigma sqrt(T))
    d2 = d1 - sigma sqrt(T)
    C  = S e^{-qT} N(d1) - K e^{-rT} N(d2)
    P  = K e^{-rT} N(-d2) - S e^{-qT} N(-d1)

References: Black & Scholes (1973); Merton (1973). Validated against ``vollib``
and QuantLib, and every Greek against central finite differences, in
``tests/quant_validation/``.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm

from quant.pricing.greeks import Greeks

ArrayLike = float | np.ndarray

#: Below this the option has no optionality left and the limiting (intrinsic)
#: values are used instead of a formula that would divide by zero.
DEGENERATE = 1e-14


def _broadcast(*arrays: ArrayLike) -> tuple[np.ndarray, ...]:
    return np.broadcast_arrays(*[np.asarray(a, dtype=float) for a in arrays])


def d1_d2(
    spot: ArrayLike,
    strike: ArrayLike,
    tau: ArrayLike,
    rate: ArrayLike,
    dividend: ArrayLike,
    sigma: ArrayLike,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(d1, d2, sigma*sqrt(tau))``."""
    spot, strike, tau, rate, dividend, sigma = _broadcast(spot, strike, tau, rate, dividend, sigma)
    with np.errstate(divide="ignore", invalid="ignore"):
        std = sigma * np.sqrt(tau)
        d1 = (np.log(spot / strike) + (rate - dividend + 0.5 * sigma**2) * tau) / std
        d2 = d1 - std
    return d1, d2, std


def bsm_price(
    spot: ArrayLike,
    strike: ArrayLike,
    tau: ArrayLike,
    rate: ArrayLike,
    dividend: ArrayLike,
    sigma: ArrayLike,
    is_call: bool | np.ndarray = True,
) -> np.ndarray:
    spot, strike, tau, rate, dividend, sigma = _broadcast(spot, strike, tau, rate, dividend, sigma)
    call_mask = np.broadcast_to(np.asarray(is_call, dtype=bool), spot.shape)
    d1, d2, std = d1_d2(spot, strike, tau, rate, dividend, sigma)

    carry = spot * np.exp(-dividend * tau)
    discounted_strike = strike * np.exp(-rate * tau)
    call = carry * norm.cdf(d1) - discounted_strike * norm.cdf(d2)
    put = discounted_strike * norm.cdf(-d2) - carry * norm.cdf(-d1)
    price = np.where(call_mask, call, put)

    degenerate = std <= DEGENERATE
    if np.any(degenerate):
        intrinsic = np.where(
            call_mask,
            np.maximum(carry - discounted_strike, 0.0),
            np.maximum(discounted_strike - carry, 0.0),
        )
        price = np.where(degenerate, intrinsic, price)
    return np.asarray(price, dtype=float)


def bsm_greeks(
    spot: ArrayLike,
    strike: ArrayLike,
    tau: ArrayLike,
    rate: ArrayLike,
    dividend: ArrayLike,
    sigma: ArrayLike,
    is_call: bool | np.ndarray = True,
) -> Greeks:
    """Analytic Greeks. Degenerate inputs collapse to their limits, not to nan."""
    spot, strike, tau, rate, dividend, sigma = _broadcast(spot, strike, tau, rate, dividend, sigma)
    call_mask = np.broadcast_to(np.asarray(is_call, dtype=bool), spot.shape)
    d1, d2, std = d1_d2(spot, strike, tau, rate, dividend, sigma)

    carry_factor = np.exp(-dividend * tau)
    discount = np.exp(-rate * tau)
    pdf_d1 = norm.pdf(d1)

    with np.errstate(divide="ignore", invalid="ignore"):
        delta = np.where(
            call_mask,
            carry_factor * norm.cdf(d1),
            carry_factor * (norm.cdf(d1) - 1.0),
        )
        gamma = carry_factor * pdf_d1 / (spot * std)
        vega = spot * carry_factor * pdf_d1 * np.sqrt(tau)

        time_decay = -spot * carry_factor * pdf_d1 * sigma / (2.0 * np.sqrt(tau))
        theta_call = (
            time_decay
            - rate * strike * discount * norm.cdf(d2)
            + dividend * spot * carry_factor * norm.cdf(d1)
        )
        theta_put = (
            time_decay
            + rate * strike * discount * norm.cdf(-d2)
            - dividend * spot * carry_factor * norm.cdf(-d1)
        )
        theta = np.where(call_mask, theta_call, theta_put)

        rho = np.where(
            call_mask,
            strike * tau * discount * norm.cdf(d2),
            -strike * tau * discount * norm.cdf(-d2),
        )

    price = bsm_price(spot, strike, tau, rate, dividend, sigma, call_mask)

    degenerate = std <= DEGENERATE
    if np.any(degenerate):
        # At expiry (or zero vol) the option is a forward contract or nothing:
        # delta is an indicator, and every second-order sensitivity vanishes.
        intrinsic_itm = np.where(
            call_mask,
            spot * carry_factor > strike * discount,
            spot * carry_factor < strike * discount,
        )
        limit_delta = np.where(intrinsic_itm, np.where(call_mask, carry_factor, -carry_factor), 0.0)
        zeros = np.zeros_like(price)
        delta = np.where(degenerate, limit_delta, delta)
        gamma = np.where(degenerate, zeros, gamma)
        vega = np.where(degenerate, zeros, vega)
        theta = np.where(degenerate, zeros, theta)
        rho = np.where(
            degenerate,
            np.where(intrinsic_itm, np.where(call_mask, 1.0, -1.0), 0.0) * strike * tau * discount,
            rho,
        )

    return Greeks(
        price=np.asarray(price, dtype=float),
        delta=np.asarray(delta, dtype=float),
        gamma=np.asarray(gamma, dtype=float),
        vega=np.asarray(vega, dtype=float),
        theta_per_year=np.asarray(theta, dtype=float),
        rho=np.asarray(rho, dtype=float),
    )


def forward_price(
    spot: ArrayLike, tau: ArrayLike, rate: ArrayLike, dividend: ArrayLike
) -> np.ndarray:
    """``F = S exp((r - q) tau)``, the cost-of-carry forward."""
    spot, tau, rate, dividend = _broadcast(spot, tau, rate, dividend)
    return np.asarray(spot * np.exp((rate - dividend) * tau), dtype=float)
