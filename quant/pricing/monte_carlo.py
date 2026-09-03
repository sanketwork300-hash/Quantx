"""Seeded Monte Carlo for European vanillas, with its own error bar.

Included in the consensus not because it prices a vanilla better than the closed
form — it cannot — but because it prices one *differently*. A consensus of
methods that share an implementation shares its mistakes; an independent path
simulation agreeing with an analytic formula is evidence, and a Monte Carlo that
drifts away from it is the cheapest possible detector of a broken input.

Every run reports its **standard error**, and the price is meaningless without
it: a Monte Carlo price quoted alone is a number pretending to a precision it
does not have.

Two variance reductions, both exact rather than approximate. Antithetic
sampling uses every draw twice, once negated, which removes the odd part of the
sampling error. The control variate is the discounted terminal price itself,
whose expectation under the pricing measure is known exactly to be the forward —
so the correction is unbiased by construction, not by assumption.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


class MonteCarloError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    price: float
    standard_error: float
    paths: int
    seed: int
    antithetic: bool
    control_variate: bool
    #: How much of the raw variance the control variate removed. Zero means it
    #: did nothing, which for a deep out-of-the-money option it nearly does.
    variance_reduction: float

    @property
    def confidence_interval(self) -> tuple[float, float]:
        """A 95% interval. Two standard errors, stated as such."""
        return (self.price - 1.96 * self.standard_error, self.price + 1.96 * self.standard_error)

    def to_dict(self) -> dict:
        low, high = self.confidence_interval
        return {
            "price": self.price,
            "standard_error": self.standard_error,
            "confidence_interval_95": [low, high],
            "paths": self.paths,
            "seed": self.seed,
            "antithetic": self.antithetic,
            "control_variate": self.control_variate,
            "variance_reduction": self.variance_reduction,
            "caveat": (
                "A simulated price is meaningless without its standard error. "
                "The same seed and path count reproduce this number exactly."
            ),
        }


def monte_carlo_price(
    spot: float,
    strike: float,
    tau: float,
    rate: float,
    dividend: float,
    sigma: float,
    is_call: bool = True,
    paths: int = 100_000,
    seed: int = 20_260_924,
    antithetic: bool = True,
    control_variate: bool = True,
) -> MonteCarloResult:
    """European vanilla under geometric Brownian motion."""
    if spot <= 0 or strike <= 0:
        raise MonteCarloError("spot and strike must be positive")
    if tau <= 0:
        raise MonteCarloError("Monte Carlo needs a positive time to expiry")
    if sigma <= 0:
        raise MonteCarloError("Monte Carlo needs a positive volatility")
    if paths < 2:
        raise MonteCarloError("a Monte Carlo estimate needs at least two paths")

    rng = np.random.default_rng(seed)
    if antithetic:
        half = (paths + 1) // 2
        base = rng.standard_normal(half)
        normals = np.concatenate([base, -base])
    else:
        normals = rng.standard_normal(paths)

    drift = (rate - dividend - 0.5 * sigma * sigma) * tau
    diffusion = sigma * math.sqrt(tau)
    terminal = spot * np.exp(drift + diffusion * normals)

    discount = math.exp(-rate * tau)
    payoff = np.maximum(terminal - strike, 0.0) if is_call else np.maximum(strike - terminal, 0.0)
    discounted = discount * payoff

    raw_variance = float(np.var(discounted, ddof=1))
    reduction = 0.0

    if control_variate:
        # E[e^{-r tau} S_T] = S e^{-q tau}, exactly, under the pricing measure.
        control = discount * terminal
        expected = spot * math.exp(-dividend * tau)
        covariance = float(np.cov(discounted, control, ddof=1)[0, 1])
        control_variance = float(np.var(control, ddof=1))
        if control_variance > 0:
            beta = covariance / control_variance
            discounted = discounted - beta * (control - expected)
            adjusted = float(np.var(discounted, ddof=1))
            reduction = max(0.0, 1.0 - adjusted / raw_variance) if raw_variance > 0 else 0.0

    count = discounted.size
    price = float(np.mean(discounted))
    standard_error = float(np.std(discounted, ddof=1) / math.sqrt(count))

    return MonteCarloResult(
        price=price,
        standard_error=standard_error,
        paths=count,
        seed=seed,
        antithetic=antithetic,
        control_variate=control_variate,
        variance_reduction=reduction,
    )
