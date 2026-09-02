"""Black-76: European options on a forward.

Black-76 rather than spot Black-Scholes is the platform default because listed
option markets quote against a forward that already embeds carry, dividends and
borrow. Keeping those effects inside one explicitly estimated forward, with its
own confidence, beats scattering a guessed dividend yield through every price.

    C = DF * [F N(d1) - K N(d2)]
    P = DF * [K N(-d2) - F N(-d1)]
    d1 = (ln(F/K) + 0.5 sigma^2 tau) / (sigma sqrt(tau)),   d2 = d1 - sigma sqrt(tau)
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm

ArrayLike = float | np.ndarray


def forward_d1_d2(
    forward: ArrayLike, strike: ArrayLike, tau: ArrayLike, sigma: ArrayLike
) -> tuple[np.ndarray, np.ndarray]:
    """Black-76 ``d1`` and ``d2``.

    Degenerate inputs (``tau <= 0`` or ``sigma <= 0``) yield infinities of the
    correct sign, so the pricing formula collapses to the discounted intrinsic
    value rather than producing ``nan``.
    """
    forward = np.asarray(forward, dtype=float)
    strike = np.asarray(strike, dtype=float)
    tau = np.asarray(tau, dtype=float)
    sigma = np.asarray(sigma, dtype=float)

    with np.errstate(divide="ignore", invalid="ignore"):
        variance = sigma * sigma * tau
        std = np.sqrt(variance)
        log_moneyness = np.log(forward / strike)
        d1 = (log_moneyness + 0.5 * variance) / std
        d2 = d1 - std

    degenerate = (std <= 0) | ~np.isfinite(std)
    if np.any(degenerate):
        limit = np.where(log_moneyness > 0, np.inf, np.where(log_moneyness < 0, -np.inf, 0.0))
        d1 = np.where(degenerate, limit, d1)
        d2 = np.where(degenerate, limit, d2)
    return d1, d2


def black76_price(
    forward: ArrayLike,
    strike: ArrayLike,
    tau: ArrayLike,
    sigma: ArrayLike,
    is_call: bool | np.ndarray = True,
    discount_factor: ArrayLike = 1.0,
) -> np.ndarray:
    """Undiscounted-forward Black-76 price, scaled by ``discount_factor``.

    Parameters
    ----------
    forward, strike, tau, sigma
        Broadcastable. ``tau`` in years, ``sigma`` annualised.
    is_call
        Scalar bool or a boolean array broadcastable with the rest.
    discount_factor
        ``exp(-r * tau)`` from the curve in use, or 1.0 for an undiscounted price.
    """
    forward_arr = np.asarray(forward, dtype=float)
    strike_arr = np.asarray(strike, dtype=float)
    tau_arr = np.asarray(tau, dtype=float)
    sigma_arr = np.asarray(sigma, dtype=float)
    discount = np.asarray(discount_factor, dtype=float)
    call_mask = np.asarray(is_call, dtype=bool)

    d1, d2 = forward_d1_d2(forward_arr, strike_arr, tau_arr, sigma_arr)

    call = forward_arr * norm.cdf(d1) - strike_arr * norm.cdf(d2)
    put = strike_arr * norm.cdf(-d2) - forward_arr * norm.cdf(-d1)
    undiscounted = np.where(call_mask, call, put)

    # At or past expiry the option is worth its intrinsic value exactly.
    expired = tau_arr <= 0
    if np.any(expired):
        intrinsic = np.where(
            call_mask,
            np.maximum(forward_arr - strike_arr, 0.0),
            np.maximum(strike_arr - forward_arr, 0.0),
        )
        undiscounted = np.where(expired, intrinsic, undiscounted)

    return np.asarray(discount * undiscounted, dtype=float)


def black76_vega(
    forward: ArrayLike,
    strike: ArrayLike,
    tau: ArrayLike,
    sigma: ArrayLike,
    discount_factor: ArrayLike = 1.0,
) -> np.ndarray:
    """``dV/dsigma`` per 1.00 of volatility. Identical for calls and puts.

    Used by the implied-volatility solver as the Newton derivative, so it is
    written here in forward space rather than being recovered from the spot
    Greeks.
    """
    forward_arr = np.asarray(forward, dtype=float)
    strike_arr = np.asarray(strike, dtype=float)
    tau_arr = np.asarray(tau, dtype=float)
    sigma_arr = np.asarray(sigma, dtype=float)
    discount = np.asarray(discount_factor, dtype=float)

    d1, _ = forward_d1_d2(forward_arr, strike_arr, tau_arr, sigma_arr)
    vega = discount * forward_arr * norm.pdf(d1) * np.sqrt(np.maximum(tau_arr, 0.0))
    # Beyond expiry, and at zero volatility, there is no volatility exposure.
    return np.asarray(np.where(tau_arr > 0, vega, 0.0), dtype=float)


def black76_bounds(
    forward: ArrayLike, strike: ArrayLike, is_call: bool | np.ndarray = True
) -> tuple[np.ndarray, np.ndarray]:
    """Undiscounted no-arbitrage price bounds ``(lower, upper)``.

    Call: ``max(F - K, 0) <= C/DF <= F``. Put: ``max(K - F, 0) <= P/DF <= K``.
    These need no rate assumption once the price is expressed on the forward,
    which is why the implied-volatility solver screens against them directly.
    """
    forward_arr, strike_arr = np.broadcast_arrays(
        np.asarray(forward, dtype=float), np.asarray(strike, dtype=float)
    )
    call_mask = np.broadcast_to(np.asarray(is_call, dtype=bool), forward_arr.shape)
    lower = np.where(
        call_mask,
        np.maximum(forward_arr - strike_arr, 0.0),
        np.maximum(strike_arr - forward_arr, 0.0),
    )
    upper = np.where(call_mask, forward_arr, strike_arr)
    return np.asarray(lower, dtype=float), np.asarray(upper, dtype=float)
