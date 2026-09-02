"""Raw volatility smile: the observed slice, in the coordinates that matter.

    k = ln(K / F)              log-moneyness
    w = sigma^2 * tau          total implied variance

Total variance is the working coordinate because the no-arbitrage conditions are
natural in it (butterfly is convexity of ``w`` in ``k``; calendar is
monotonicity of ``w`` in ``tau``), and because it removes the ``1/sqrt(tau)``
distortion that makes short expiries look artificially violent in volatility
space.

This holds **observations only**. Fitted values live in a separate object and a
separate table, and the two are never merged (build spec 1.2).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Half-width in log-moneyness treated as "at the money" for summary statistics.
ATM_WINDOW = 0.05


@dataclass(frozen=True, slots=True)
class RawSmile:
    """One expiry's observed smile. All arrays share a length and an order."""

    tau: float
    forward: float
    strike: np.ndarray
    log_moneyness: np.ndarray
    implied_vol: np.ndarray
    total_variance: np.ndarray
    #: Calibration weight per observation, from spread and liquidity. Phase 2
    #: consumes it; Phase 1 computes and stores it so the fit has it available.
    weight: np.ndarray
    #: Implied volatility at the bid and the ask, where both sides exist. The
    #: envelope width is how much of an apparent deviation is just the spread.
    implied_vol_bid: np.ndarray | None = None
    implied_vol_ask: np.ndarray | None = None

    def __post_init__(self) -> None:
        n = self.strike.size
        for name in ("log_moneyness", "implied_vol", "total_variance", "weight"):
            if getattr(self, name).size != n:
                raise ValueError(f"{name} has length {getattr(self, name).size}, expected {n}")
        if self.tau <= 0:
            raise ValueError(f"tau must be positive, got {self.tau}")

    def __len__(self) -> int:
        return int(self.strike.size)

    @property
    def is_empty(self) -> bool:
        return len(self) == 0

    def atm_volatility(self) -> float | None:
        """Volatility at ``k = 0``, linearly interpolated in total variance.

        Interpolating variance rather than volatility is the consistent choice:
        variance is what is additive in time, and it is the quantity the
        no-arbitrage conditions are written in.
        """
        if len(self) < 2:
            return None
        order = np.argsort(self.log_moneyness)
        k = self.log_moneyness[order]
        w = self.total_variance[order]
        if k[0] > 0.0 or k[-1] < 0.0:
            return None  # k = 0 is outside the observed range; do not extrapolate
        w_atm = float(np.interp(0.0, k, w))
        return _sqrt(w_atm / self.tau)

    def skew(self, half_width: float = ATM_WINDOW) -> float | None:
        """``d(sigma)/dk`` near the money, by least squares over a window.

        Negative for the usual equity-index shape: lower strikes trade at
        higher volatility.
        """
        return self._local_polynomial(half_width, degree=1, coefficient=1)

    def curvature(self, half_width: float = 2 * ATM_WINDOW) -> float | None:
        """``d2(sigma)/dk2`` near the money, by least squares over a window."""
        value = self._local_polynomial(half_width, degree=2, coefficient=2)
        return None if value is None else 2.0 * value

    def _local_polynomial(self, half_width: float, degree: int, coefficient: int) -> float | None:
        mask = np.abs(self.log_moneyness) <= half_width
        if int(mask.sum()) < degree + 1:
            return None
        fit = np.polynomial.polynomial.polyfit(
            self.log_moneyness[mask], self.implied_vol[mask], degree
        )
        return float(fit[coefficient])


def _sqrt(value: float) -> float:
    return float(np.sqrt(value)) if value >= 0 else float("nan")


def build_raw_smile(
    strike: np.ndarray,
    implied_vol: np.ndarray,
    forward: float,
    tau: float,
    weight: np.ndarray | None = None,
    implied_vol_bid: np.ndarray | None = None,
    implied_vol_ask: np.ndarray | None = None,
) -> RawSmile:
    """Assemble a smile from solved implied volatilities.

    Observations with no implied volatility must already have been removed by
    the caller, which knows *why* each one is missing and has to report it.
    """
    strike = np.asarray(strike, dtype=float)
    implied_vol = np.asarray(implied_vol, dtype=float)
    if forward <= 0:
        raise ValueError(f"forward must be positive, got {forward}")

    order = np.argsort(strike)
    strike = strike[order]
    implied_vol = implied_vol[order]
    log_moneyness = np.log(strike / forward)
    total_variance = implied_vol * implied_vol * tau

    weights = np.ones_like(strike) if weight is None else np.asarray(weight, dtype=float)[order]

    return RawSmile(
        tau=tau,
        forward=forward,
        strike=strike,
        log_moneyness=log_moneyness,
        implied_vol=implied_vol,
        total_variance=total_variance,
        weight=weights,
        implied_vol_bid=(
            None if implied_vol_bid is None else np.asarray(implied_vol_bid, float)[order]
        ),
        implied_vol_ask=(
            None if implied_vol_ask is None else np.asarray(implied_vol_ask, float)[order]
        ),
    )
