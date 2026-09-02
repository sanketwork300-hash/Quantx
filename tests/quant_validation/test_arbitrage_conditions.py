"""Static no-arbitrage conditions.

Two directions: a clean synthetic chain must produce *no* violations (or the
detectors are too sensitive to be usable), and each seeded corruption must be
caught by the right detector with a sensible magnitude (or they are decorative).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant.pricing.black76 import black76_price
from quant.volatility.arbitrage import (
    check_butterfly,
    check_calendar,
    check_price_bounds,
    check_put_call_parity,
    check_vertical_spreads,
)

FORWARD, TAU, SIGMA = 100.0, 0.25, 0.20
DISCOUNT = math.exp(-0.05 * TAU)
STRIKES = np.arange(70.0, 131.0, 5.0)


@pytest.fixture
def clean():
    calls = np.asarray(black76_price(FORWARD, STRIKES, TAU, SIGMA, True, DISCOUNT))
    puts = np.asarray(black76_price(FORWARD, STRIKES, TAU, SIGMA, False, DISCOUNT))
    return STRIKES, calls, puts


class TestCleanChain:
    def test_no_bound_violations(self, clean):
        strikes, calls, puts = clean
        assert check_price_bounds(strikes, calls, FORWARD, DISCOUNT, True) == []
        assert check_price_bounds(strikes, puts, FORWARD, DISCOUNT, False) == []

    def test_parity_residuals_are_at_machine_precision(self, clean):
        strikes, calls, puts = clean
        residuals = check_put_call_parity(strikes, calls, puts, FORWARD, DISCOUNT)
        assert max(v.magnitude for v in residuals) < 1e-12

    def test_no_vertical_violations(self, clean):
        strikes, calls, puts = clean
        assert check_vertical_spreads(strikes, calls, DISCOUNT, True) == []
        assert check_vertical_spreads(strikes, puts, DISCOUNT, False) == []

    def test_no_butterfly_violations(self, clean):
        strikes, calls, puts = clean
        assert check_butterfly(strikes, calls) == []
        assert check_butterfly(strikes, puts) == []


class TestPriceBounds:
    def test_a_call_below_its_discounted_intrinsic_is_caught(self, clean):
        strikes, calls, _puts = clean
        broken = np.array(calls)
        broken[0] = 1.0  # deep ITM call priced at 1.0
        violations = check_price_bounds(strikes, broken, FORWARD, DISCOUNT, True)
        assert len(violations) == 1
        assert violations[0].detail["bound"] == "LOWER"
        expected = max(DISCOUNT * (FORWARD - strikes[0]), 0.0) - 1.0
        assert violations[0].magnitude == pytest.approx(expected)

    def test_a_call_above_the_discounted_forward_is_caught(self, clean):
        strikes, calls, _puts = clean
        broken = np.array(calls)
        broken[-1] = DISCOUNT * FORWARD + 5.0
        violations = check_price_bounds(strikes, broken, FORWARD, DISCOUNT, True)
        assert violations[0].detail["bound"] == "UPPER"
        assert violations[0].magnitude == pytest.approx(5.0)

    def test_put_upper_bound_is_the_discounted_strike(self, clean):
        strikes, _calls, puts = clean
        broken = np.array(puts)
        broken[-1] = DISCOUNT * strikes[-1] + 2.0
        violations = check_price_bounds(strikes, broken, FORWARD, DISCOUNT, False)
        assert violations[0].detail["bound"] == "UPPER"
        assert violations[0].magnitude == pytest.approx(2.0)

    def test_non_finite_prices_are_skipped_not_flagged(self, clean):
        strikes, calls, _puts = clean
        broken = np.array(calls)
        broken[2] = np.nan
        assert check_price_bounds(strikes, broken, FORWARD, DISCOUNT, True) == []


class TestParity:
    def test_a_broken_pair_is_measured_in_price_units(self, clean):
        strikes, calls, puts = clean
        broken = np.array(calls)
        broken[5] += 3.0
        violations = check_put_call_parity(strikes, broken, puts, FORWARD, DISCOUNT)
        worst = max(violations, key=lambda v: v.magnitude)
        assert worst.magnitude == pytest.approx(3.0, abs=1e-9)
        assert worst.detail["strike"] == pytest.approx(float(strikes[5]))


class TestVerticalSpreads:
    def test_a_call_rising_with_strike_is_caught(self, clean):
        strikes, calls, _puts = clean
        broken = np.array(calls)
        broken[6] = broken[5] + 1.0
        violations = check_vertical_spreads(strikes, broken, DISCOUNT, True)
        conditions = {v.detail["condition"] for v in violations}
        assert "MONOTONICITY" in conditions

    def test_a_call_falling_faster_than_the_discounted_strike_gap(self, clean):
        strikes, calls, _puts = clean
        broken = np.array(calls)
        broken[4] += 20.0  # makes the next gap exceed DF * dK
        violations = check_vertical_spreads(strikes, broken, DISCOUNT, True)
        assert any(v.detail["condition"] == "SLOPE_BOUND" for v in violations)

    def test_a_put_falling_with_strike_is_caught(self, clean):
        strikes, _calls, puts = clean
        broken = np.array(puts)
        broken[6] = broken[5] - 1.0
        violations = check_vertical_spreads(strikes, broken, DISCOUNT, False)
        assert any(v.detail["condition"] == "MONOTONICITY" for v in violations)


class TestButterfly:
    @staticmethod
    def _convexity(strikes, prices, index):
        k1, k2, k3 = strikes[index - 1], strikes[index], strikes[index + 1]
        c1, c2, c3 = prices[index - 1], prices[index], prices[index + 1]
        return ((k3 - k2) * c1 - (k3 - k1) * c2 + (k2 - k1) * c3) / (k3 - k1)

    def test_a_bumped_middle_strike_is_caught(self, clean):
        strikes, calls, _puts = clean
        bump = 4.0
        headroom = self._convexity(strikes, calls, 5)
        assert headroom > 0, "the clean chain must be strictly convex here"

        broken = np.array(calls)
        broken[5] += bump
        violations = check_butterfly(strikes, broken)
        centred = [v for v in violations if v.indices[1] == 5]
        assert centred, "the violation should be centred on the bumped strike"

        # On an evenly spaced grid a bump of d moves the butterfly value by -d,
        # so the breach is the bump minus whatever convexity was there to absorb it.
        assert centred[0].magnitude == pytest.approx(bump - headroom, rel=1e-6)

    def test_a_bump_smaller_than_the_local_convexity_is_absorbed(self, clean):
        """Not every distortion is a violation: the chain has real convexity to
        spend, and flagging inside it would drown the genuine breaches."""
        strikes, calls, _puts = clean
        headroom = self._convexity(strikes, calls, 5)
        broken = np.array(calls)
        broken[5] += headroom * 0.5
        assert [v for v in check_butterfly(strikes, broken) if v.indices[1] == 5] == []

    def test_unevenly_spaced_strikes_use_the_general_form(self):
        strikes = np.array([90.0, 100.0, 130.0])
        # A straight line through the outer strikes: convexity is exactly zero.
        prices = np.array([20.0, 15.0, 0.0])
        assert check_butterfly(strikes, prices) == []
        assert check_butterfly(strikes, np.array([20.0, 15.5, 0.0]))

    def test_magnitude_is_in_price_units(self):
        strikes = np.array([95.0, 100.0, 105.0])
        prices = np.array([10.0, 8.0, 5.0])  # convex: 10 - 16 + 5 = -1 -> breach 0.5
        violations = check_butterfly(strikes, prices)
        assert violations[0].magnitude == pytest.approx(0.5)


class TestCalendar:
    def test_a_falling_total_variance_is_caught(self):
        k = np.linspace(-0.3, 0.3, 7)
        short = np.full(7, 0.02)
        long = np.array([0.03] * 3 + [0.015] + [0.03] * 3)
        violations = check_calendar(k, short, long)
        assert len(violations) == 1
        assert violations[0].magnitude == pytest.approx(0.005)
        assert violations[0].detail["log_moneyness"] == pytest.approx(0.0)

    def test_a_rising_term_structure_is_clean(self):
        k = np.linspace(-0.3, 0.3, 7)
        assert check_calendar(k, np.full(7, 0.02), np.full(7, 0.05)) == []

    def test_equal_variances_are_not_a_violation(self):
        """Flat forward variance is admissible, if unusual."""
        k = np.linspace(-0.3, 0.3, 7)
        assert check_calendar(k, np.full(7, 0.02), np.full(7, 0.02)) == []


class TestReporting:
    def test_every_violation_carries_indices_and_a_magnitude(self, clean):
        strikes, calls, _puts = clean
        broken = np.array(calls)
        broken[5] += 4.0
        for violation in check_butterfly(strikes, broken):
            assert violation.indices
            assert violation.magnitude > 0
            assert violation.detail

    def test_magnitudes_are_never_booleans(self, clean):
        """A boolean would erase the difference between noise and a real
        problem, which is the whole reason severity is magnitude-based."""
        strikes, calls, _puts = clean
        small = np.array(calls)
        small[5] += 1.0
        large = np.array(calls)
        large[5] += 10.0
        assert (
            check_butterfly(strikes, small)[0].magnitude
            < check_butterfly(strikes, large)[0].magnitude
        )
