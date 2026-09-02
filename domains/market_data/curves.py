"""Yield curves.

Rates are **continuously compounded** throughout the platform unless a curve
says otherwise, and the discount factor is ``exp(-z(t) * t)``.

A curve is an input to a valuation exactly like a quote is, so it carries an id,
a source and an as-of timestamp, and it serialises into provenance. "We used a
flat 6.5%" is a legitimate configuration; "we used 0.9839" is not, because it
cannot be reproduced or argued with.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np

from quant.daycount import DEFAULT_DAY_COUNT, DayCount, year_fraction
from quant.interpolation import Extrapolation, linear_interpolate


class CurveInterpolation(StrEnum):
    #: Linear in the continuously compounded zero rate, flat outside the range.
    LINEAR_ZERO = "LINEAR_ZERO"
    #: Constant zero rate everywhere.
    FLAT = "FLAT"


class CurveError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class YieldCurve:
    """A discount curve, addressed by a content-derived id.

    Two curves with the same id were built from the same numbers, so a stored
    ``yield_curve_id`` in a provenance record is enough to rebuild the exact
    discounting a six-month-old analysis used.
    """

    as_of: datetime
    currency: str
    #: Year fractions, strictly increasing.
    times: tuple[float, ...]
    #: Continuously compounded zero rates at those times.
    zero_rates: tuple[float, ...]
    source: str = "user"
    day_count: DayCount = DEFAULT_DAY_COUNT
    interpolation: CurveInterpolation = CurveInterpolation.LINEAR_ZERO
    label: str | None = None

    def __post_init__(self) -> None:
        if len(self.times) != len(self.zero_rates):
            raise CurveError(
                f"length mismatch: {len(self.times)} times, {len(self.zero_rates)} rates"
            )
        if not self.times:
            raise CurveError("a curve needs at least one point")
        if any(t < 0 for t in self.times):
            raise CurveError("curve times must be non-negative")
        if len(self.times) > 1 and any(
            later <= earlier for earlier, later in zip(self.times, self.times[1:], strict=False)
        ):
            raise CurveError("curve times must be strictly increasing")
        if len(self.currency) != 3:
            raise CurveError(f"currency must be a 3-letter ISO code, got {self.currency!r}")

    # --------------------------------------------------------- constructors
    @classmethod
    def flat(
        cls,
        rate: float,
        as_of: datetime,
        currency: str,
        source: str = "user",
        day_count: DayCount = DEFAULT_DAY_COUNT,
        label: str | None = None,
    ) -> YieldCurve:
        """A single continuously compounded rate at every maturity.

        The honest default when no term structure is available: it is an
        assumption, it is recorded as one, and it is visible in provenance
        rather than buried in a discount factor.
        """
        return cls(
            as_of=as_of,
            currency=currency,
            times=(1.0,),
            zero_rates=(float(rate),),
            source=source,
            day_count=day_count,
            interpolation=CurveInterpolation.FLAT,
            label=label or f"flat-{rate:.6f}",
        )

    # -------------------------------------------------------------- queries
    @property
    def is_flat(self) -> bool:
        return self.interpolation is CurveInterpolation.FLAT or len(self.times) == 1

    def zero_rate(self, tau: float) -> float:
        if self.is_flat:
            return float(self.zero_rates[0])
        return float(
            linear_interpolate(
                tau,
                np.asarray(self.times),
                np.asarray(self.zero_rates),
                Extrapolation.FLAT,
            )
        )

    def discount_factor(self, tau: float) -> float:
        """``exp(-z(tau) * tau)``. Returns 1.0 at or before the as-of date."""
        if tau <= 0:
            return 1.0
        return float(np.exp(-self.zero_rate(tau) * tau))

    def discount_factor_to(self, when: datetime) -> float:
        return self.discount_factor(self.year_fraction_to(when))

    def year_fraction_to(self, when: datetime) -> float:
        return year_fraction(self.as_of, when, self.day_count)

    def forward_rate(self, start: float, end: float) -> float:
        """Continuously compounded forward rate between two maturities."""
        if end <= start:
            raise CurveError(f"end {end} must exceed start {start}")
        numerator = self.zero_rate(end) * end - self.zero_rate(start) * start
        return numerator / (end - start)

    # ----------------------------------------------------------- provenance
    @property
    def curve_id(self) -> str:
        payload = {
            "as_of": self.as_of.isoformat(),
            "currency": self.currency,
            "times": list(self.times),
            "zero_rates": list(self.zero_rates),
            "day_count": str(self.day_count),
            "interpolation": str(self.interpolation),
            "source": self.source,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
        return f"curve:{digest.hexdigest()[:16]}"

    def to_provenance(self) -> dict:
        return {
            "curve_id": self.curve_id,
            "as_of": self.as_of.isoformat(),
            "currency": self.currency,
            "source": self.source,
            "day_count": str(self.day_count),
            "interpolation": str(self.interpolation),
            "label": self.label,
            "times": list(self.times),
            "zero_rates": list(self.zero_rates),
        }
