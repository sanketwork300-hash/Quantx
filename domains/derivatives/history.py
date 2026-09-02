"""Historical surface analytics.

Percentiles answer "is today's level unusual *for this underlying*?", which is a
different and more useful question than "is today's level high?". The machinery
is deliberately plain; what matters is that the observation count travels with
every answer, because a percentile from eight surfaces and one from six hundred
are different kinds of statement and presenting them identically is the
dishonest part.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from quant.statistics import (
    MIN_RELIABLE_OBSERVATIONS,
    DistributionSummary,
    percentile_rank,
    summarise,
    z_score,
)

HISTORY_MODEL_VERSION = "surface-history@1.0.0"

#: The characteristics a percentile is reported for.
CHARACTERISTIC_NAMES = ("atm_volatility", "skew", "curvature", "atm_total_variance")


@dataclass(frozen=True, slots=True)
class CharacteristicPercentile:
    name: str
    current: float | None
    percentile: float | None
    z_score: float | None
    distribution: DistributionSummary

    @property
    def is_reliable(self) -> bool:
        return self.distribution.is_reliable

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "current": self.current,
            "percentile": self.percentile,
            "z_score": self.z_score,
            "distribution": self.distribution.to_dict(),
            "is_reliable": self.is_reliable,
        }


@dataclass(frozen=True, slots=True)
class TenorHistory:
    tenor_days: int
    as_of: datetime | None
    observations: int
    percentiles: tuple[CharacteristicPercentile, ...]
    #: Every historical point, for plotting a term-structure time series.
    series: tuple[dict, ...] = ()

    @property
    def is_reliable(self) -> bool:
        return self.observations >= MIN_RELIABLE_OBSERVATIONS

    def to_dict(self, include_series: bool = True) -> dict:
        payload = {
            "tenor_days": self.tenor_days,
            "as_of_timestamp": self.as_of.isoformat() if self.as_of else None,
            "observations": self.observations,
            "is_reliable": self.is_reliable,
            "minimum_reliable_observations": MIN_RELIABLE_OBSERVATIONS,
            "percentiles": [p.to_dict() for p in self.percentiles],
        }
        if include_series:
            payload["series"] = list(self.series)
        return payload


def build_tenor_history(tenor_days: int, rows: list) -> TenorHistory:
    """Percentiles for one tenor from its ordered history.

    ``rows`` must be ordered oldest first; the most recent is treated as
    "current" and is included in its own distribution — excluding it would make
    a percentile of 100% impossible and quietly bias every reading downward.
    """
    if not rows:
        return TenorHistory(tenor_days, None, 0, ())

    latest = rows[-1]
    percentiles = []
    for name in CHARACTERISTIC_NAMES:
        sample = [getattr(row, name) for row in rows]
        current = getattr(latest, name)
        percentiles.append(
            CharacteristicPercentile(
                name=name,
                current=current,
                percentile=percentile_rank(current, sample),
                z_score=z_score(current, sample),
                distribution=summarise(sample),
            )
        )

    series = tuple(
        {
            "as_of_timestamp": row.as_of_timestamp.isoformat(),
            "time_to_expiry": row.time_to_expiry,
            "forward": row.forward,
            "atm_volatility": row.atm_volatility,
            "skew": row.skew,
            "curvature": row.curvature,
            "atm_total_variance": row.atm_total_variance,
            "method": row.method,
        }
        for row in rows
    )

    return TenorHistory(
        tenor_days=tenor_days,
        as_of=latest.as_of_timestamp,
        observations=len(rows),
        percentiles=tuple(percentiles),
        series=series,
    )
