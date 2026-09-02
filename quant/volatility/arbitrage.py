"""Static no-arbitrage conditions on option prices and total-variance slices.

Pure array mathematics. Every check returns the **signed magnitude** of each
violation in interpretable units, never a boolean: on a discrete strike grid
with wide markets, tiny convexity violations are ubiquitous and unexploitable,
while a large one on liquid strikes is a real data problem. A boolean would
erase that distinction, and the severity policy that consumes these magnitudes
lives in the domain layer where the bid/ask context is known.

References: standard static no-arbitrage bounds; Gatheral & Jacquier (2014) for
the total-variance forms.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class ConditionViolation:
    """One violated condition.

    ``magnitude`` is how far the condition is breached, in the natural units of
    that condition (currency for price conditions, total variance for calendar).
    ``indices`` point back into the input arrays so the caller can name the
    quotes involved.
    """

    indices: tuple[int, ...]
    magnitude: float
    detail: dict


def check_price_bounds(
    strikes: np.ndarray,
    prices: np.ndarray,
    forward: float,
    discount: float,
    is_call: bool,
) -> list[ConditionViolation]:
    """``max(DF(F-K), 0) <= C <= DF*F`` and the corresponding put bounds.

    Expressed on the forward, so no separate dividend assumption enters: the
    forward already carries the carry.
    """
    strikes = np.asarray(strikes, dtype=float)
    prices = np.asarray(prices, dtype=float)

    if is_call:
        lower = np.maximum(discount * (forward - strikes), 0.0)
        upper = np.full_like(strikes, discount * forward)
    else:
        lower = np.maximum(discount * (strikes - forward), 0.0)
        upper = discount * strikes

    violations: list[ConditionViolation] = []
    for index in range(strikes.size):
        price = prices[index]
        if not np.isfinite(price):
            continue
        if price < lower[index]:
            violations.append(
                ConditionViolation(
                    (index,),
                    float(lower[index] - price),
                    {
                        "bound": "LOWER",
                        "limit": float(lower[index]),
                        "price": float(price),
                        "strike": float(strikes[index]),
                    },
                )
            )
        elif price > upper[index]:
            violations.append(
                ConditionViolation(
                    (index,),
                    float(price - upper[index]),
                    {
                        "bound": "UPPER",
                        "limit": float(upper[index]),
                        "price": float(price),
                        "strike": float(strikes[index]),
                    },
                )
            )
    return violations


def check_put_call_parity(
    strikes: np.ndarray,
    call_prices: np.ndarray,
    put_prices: np.ndarray,
    forward: float,
    discount: float,
) -> list[ConditionViolation]:
    """``C - P = DF (F - K)``, per strike."""
    strikes = np.asarray(strikes, dtype=float)
    calls = np.asarray(call_prices, dtype=float)
    puts = np.asarray(put_prices, dtype=float)

    residual = (calls - puts) - discount * (forward - strikes)
    violations: list[ConditionViolation] = []
    for index in range(strikes.size):
        if not np.isfinite(residual[index]):
            continue
        violations.append(
            ConditionViolation(
                (index,),
                float(abs(residual[index])),
                {
                    "strike": float(strikes[index]),
                    "residual": float(residual[index]),
                    "call": float(calls[index]),
                    "put": float(puts[index]),
                },
            )
        )
    return violations


def check_vertical_spreads(
    strikes: np.ndarray, prices: np.ndarray, discount: float, is_call: bool
) -> list[ConditionViolation]:
    """Monotonicity and the slope bound on adjacent strikes.

    A call must not gain value as the strike rises, and may not lose more than
    the discounted strike difference. Both halves matter: the first catches a
    crossed pair, the second catches a price that has fallen too fast to be
    consistent with any density.

    ``strikes`` must be sorted ascending.
    """
    strikes = np.asarray(strikes, dtype=float)
    prices = np.asarray(prices, dtype=float)

    violations: list[ConditionViolation] = []
    for index in range(strikes.size - 1):
        low, high = prices[index], prices[index + 1]
        if not (np.isfinite(low) and np.isfinite(high)):
            continue
        gap = discount * float(strikes[index + 1] - strikes[index])

        if is_call:
            monotone_breach = high - low  # calls must fall with strike
            slope_breach = (low - high) - gap  # and not fall faster than DF*dK
        else:
            monotone_breach = low - high  # puts must rise with strike
            slope_breach = (high - low) - gap

        if monotone_breach > 0:
            violations.append(
                ConditionViolation(
                    (index, index + 1),
                    float(monotone_breach),
                    {
                        "condition": "MONOTONICITY",
                        "strikes": [float(strikes[index]), float(strikes[index + 1])],
                        "prices": [float(low), float(high)],
                    },
                )
            )
        elif slope_breach > 0:
            violations.append(
                ConditionViolation(
                    (index, index + 1),
                    float(slope_breach),
                    {
                        "condition": "SLOPE_BOUND",
                        "strikes": [float(strikes[index]), float(strikes[index + 1])],
                        "max_difference": gap,
                    },
                )
            )
    return violations


def check_butterfly(strikes: np.ndarray, prices: np.ndarray) -> list[ConditionViolation]:
    """Convexity in strike, on adjacent triples.

    The general unequally-spaced form, normalised to price units:

        [(K3-K2) C1 - (K3-K1) C2 + (K2-K1) C3] / (K3-K1) >= 0

    which reduces to ``(C1 - 2 C2 + C3) / 2`` on an evenly spaced grid. A
    negative value is a negative butterfly price, and therefore a negative
    implied density.
    """
    strikes = np.asarray(strikes, dtype=float)
    prices = np.asarray(prices, dtype=float)

    violations: list[ConditionViolation] = []
    for index in range(1, strikes.size - 1):
        k1, k2, k3 = strikes[index - 1], strikes[index], strikes[index + 1]
        c1, c2, c3 = prices[index - 1], prices[index], prices[index + 1]
        if not all(np.isfinite(value) for value in (c1, c2, c3)):
            continue
        span = k3 - k1
        if span <= 0:
            continue
        value = ((k3 - k2) * c1 - span * c2 + (k2 - k1) * c3) / span
        if value < 0:
            violations.append(
                ConditionViolation(
                    (index - 1, index, index + 1),
                    float(-value),
                    {
                        "strikes": [float(k1), float(k2), float(k3)],
                        "butterfly_value": float(value),
                    },
                )
            )
    return violations


def check_calendar(
    log_moneyness: np.ndarray,
    total_variance_short: np.ndarray,
    total_variance_long: np.ndarray,
) -> list[ConditionViolation]:
    """Total variance must not fall as maturity rises, at fixed log-moneyness.

    Compared at fixed ``k`` rather than fixed strike: that is the coordinate the
    condition is actually stated in, and comparing at fixed strike would produce
    spurious violations whenever the forward moves between expiries.
    """
    k = np.asarray(log_moneyness, dtype=float)
    short = np.asarray(total_variance_short, dtype=float)
    long = np.asarray(total_variance_long, dtype=float)

    violations: list[ConditionViolation] = []
    for index in range(k.size):
        if not (np.isfinite(short[index]) and np.isfinite(long[index])):
            continue
        breach = short[index] - long[index]
        if breach > 0:
            violations.append(
                ConditionViolation(
                    (index,),
                    float(breach),
                    {
                        "log_moneyness": float(k[index]),
                        "total_variance_short": float(short[index]),
                        "total_variance_long": float(long[index]),
                    },
                )
            )
    return violations
