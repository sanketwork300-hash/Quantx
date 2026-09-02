"""Forward estimation.

Three estimators, reported side by side. Disagreement between them is
information — it usually means bad data or an unstated carry — so they are never
averaged into one number that hides it.

Every estimate carries method, confidence, observation count and residual error,
because a forward from twelve liquid put/call pairs and a forward from a guessed
dividend yield are not the same kind of number, and everything downstream (the
whole smile sits on ``k = ln(K/F)``) inherits the difference.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from quant.statistics.scoring import saturating_score, weighted_geometric_mean

#: A put-call-parity regression needs at least this many usable strike pairs
#: before its slope means anything.
MIN_PARITY_PAIRS = 3
#: Pairs at which the observation-count factor reaches 0.5.
PARITY_REFERENCE_PAIRS = 6.0
#: A recovered discount factor outside this range is a degenerate regression,
#: not a discount factor.
MIN_DISCOUNT_FACTOR = 0.5
MAX_DISCOUNT_FACTOR = 1.02


class ForwardMethod(StrEnum):
    SPOT_CARRY = "SPOT_CARRY"
    FUTURE = "FUTURE"
    PUT_CALL_PARITY = "PUT_CALL_PARITY"


class ForwardFailure(StrEnum):
    NO_SPOT = "NO_SPOT"
    NO_FUTURE = "NO_FUTURE"
    INSUFFICIENT_PAIRS = "INSUFFICIENT_PAIRS"
    DEGENERATE_REGRESSION = "DEGENERATE_REGRESSION"
    NON_POSITIVE_TIME = "NON_POSITIVE_TIME"


@dataclass(frozen=True, slots=True)
class ForwardEstimate:
    value: float | None
    method: ForwardMethod
    confidence: float
    observations: int
    #: Root mean squared regression residual, in price units. ``None`` where the
    #: method has no residual to report (a forward read off a future does not).
    residual_error: float | None = None
    #: Recovered by the parity regression; supplied by the curve otherwise.
    discount_factor: float | None = None
    error: ForwardFailure | None = None
    assumptions: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.value is not None and self.value > 0

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "method": str(self.method),
            "confidence": self.confidence,
            "observations": self.observations,
            "residual_error": self.residual_error,
            "discount_factor": self.discount_factor,
            "error": str(self.error) if self.error else None,
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class ForwardEstimateSet:
    """Every estimate that could be made for one expiry, plus the chosen one."""

    estimates: tuple[ForwardEstimate, ...]
    selected: ForwardEstimate | None

    @property
    def disagreement(self) -> float | None:
        """Relative spread between the usable estimates.

        Surfaced rather than smoothed: two estimators 3% apart mean one of the
        inputs is wrong, and that is worth more than their average.
        """
        values = [e.value for e in self.estimates if e.ok]
        if len(values) < 2:
            return None
        return (max(values) - min(values)) / min(values)

    def to_dict(self) -> dict:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "estimates": [estimate.to_dict() for estimate in self.estimates],
            "disagreement": self.disagreement,
        }


class ForwardEstimator:
    """Stateless. Each method returns an estimate; the caller decides."""

    @staticmethod
    def from_spot_carry(
        spot: float,
        tau: float,
        rate: float,
        dividend: float,
        dividend_assumed: bool = True,
    ) -> ForwardEstimate:
        """``F = S exp((r - q) tau)``.

        Confidence is capped when the dividend or borrow yield is an assumption
        rather than an observation, which for a retail data set it almost always
        is.
        """
        if tau <= 0:
            return ForwardEstimate(
                None, ForwardMethod.SPOT_CARRY, 0.0, 0, error=ForwardFailure.NON_POSITIVE_TIME
            )
        if spot is None or spot <= 0:
            return ForwardEstimate(
                None, ForwardMethod.SPOT_CARRY, 0.0, 0, error=ForwardFailure.NO_SPOT
            )

        assumptions = [f"risk_free_rate={rate:.6f}", f"dividend_yield={dividend:.6f}"]
        if dividend_assumed:
            assumptions.append("dividend_yield_assumed")
        return ForwardEstimate(
            value=float(spot * math.exp((rate - dividend) * tau)),
            method=ForwardMethod.SPOT_CARRY,
            confidence=0.55 if dividend_assumed else 0.80,
            observations=1,
            discount_factor=float(math.exp(-rate * tau)),
            assumptions=tuple(assumptions),
        )

    @staticmethod
    def from_future(
        future_price: float | None,
        future_tau: float | None,
        target_tau: float,
        discount_factor: float | None = None,
    ) -> ForwardEstimate:
        """Read the forward off a listed future.

        Only an exact maturity match is treated as reliable. A future of a
        different expiry implies a basis that would have to be modelled, and an
        unmodelled basis is a silent error in every strike's log-moneyness.
        """
        if future_price is None or future_price <= 0 or future_tau is None:
            return ForwardEstimate(
                None, ForwardMethod.FUTURE, 0.0, 0, error=ForwardFailure.NO_FUTURE
            )
        if target_tau <= 0:
            return ForwardEstimate(
                None, ForwardMethod.FUTURE, 0.0, 0, error=ForwardFailure.NON_POSITIVE_TIME
            )

        matched = abs(future_tau - target_tau) <= 1.0 / 365.0
        if not matched:
            return ForwardEstimate(
                None,
                ForwardMethod.FUTURE,
                0.0,
                0,
                error=ForwardFailure.NO_FUTURE,
                assumptions=("no future of matching expiry; basis not modelled",),
            )
        return ForwardEstimate(
            value=float(future_price),
            method=ForwardMethod.FUTURE,
            confidence=0.95,
            observations=1,
            discount_factor=discount_factor,
        )

    @staticmethod
    def from_put_call_parity(
        strikes: Sequence[float],
        call_prices: Sequence[float],
        put_prices: Sequence[float],
        weights: Sequence[float] | None = None,
        price_scale: float | None = None,
    ) -> ForwardEstimate:
        """Recover ``F`` and ``DF`` from option prices alone.

        ``C - P = DF (F - K)``, so regressing ``C - P`` on ``K`` gives slope
        ``-DF`` and intercept ``DF * F``. Both the discount factor and the
        forward come out of the option market itself, with no rate or dividend
        assumption at all — which is why this is the preferred estimator
        whenever there are enough liquid pairs.

        ``price_scale`` (a typical half-spread) sets the scale against which
        residuals are judged: a fit that lands inside the bid/ask noise is a
        good fit, and one that does not is telling you something.
        """
        k = np.asarray(strikes, dtype=float)
        calls = np.asarray(call_prices, dtype=float)
        puts = np.asarray(put_prices, dtype=float)

        usable = np.isfinite(k) & np.isfinite(calls) & np.isfinite(puts) & (k > 0)
        k, calls, puts = k[usable], calls[usable], puts[usable]
        w = np.ones_like(k) if weights is None else np.asarray(weights, dtype=float)[usable]
        w = np.where(np.isfinite(w) & (w > 0), w, 0.0)

        pairs = int(np.count_nonzero(w > 0))
        if pairs < MIN_PARITY_PAIRS:
            return ForwardEstimate(
                None,
                ForwardMethod.PUT_CALL_PARITY,
                0.0,
                pairs,
                error=ForwardFailure.INSUFFICIENT_PAIRS,
            )

        y = calls - puts
        sqrt_w = np.sqrt(w)
        design = np.column_stack([np.ones_like(k), k]) * sqrt_w[:, None]
        target = y * sqrt_w
        (intercept, slope), *_ = np.linalg.lstsq(design, target, rcond=None)

        discount = -float(slope)
        if not (MIN_DISCOUNT_FACTOR <= discount <= MAX_DISCOUNT_FACTOR):
            return ForwardEstimate(
                None,
                ForwardMethod.PUT_CALL_PARITY,
                0.0,
                pairs,
                error=ForwardFailure.DEGENERATE_REGRESSION,
                discount_factor=discount,
            )

        forward = float(intercept) / discount
        if not np.isfinite(forward) or forward <= 0:
            return ForwardEstimate(
                None,
                ForwardMethod.PUT_CALL_PARITY,
                0.0,
                pairs,
                error=ForwardFailure.DEGENERATE_REGRESSION,
                discount_factor=discount,
            )

        residuals = y - (intercept + slope * k)
        weight_total = float(w.sum())
        rms = float(np.sqrt(float((w * residuals**2).sum()) / weight_total))

        scale = price_scale if price_scale and price_scale > 0 else max(forward * 1e-4, 1e-8)
        count_factor = saturating_score(pairs, PARITY_REFERENCE_PAIRS)
        fit_factor = 1.0 / (1.0 + (rms / scale) ** 2)
        confidence = weighted_geometric_mean([count_factor, fit_factor], [1.0, 1.5])

        return ForwardEstimate(
            value=forward,
            method=ForwardMethod.PUT_CALL_PARITY,
            confidence=float(confidence),
            observations=pairs,
            residual_error=rms,
            discount_factor=discount,
        )

    @staticmethod
    def select(estimates: Sequence[ForwardEstimate]) -> ForwardEstimateSet:
        """Highest confidence wins; every estimate is retained and reported."""
        usable = [estimate for estimate in estimates if estimate.ok]
        selected = max(usable, key=lambda e: e.confidence) if usable else None
        return ForwardEstimateSet(estimates=tuple(estimates), selected=selected)
