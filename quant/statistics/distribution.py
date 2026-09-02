"""Distribution summaries for historical analytics.

Deliberately small and explicit. The percentile machinery exists so the platform
can say "today's at-the-money level is at the 82nd percentile of the last 60
observations" — and the observation count travels with the answer, because a
percentile computed from eight surfaces is a different kind of statement from
one computed from six hundred, and presenting them identically would be the
dishonest part.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: A sample is treated as having no spread when its standard deviation is this
#: small relative to its level. A constant sample does not compute to exactly
#: zero variance in float64 -- three copies of 0.1 give a standard deviation of
#: about 3e-17 -- and dividing by that produces a confident-looking score from
#: no information at all.
DEGENERATE_STD_RELATIVE = 1e-9
DEGENERATE_STD_ABSOLUTE = 1e-15

#: Below this many observations a percentile is reported but marked unreliable.
#: Chosen so that a single observation cannot move a rank by more than about
#: five points; it is a stated convention, not a statistical theorem.
MIN_RELIABLE_OBSERVATIONS = 20


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    """What a sample of historical values supports saying."""

    count: int
    mean: float | None
    std: float | None
    minimum: float | None
    maximum: float | None
    median: float | None
    p10: float | None
    p90: float | None

    @property
    def is_reliable(self) -> bool:
        return self.count >= MIN_RELIABLE_OBSERVATIONS

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "mean": self.mean,
            "std": self.std,
            "min": self.minimum,
            "max": self.maximum,
            "median": self.median,
            "p10": self.p10,
            "p90": self.p90,
            "is_reliable": self.is_reliable,
        }


def summarise(sample: Sequence[float]) -> DistributionSummary:
    values = np.asarray([v for v in sample if v is not None and np.isfinite(v)], dtype=float)
    if values.size == 0:
        return DistributionSummary(0, None, None, None, None, None, None, None)
    return DistributionSummary(
        count=int(values.size),
        mean=float(np.mean(values)),
        std=float(np.std(values, ddof=1)) if values.size > 1 else 0.0,
        minimum=float(np.min(values)),
        maximum=float(np.max(values)),
        median=float(np.median(values)),
        p10=float(np.percentile(values, 10)),
        p90=float(np.percentile(values, 90)),
    )


def percentile_rank(value: float, sample: Sequence[float]) -> float | None:
    """Fraction of the sample at or below ``value``, in [0, 1].

    The *inclusive* definition (``<=``), so a value equal to every observation
    ranks at 1.0 rather than 0.0. Returns ``None`` for an empty sample rather
    than a misleading 0.5.
    """
    values = np.asarray([v for v in sample if v is not None and np.isfinite(v)], dtype=float)
    if values.size == 0 or not np.isfinite(value):
        return None
    return float(np.count_nonzero(values <= value) / values.size)


def z_score(value: float, sample: Sequence[float]) -> float | None:
    """Standard scores against a sample. ``None`` when it cannot be computed.

    Requires at least two observations and a genuinely non-degenerate spread. A
    sample with no variation would otherwise produce a large score from rounding
    noise, which reads as enormous significance when it actually means "no
    history to speak of".
    """
    summary = summarise(sample)
    if summary.count < 2 or summary.std is None:
        return None
    floor = max(DEGENERATE_STD_ABSOLUTE, DEGENERATE_STD_RELATIVE * abs(summary.mean or 0.0))
    if summary.std <= floor:
        return None
    return float((value - summary.mean) / summary.std)
