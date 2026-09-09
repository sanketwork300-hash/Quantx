"""Skew quoted by delta, which is how a market quotes it.

The number this produces is the one a user will compare against their broker's
runs, and the two most likely ways to disagree are a convention mismatch and a
strike solved outside the range anything was actually quoted at. Both are what
these tests are about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from domains.derivatives.delta_skew import DELTA_LEVELS, slice_delta_skew, surface_delta_skew
from domains.derivatives.surface import SurfaceSliceFit, VolatilitySurface
from quant.volatility.delta_skew import (
    DeltaConvention,
    DeltaSolveStatus,
    delta_smile,
    forward_delta,
    solve_delta_strike,
)
from quant.volatility.svi import SVIParameters
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult

FORWARD = 24_000.0
TAU = 0.25


def flat(level: float = 0.20):
    return lambda _k: level


def sloped(level: float = 0.20, slope: float = -0.30):
    """The usual equity-index shape: volatility falls as strike rises."""
    return lambda k: level + slope * k


class TestForwardDelta:
    def test_an_at_the_money_forward_call_is_near_a_half(self):
        assert forward_delta(0.0, 0.20, TAU, is_call=True) == pytest.approx(0.52, abs=0.03)

    def test_call_and_put_deltas_differ_by_one(self):
        """The forward-delta parity that makes a 25-delta call and a 25-delta
        put describe two different strikes rather than the same one."""
        for k in (-0.3, 0.0, 0.4):
            call = forward_delta(k, 0.22, TAU, is_call=True)
            put = forward_delta(k, 0.22, TAU, is_call=False)
            assert call - put == pytest.approx(1.0)

    def test_at_zero_time_delta_is_the_intrinsic_step(self):
        """Not an error and not a smoothed value: at expiry the option is its
        intrinsic value and its delta really is a step."""
        assert forward_delta(-0.1, 0.2, 0.0, is_call=True) == 1.0
        assert forward_delta(0.1, 0.2, 0.0, is_call=True) == 0.0


class TestSolvingForAStrike:
    def test_the_solved_strike_really_has_that_delta(self):
        solved = solve_delta_strike(0.25, True, sloped(), FORWARD, TAU)
        assert solved.status is DeltaSolveStatus.OK
        assert forward_delta(
            solved.log_moneyness, solved.volatility, TAU, is_call=True
        ) == pytest.approx(0.25, abs=1e-9)

    def test_the_strike_is_the_forward_times_exp_k(self):
        solved = solve_delta_strike(0.25, True, flat(), FORWARD, TAU)
        assert solved.strike == pytest.approx(
            FORWARD * pow(2.718281828459045, solved.log_moneyness)
        )

    def test_a_strike_outside_the_fitted_range_is_flagged_not_hidden(self):
        """A 10-delta strike is often outside anything that was quoted. The
        number is still useful; presenting it as if it were fitted is not."""
        solved = solve_delta_strike(0.10, True, sloped(), FORWARD, TAU, fitted_range=(-0.05, 0.05))
        assert solved.status is DeltaSolveStatus.EXTRAPOLATED
        assert solved.ok is True

    def test_a_delta_that_occurs_nowhere_is_reported_not_widened_into(self):
        """Widening the search silently would put a strike far outside the
        quoted market into a skew number."""
        solved = solve_delta_strike(
            0.999999, True, flat(0.05), FORWARD, TAU, k_lower=-0.01, k_upper=0.01
        )
        assert solved.status is DeltaSolveStatus.NOT_BRACKETED
        assert solved.ok is False
        assert solved.strike is None

    def test_a_sign_that_does_not_match_the_side_is_refused(self):
        """A call delta is positive and a put delta is negative. Accepting the
        wrong sign would silently solve for the opposite wing."""
        assert (
            solve_delta_strike(-0.25, True, flat(), FORWARD, TAU).status
            is DeltaSolveStatus.UNUSABLE_SLICE
        )
        assert (
            solve_delta_strike(0.25, False, flat(), FORWARD, TAU).status
            is DeltaSolveStatus.UNUSABLE_SLICE
        )

    def test_an_expired_slice_produces_no_strike(self):
        assert (
            solve_delta_strike(0.25, True, flat(), FORWARD, 0.0).status
            is DeltaSolveStatus.UNUSABLE_SLICE
        )

    def test_every_result_names_the_convention_it_used(self):
        """A number whose delta convention is unstated cannot be reconciled
        with anyone else's."""
        solved = solve_delta_strike(0.25, True, flat(), FORWARD, TAU)
        assert solved.convention is DeltaConvention.FORWARD
        assert solved.to_dict()["delta_convention"] == "FORWARD"


class TestRiskReversalAndButterfly:
    def test_a_flat_smile_has_exactly_no_skew(self):
        smile = delta_smile(0.25, flat(), FORWARD, TAU)
        assert smile.risk_reversal == pytest.approx(0.0, abs=1e-12)
        assert smile.butterfly == pytest.approx(0.0, abs=1e-12)

    def test_the_usual_equity_shape_gives_a_negative_risk_reversal(self):
        smile = delta_smile(0.25, sloped(), FORWARD, TAU)
        assert smile.risk_reversal < 0

    def test_an_inverted_smile_gives_a_positive_one(self):
        smile = delta_smile(0.25, sloped(slope=+0.30), FORWARD, TAU)
        assert smile.risk_reversal > 0

    def test_a_convex_smile_has_a_positive_butterfly(self):
        smile = delta_smile(0.25, lambda k: 0.20 + 0.1 * k * k, FORWARD, TAU)
        assert smile.butterfly > 0

    def test_a_smile_whose_delta_turns_back_on_itself_is_refused(self):
        """With wings steep enough to break the no-arbitrage slope bound, delta
        stops being monotone in strike and a delta level can occur at more than
        one strike. Reported as unmeasurable rather than resolved by picking one
        of them without saying which."""
        smile = delta_smile(0.25, lambda k: 0.20 + 0.5 * k * k, FORWARD, TAU)
        assert smile.call.status is DeltaSolveStatus.NOT_BRACKETED
        assert smile.risk_reversal is None

    def test_the_ten_delta_wing_is_wider_than_the_twenty_five(self):
        near = delta_smile(0.25, sloped(), FORWARD, TAU)
        far = delta_smile(0.10, sloped(), FORWARD, TAU)
        assert abs(far.risk_reversal) > abs(near.risk_reversal)

    def test_an_unmeasurable_wing_gives_null_rather_than_zero(self):
        """A skew of zero and a skew that could not be measured are different
        statements, and a chart that cannot tell them apart is worse than a gap
        in the line."""
        smile = delta_smile(
            0.25,
            flat(0.02),
            FORWARD,
            TAU,
        )
        # Force the failure by asking for a delta the smile cannot reach.
        broken = delta_smile(0.4999999, flat(1e-6), FORWARD, 1e-9)
        assert broken.risk_reversal is None
        assert broken.measured is False
        assert smile.measured is True


def _fit(expiry: date, params: SVIParameters, tau: float = TAU, **kwargs) -> SurfaceSliceFit:
    return SurfaceSliceFit(
        expiry=expiry,
        time_to_expiry=tau,
        forward=FORWARD,
        discount_factor=1.0,
        parameters=params,
        calibration=SVICalibrationResult(
            parameters=params, status=CalibrationStatus.CONVERGED, n_observations=20
        ),
        **kwargs,
    )


class TestOverAFittedSurface:
    #: A downward-sloping SVI slice: rho < 0 is the equity-index skew.
    PARAMS = SVIParameters(a=0.004, b=0.10, rho=-0.6, m=0.01, sigma=0.10)

    def test_a_fitted_slice_produces_both_delta_levels(self):
        result = slice_delta_skew(_fit(date(2026, 10, 29), self.PARAMS))
        assert [smile.delta_level for smile in result.smiles] == list(DELTA_LEVELS)
        assert result.at(0.25).risk_reversal < 0

    def test_the_atm_reference_is_the_volatility_at_zero_log_moneyness(self):
        """The same reference the surface characteristics use, so a butterfly
        here and a level there are measured against the same thing."""
        fit = _fit(date(2026, 10, 29), self.PARAMS)
        result = slice_delta_skew(fit)
        assert result.atm_volatility == pytest.approx(float(fit.implied_vol(0.0)))

    def test_a_slice_with_no_fit_is_listed_as_unmeasured_not_dropped(self):
        """A term structure with a silent hole in it reads as a smooth curve."""
        failed = SurfaceSliceFit(
            expiry=date(2026, 11, 26),
            time_to_expiry=0.4,
            forward=FORWARD,
            discount_factor=1.0,
            parameters=None,
            calibration=SVICalibrationResult(
                parameters=None, status=CalibrationStatus.FAILED, n_observations=3
            ),
        )
        surface = VolatilitySurface(
            underlying_id=None,
            as_of=datetime(2026, 9, 9, tzinfo=UTC),
            slices=(_fit(date(2026, 10, 29), self.PARAMS), failed),
            curve_id="flat",
        )
        skew = surface_delta_skew(surface)
        assert len(skew.slices) == 1
        assert [item.expiry for item in skew.unmeasured] == [date(2026, 11, 26)]
        assert "no fitted parameters" in skew.unmeasured[0].reason

    def test_the_term_structure_keeps_a_gap_as_a_gap(self):
        surface = VolatilitySurface(
            underlying_id=None,
            as_of=datetime(2026, 9, 9, tzinfo=UTC),
            slices=(
                _fit(date(2026, 10, 29), self.PARAMS, tau=0.10),
                _fit(date(2026, 11, 26), self.PARAMS, tau=0.30),
            ),
            curve_id="flat",
        )
        rows = surface_delta_skew(surface).term_structure(0.25)
        assert [row[0] for row in rows] == [0.10, 0.30]
        assert all(row[1] is not None for row in rows)

    def test_a_degraded_fit_is_measured_but_marked(self):
        """A degraded fit is not a missing one; a caller has to be able to see
        that the smile they are reading was not clean."""
        degraded = SurfaceSliceFit(
            expiry=date(2026, 10, 29),
            time_to_expiry=TAU,
            forward=FORWARD,
            discount_factor=1.0,
            parameters=self.PARAMS,
            calibration=SVICalibrationResult(
                parameters=self.PARAMS,
                status=CalibrationStatus.DEGRADED,
                n_observations=4,
            ),
        )
        result = slice_delta_skew(degraded)
        assert result.degraded is True
        assert result.at(0.25).risk_reversal is not None
