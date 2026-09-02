"""Day-count conventions, the time-to-expiry policy, and yield curves."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta

import pytest

from domains.derivatives.timeconv import (
    EXPIRED,
    UNKNOWN_SETTLEMENT_TIME,
    ExpiryPolicy,
    time_to_expiry,
)
from domains.market_data.curves import CurveError, CurveInterpolation, YieldCurve
from quant.daycount import DayCount, days_between, year_fraction

NOW = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)


class TestDayCount:
    def test_one_calendar_year_act_365f(self):
        assert year_fraction(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
        ) == pytest.approx(1.0)

    def test_denominators(self):
        start, end = NOW, NOW + timedelta(days=365)
        assert year_fraction(start, end, DayCount.ACT_365F) == pytest.approx(1.0)
        assert year_fraction(start, end, DayCount.ACT_360) == pytest.approx(365 / 360)
        assert year_fraction(start, end, DayCount.ACT_365_25) == pytest.approx(365 / 365.25)

    def test_intraday_time_is_not_rounded_to_whole_days(self):
        """A weekly option must not lose a seventh of its life at every moment."""
        morning = year_fraction(NOW, NOW + timedelta(days=7))
        afternoon = year_fraction(NOW + timedelta(hours=6), NOW + timedelta(days=7))
        assert morning > afternoon
        assert morning - afternoon == pytest.approx(0.25 / 365.0)

    def test_expired_returns_a_negative_value_not_a_clamp(self):
        assert year_fraction(NOW, NOW - timedelta(days=1)) < 0

    def test_naive_datetimes_are_rejected(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            year_fraction(datetime(2026, 1, 1), datetime(2027, 1, 1, tzinfo=UTC))

    def test_timezone_is_normalised_not_ignored(self):
        from datetime import timezone

        ist = timezone(timedelta(hours=5, minutes=30))
        same_instant = NOW.astimezone(ist)
        assert year_fraction(same_instant, NOW + timedelta(days=30)) == pytest.approx(
            year_fraction(NOW, NOW + timedelta(days=30))
        )

    def test_thirty_360(self):
        fraction = year_fraction(
            datetime(2026, 1, 15, tzinfo=UTC),
            datetime(2026, 7, 15, tzinfo=UTC),
            DayCount.THIRTY_360,
        )
        assert fraction == pytest.approx(0.5)

    def test_days_between(self):
        assert days_between(NOW, NOW + timedelta(days=35, hours=12)) == pytest.approx(35.5)

    def test_act_252_is_deliberately_absent(self):
        """It counts business days and would need a fabricated calendar."""
        assert "ACT/252" not in {str(convention) for convention in DayCount}


class TestExpiryPolicy:
    def test_uses_the_supplied_settlement_time(self):
        result = time_to_expiry(
            NOW, date(2026, 10, 29), ExpiryPolicy(settlement_time_utc=time(10, 0))
        )
        assert result.expiry_instant == datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        assert result.settlement_time_assumed is True
        assert result.years == pytest.approx(35.02777 / 365.0, rel=1e-5)

    def test_an_explicit_instant_is_not_an_assumption(self):
        instant = datetime(2026, 10, 29, 15, 30, tzinfo=UTC)
        result = time_to_expiry(
            NOW,
            date(2026, 10, 29),
            ExpiryPolicy(settlement_time_utc=time(10, 0)),
            expiry_instant=instant,
        )
        assert result.expiry_instant == instant
        assert result.settlement_time_assumed is False

    def test_without_a_settlement_time_the_answer_is_unknown_not_midnight(self):
        """Defaulting to midnight would misprice every same-day expiry."""
        result = time_to_expiry(NOW, date(2026, 10, 29), ExpiryPolicy())
        assert result.years is None
        assert result.reason == UNKNOWN_SETTLEMENT_TIME
        assert not result.is_positive

    def test_a_past_expiry_is_reported_not_clamped(self):
        result = time_to_expiry(
            NOW, date(2026, 9, 1), ExpiryPolicy(settlement_time_utc=time(10, 0))
        )
        assert result.years < 0
        assert result.reason == EXPIRED
        assert not result.is_positive

    def test_policy_serialises_for_provenance(self):
        payload = ExpiryPolicy(settlement_time_utc=time(10, 0)).to_provenance()
        assert payload["settlement_time_utc"] == "10:00:00"
        assert payload["day_count"] == "ACT/365F"


class TestYieldCurve:
    def test_flat_curve_discounting(self):
        curve = YieldCurve.flat(0.065, NOW, "INR", source="assumption")
        assert curve.zero_rate(0.25) == 0.065
        assert curve.discount_factor(0.25) == pytest.approx(math.exp(-0.065 * 0.25))
        assert curve.is_flat

    def test_discount_factor_is_one_at_or_before_the_as_of(self):
        curve = YieldCurve.flat(0.065, NOW, "INR")
        assert curve.discount_factor(0.0) == 1.0
        assert curve.discount_factor(-1.0) == 1.0

    def test_term_structure_interpolates_the_zero_rate(self):
        curve = YieldCurve(as_of=NOW, currency="INR", times=(0.25, 1.0), zero_rates=(0.06, 0.07))
        assert curve.zero_rate(0.625) == pytest.approx(0.065)

    def test_extrapolation_is_flat_and_documented(self):
        curve = YieldCurve(as_of=NOW, currency="INR", times=(0.25, 1.0), zero_rates=(0.06, 0.07))
        assert curve.zero_rate(10.0) == 0.07
        assert curve.zero_rate(0.01) == 0.06

    def test_forward_rate(self):
        curve = YieldCurve(as_of=NOW, currency="USD", times=(1.0, 2.0), zero_rates=(0.05, 0.06))
        # (0.06*2 - 0.05*1) / (2 - 1)
        assert curve.forward_rate(1.0, 2.0) == pytest.approx(0.07)

    def test_forward_rate_requires_an_ordered_interval(self):
        curve = YieldCurve.flat(0.05, NOW, "USD")
        with pytest.raises(CurveError):
            curve.forward_rate(2.0, 1.0)

    def test_curve_id_is_content_addressed(self):
        first = YieldCurve(as_of=NOW, currency="INR", times=(1.0,), zero_rates=(0.065,))
        same = YieldCurve(as_of=NOW, currency="INR", times=(1.0,), zero_rates=(0.065,))
        different = YieldCurve(as_of=NOW, currency="INR", times=(1.0,), zero_rates=(0.070,))
        assert first.curve_id == same.curve_id
        assert first.curve_id != different.curve_id
        assert first.curve_id.startswith("curve:")

    def test_provenance_round_trips_the_curve(self):
        curve = YieldCurve(as_of=NOW, currency="INR", times=(0.25, 1.0), zero_rates=(0.06, 0.07))
        payload = curve.to_provenance()
        rebuilt = YieldCurve(
            as_of=NOW,
            currency=payload["currency"],
            times=tuple(payload["times"]),
            zero_rates=tuple(payload["zero_rates"]),
            source=payload["source"],
            interpolation=CurveInterpolation(payload["interpolation"]),
        )
        assert rebuilt.curve_id == curve.curve_id

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"times": (), "zero_rates": ()},
            {"times": (1.0, 0.5), "zero_rates": (0.05, 0.06)},
            {"times": (1.0,), "zero_rates": (0.05, 0.06)},
            {"times": (-1.0,), "zero_rates": (0.05,)},
        ],
    )
    def test_malformed_curves_are_rejected(self, kwargs):
        with pytest.raises(CurveError):
            YieldCurve(as_of=NOW, currency="INR", **kwargs)

    def test_currency_must_be_iso(self):
        with pytest.raises(CurveError):
            YieldCurve(as_of=NOW, currency="RUPEE", times=(1.0,), zero_rates=(0.05,))
