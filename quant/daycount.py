"""Day-count conventions and year fractions.

Lives in ``quant`` because it is pure, dependency-free date arithmetic that both
the derivatives domain (time to expiry) and the curve model (discount factors)
need. Putting it in either domain would force the other to import across a
domain boundary for what is really a shared convention.

The convention used by a calculation is always recorded in provenance. "We used
ACT/365F" is a legitimate statement; "we used 0.25" is not, because it cannot be
reproduced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

SECONDS_PER_DAY = 86_400.0


class DayCount(StrEnum):
    """Supported conventions.

    ACT/252 is deliberately absent: it counts *business* days and therefore
    needs an exchange calendar. Inventing one would be exactly the fabrication
    the platform forbids, so it arrives with a real calendar source (see
    docs/references.md, QuantLib -> USE DIRECTLY for calendars).
    """

    ACT_365F = "ACT/365F"
    ACT_360 = "ACT/360"
    ACT_365_25 = "ACT/365.25"
    THIRTY_360 = "30/360"

    @property
    def denominator(self) -> float:
        return {
            DayCount.ACT_365F: 365.0,
            DayCount.ACT_360: 360.0,
            DayCount.ACT_365_25: 365.25,
            DayCount.THIRTY_360: 360.0,
        }[self]


DEFAULT_DAY_COUNT = DayCount.ACT_365F


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime rejected; platform timestamps are UTC-aware")
    return value.astimezone(UTC)


def _thirty_360_days(start: date, end: date) -> int:
    """US 30/360 (bond basis) day count."""
    d1, d2 = min(start.day, 30), end.day
    if d1 == 30 and d2 == 31:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def year_fraction(
    start: datetime, end: datetime, convention: DayCount = DEFAULT_DAY_COUNT
) -> float:
    """Year fraction between two instants.

    Computed in seconds and divided, not in whole days: a weekly option priced
    with a whole-day count loses roughly a seventh of its remaining life at
    every intraday moment, which moves its implied volatility visibly.

    Returns a **non-positive** value when ``end <= start``. Callers must treat
    that as a structured non-result (the option has expired), never clamp it to
    a small positive number to keep a formula alive.
    """
    start_utc, end_utc = _as_utc(start), _as_utc(end)

    if convention is DayCount.THIRTY_360:
        # 30/360 is defined on dates; the intraday remainder is carried on the
        # actual-day scale so the function stays continuous in time.
        whole = _thirty_360_days(start_utc.date(), end_utc.date())
        remainder = (
            end_utc - datetime.combine(end_utc.date(), datetime.min.time(), tzinfo=UTC)
        ).total_seconds() - (
            start_utc - datetime.combine(start_utc.date(), datetime.min.time(), tzinfo=UTC)
        ).total_seconds()
        return (whole + remainder / SECONDS_PER_DAY) / convention.denominator

    seconds = (end_utc - start_utc).total_seconds()
    return seconds / (SECONDS_PER_DAY * convention.denominator)


def days_between(start: datetime, end: datetime) -> float:
    """Actual days, fractional. Used for per-day theta scaling."""
    return (_as_utc(end) - _as_utc(start)).total_seconds() / SECONDS_PER_DAY
