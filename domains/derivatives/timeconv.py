"""Time to expiry: the policy, not just the arithmetic.

An option's remaining life is one of the three numbers that determine its
implied volatility, and it is the one most often computed sloppily. The two
decisions that matter are recorded explicitly here so they land in provenance:

1. **Which instant does the contract expire at?** A date is not an instant. If
   the data source gives a settlement time, that is used; otherwise the caller
   supplies a convention and the result is flagged as assumed. It is never
   guessed silently, and never defaulted to midnight, which would misprice
   every same-day expiry.

2. **Which day count?** ACT/365 Fixed by default, computed in seconds and
   divided, so a weekly option does not lose a seventh of its life at every
   intraday moment.

``T <= 0`` is returned as-is. It is a structured non-result for the caller to
report, never something to clamp to a small positive number to keep a formula
alive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from quant.daycount import DEFAULT_DAY_COUNT, DayCount, year_fraction


@dataclass(frozen=True, slots=True)
class ExpiryPolicy:
    """How to turn an expiry *date* into an expiry *instant*."""

    #: Settlement time on the expiry date. ``None`` means unknown, and the
    #: result will say so rather than inventing one.
    settlement_time_utc: time | None = None
    day_count: DayCount = DEFAULT_DAY_COUNT

    def to_provenance(self) -> dict:
        return {
            "settlement_time_utc": (
                self.settlement_time_utc.isoformat() if self.settlement_time_utc else None
            ),
            "day_count": str(self.day_count),
        }


@dataclass(frozen=True, slots=True)
class TimeToExpiry:
    years: float | None
    expiry_instant: datetime | None
    day_count: DayCount
    #: True when the settlement time came from policy rather than from data.
    settlement_time_assumed: bool
    reason: str | None = None

    @property
    def is_positive(self) -> bool:
        return self.years is not None and self.years > 0

    def to_dict(self) -> dict:
        return {
            "years": self.years,
            "expiry_instant": (self.expiry_instant.isoformat() if self.expiry_instant else None),
            "day_count": str(self.day_count),
            "settlement_time_assumed": self.settlement_time_assumed,
            "reason": self.reason,
        }


UNKNOWN_SETTLEMENT_TIME = "SETTLEMENT_TIME_UNKNOWN"
EXPIRED = "OPTION_EXPIRED"


def time_to_expiry(
    as_of: datetime,
    expiry: date,
    policy: ExpiryPolicy,
    expiry_instant: datetime | None = None,
) -> TimeToExpiry:
    """Year fraction from ``as_of`` to the contract's expiry instant."""
    assumed = False
    instant = expiry_instant
    if instant is None:
        if policy.settlement_time_utc is None:
            return TimeToExpiry(
                years=None,
                expiry_instant=None,
                day_count=policy.day_count,
                settlement_time_assumed=False,
                reason=UNKNOWN_SETTLEMENT_TIME,
            )
        instant = datetime.combine(expiry, policy.settlement_time_utc, tzinfo=UTC)
        assumed = True

    years = year_fraction(as_of, instant, policy.day_count)
    return TimeToExpiry(
        years=years,
        expiry_instant=instant,
        day_count=policy.day_count,
        settlement_time_assumed=assumed,
        reason=None if years > 0 else EXPIRED,
    )
