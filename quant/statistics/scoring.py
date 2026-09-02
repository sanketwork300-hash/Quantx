"""Bounded score transforms.

Quality, liquidity and confidence scoring all need to map an unbounded
measurement (an age in seconds, a spread ratio, a volume) into ``[0, 1]``. The
transforms live here, as pure functions with stated shapes, rather than as
inline arithmetic scattered through the domain layer, so that they can be
unit-tested and so that the *shape* of each penalty is a documented decision.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from quant.numerical.tolerances import clamp


def exponential_decay_score(value: float, half_life: float) -> float:
    """``0.5 ** (value / half_life)``: 1.0 at zero, 0.5 at one half-life.

    Used for staleness. Chosen over a linear ramp because there is no natural
    age at which a quote becomes exactly worthless, and because the damage from
    staleness is multiplicative: twice as old is roughly half as useful.
    """
    if half_life <= 0:
        raise ValueError(f"half_life must be positive, got {half_life}")
    if value <= 0:
        return 1.0
    return float(0.5 ** (value / half_life))


def ratio_penalty_score(value: float, reference: float, exponent: float = 2.0) -> float:
    """``1 / (1 + (value / reference) ** exponent)``.

    Used for spreads. Quadratic by default so that a spread near the reference
    is barely penalised while a spread several times the reference collapses
    quickly, which matches how usable a quote actually is.
    """
    if reference <= 0:
        raise ValueError(f"reference must be positive, got {reference}")
    if value <= 0:
        return 1.0
    return float(1.0 / (1.0 + (value / reference) ** exponent))


def saturating_score(value: float, reference: float) -> float:
    """``x / (1 + x)`` with ``x = value / reference``; 0.5 at the reference.

    Used for liquidity. Saturating rather than linear because the difference
    between 10x and 100x the reference volume does not change whether a quote
    can be trusted, but the difference between 0.1x and 1x does.
    """
    if reference <= 0:
        raise ValueError(f"reference must be positive, got {reference}")
    if value <= 0:
        return 0.0
    x = value / reference
    return float(x / (1.0 + x))


def geometric_mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("geometric_mean of an empty sequence")
    return weighted_geometric_mean(values, [1.0] * len(values))


def weighted_geometric_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted geometric mean, with an exact zero result if any value is zero.

    This is the aggregation used for the overall data-quality score. A single
    catastrophic dimension (a crossed market scores 0 on consistency) must drive
    the overall score to zero; an arithmetic mean would let it be averaged away
    by four healthy dimensions, which is exactly the failure the quality engine
    exists to prevent.
    """
    if len(values) != len(weights):
        raise ValueError(f"length mismatch: {len(values)} values, {len(weights)} weights")
    if not values:
        raise ValueError("weighted_geometric_mean of an empty sequence")

    total_weight = 0.0
    log_sum = 0.0
    for value, weight in zip(values, weights, strict=True):
        if weight < 0:
            raise ValueError(f"weights must be non-negative, got {weight}")
        if weight == 0:
            continue
        if value < 0:
            raise ValueError(f"scores must be non-negative, got {value}")
        if value == 0:
            return 0.0
        log_sum += weight * math.log(value)
        total_weight += weight

    if total_weight == 0:
        raise ValueError("weights sum to zero")
    return clamp(math.exp(log_sum / total_weight), 0.0, 1.0)
