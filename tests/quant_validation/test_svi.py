"""Raw SVI parameterization and its admissibility constraints.

Phase 0 ships the parameterization only; calibration and the Durrleman
butterfly condition arrive in Phase 2 (docs/backlog.md). What is testable now is
that inadmissible parameters are rejected at construction rather than producing
a surface with negative variance somewhere in the wings.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant.volatility.svi import (
    SVIParameters,
    raw_svi_implied_vol,
    raw_svi_total_variance,
)

TYPICAL = SVIParameters(a=0.010, b=0.045, rho=-0.55, m=0.015, sigma=0.10)


class TestConstraints:
    def test_a_typical_slice_is_admissible(self):
        TYPICAL.validate()
        assert TYPICAL.minimum_total_variance() > 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"b": -0.01},
            {"rho": 1.0},
            {"rho": -1.0},
            {"rho": 1.5},
            {"sigma": 0.0},
            {"sigma": -0.1},
        ],
    )
    def test_inadmissible_parameters_are_rejected_at_construction(self, kwargs):
        params = {"a": 0.01, "b": 0.045, "rho": -0.55, "m": 0.015, "sigma": 0.10}
        params.update(kwargs)
        with pytest.raises(ValueError):
            SVIParameters(**params)

    def test_negative_minimum_variance_is_rejected(self):
        """a + b*sigma*sqrt(1 - rho^2) >= 0, or the smile dips below zero."""
        with pytest.raises(ValueError, match="minimum total variance"):
            SVIParameters(a=-1.0, b=0.045, rho=-0.55, m=0.015, sigma=0.10)

    def test_minimum_total_variance_formula(self):
        expected = TYPICAL.a + TYPICAL.b * TYPICAL.sigma * math.sqrt(1 - TYPICAL.rho**2)
        assert TYPICAL.minimum_total_variance() == pytest.approx(expected)


class TestShape:
    def test_total_variance_is_positive_across_the_wings(self):
        k = np.linspace(-3.0, 3.0, 601)
        assert np.all(raw_svi_total_variance(k, TYPICAL) > 0)

    def test_the_minimum_matches_the_analytic_minimum(self):
        k = np.linspace(-5.0, 5.0, 100_001)
        numerical = float(raw_svi_total_variance(k, TYPICAL).min())
        assert numerical == pytest.approx(TYPICAL.minimum_total_variance(), rel=1e-6)

    def test_wings_are_asymptotically_linear(self):
        """w(k) -> a + b(rho +/- 1)(k - m) far from the money."""
        for k in (50.0, 100.0):
            left = float(raw_svi_total_variance(-k, TYPICAL))
            expected_left = TYPICAL.a + TYPICAL.b * (TYPICAL.rho - 1.0) * (-k - TYPICAL.m)
            assert left == pytest.approx(expected_left, rel=1e-3)

    def test_negative_rho_produces_a_downward_skew(self):
        """A put-side skew: lower strikes carry higher implied volatility."""
        low = float(raw_svi_implied_vol(-0.2, 0.25, TYPICAL))
        high = float(raw_svi_implied_vol(0.2, 0.25, TYPICAL))
        assert low > high

    def test_implied_vol_is_the_square_root_of_variance_over_time(self):
        tau = 0.5
        w = float(raw_svi_total_variance(0.1, TYPICAL))
        assert float(raw_svi_implied_vol(0.1, tau, TYPICAL)) == pytest.approx(math.sqrt(w / tau))

    def test_non_positive_maturity_is_rejected(self):
        with pytest.raises(ValueError):
            raw_svi_implied_vol(0.0, 0.0, TYPICAL)


class TestProperties:
    @given(
        a=st.floats(min_value=0.0, max_value=0.5),
        b=st.floats(min_value=0.0, max_value=1.0),
        rho=st.floats(min_value=-0.98, max_value=0.98),
        m=st.floats(min_value=-0.5, max_value=0.5),
        sigma=st.floats(min_value=1e-3, max_value=1.0),
        k=st.floats(min_value=-2.0, max_value=2.0),
    )
    @settings(max_examples=300, deadline=None)
    def test_admissible_parameters_never_produce_negative_variance(self, a, b, rho, m, sigma, k):
        params = SVIParameters(a=a, b=b, rho=rho, m=m, sigma=sigma)
        assert float(raw_svi_total_variance(k, params)) >= -1e-12

    @given(
        rho=st.floats(min_value=-0.95, max_value=0.95),
        m=st.floats(min_value=-0.3, max_value=0.3),
        sigma=st.floats(min_value=1e-2, max_value=0.5),
    )
    @settings(max_examples=100, deadline=None)
    def test_the_curve_is_convex_in_log_moneyness(self, rho, m, sigma):
        """Convexity of w in k is what makes the slice butterfly-arbitrage free
        in the limit; the full Durrleman condition is a Phase 2 check."""
        params = SVIParameters(a=0.02, b=0.05, rho=rho, m=m, sigma=sigma)
        k = np.linspace(-1.5, 1.5, 601)
        w = raw_svi_total_variance(k, params)
        second_difference = np.diff(w, 2)
        assert np.all(second_difference >= -1e-9)

    def test_parameters_serialise_for_provenance(self):
        payload = TYPICAL.to_dict()
        assert set(payload) == {"a", "b", "rho", "m", "sigma"}
        assert SVIParameters(**payload) == TYPICAL


class TestDerivatives:
    """Analytic derivatives, because the Durrleman condition needs exact ones."""

    @pytest.mark.parametrize("k", [-1.0, -0.3, 0.0, 0.05, 0.4, 1.2])
    def test_first_derivative_matches_finite_differences(self, k):
        from quant.volatility.svi import raw_svi_derivatives

        _w, dw, _d2w = raw_svi_derivatives(k, TYPICAL)
        h = 1e-7
        fd = (
            float(raw_svi_total_variance(k + h, TYPICAL))
            - float(raw_svi_total_variance(k - h, TYPICAL))
        ) / (2 * h)
        assert float(dw) == pytest.approx(fd, rel=1e-5, abs=1e-10)

    @pytest.mark.parametrize("k", [-1.0, -0.3, 0.0, 0.05, 0.4, 1.2])
    def test_second_derivative_matches_finite_differences(self, k):
        from quant.volatility.svi import raw_svi_derivatives

        _w, _dw, d2w = raw_svi_derivatives(k, TYPICAL)
        h = 1e-4
        fd = (
            float(raw_svi_total_variance(k + h, TYPICAL))
            - 2 * float(raw_svi_total_variance(k, TYPICAL))
            + float(raw_svi_total_variance(k - h, TYPICAL))
        ) / (h * h)
        assert float(d2w) == pytest.approx(fd, rel=1e-4, abs=1e-8)

    def test_second_derivative_is_strictly_positive(self):
        """``w`` is convex in ``k`` for every admissible parameter set."""
        from quant.volatility.svi import raw_svi_derivatives

        _w, _dw, d2w = raw_svi_derivatives(np.linspace(-2, 2, 201), TYPICAL)
        assert np.all(d2w > 0)


class TestButterflyConditions:
    """Durrleman's g and Lee's wing bound."""

    def test_a_typical_slice_is_butterfly_free(self):
        from quant.volatility.svi import durrleman_g, is_butterfly_free

        assert is_butterfly_free(TYPICAL)
        assert np.all(durrleman_g(np.linspace(-2, 2, 401), TYPICAL) > 0)

    def test_a_steep_slice_is_caught(self):
        """Large ``b`` with extreme ``rho``: the density goes negative."""
        from quant.volatility.svi import durrleman_g, is_butterfly_free

        steep = SVIParameters(a=0.001, b=1.5, rho=-0.9, m=0.0, sigma=0.02)
        assert not is_butterfly_free(steep)
        assert float(np.min(durrleman_g(np.linspace(-1, 1, 401), steep))) < 0

    def test_lee_wing_bound(self):
        from quant.volatility.svi import (
            LEE_WING_BOUND,
            satisfies_lee_bound,
            wing_slope,
        )

        assert wing_slope(TYPICAL) == pytest.approx(TYPICAL.b * (1 + abs(TYPICAL.rho)))
        assert satisfies_lee_bound(TYPICAL)

        # b(1+|rho|) = 1.2 * 1.9 = 2.28 > 2
        too_steep = SVIParameters(a=0.05, b=1.2, rho=-0.9, m=0.0, sigma=0.1)
        assert wing_slope(too_steep) > LEE_WING_BOUND
        assert not satisfies_lee_bound(too_steep)

    def test_the_wing_bound_is_the_asymptotic_slope(self):
        """Lee's moment formula bounds ``w(k)/|k|`` at 2 for large ``|k|``."""
        k = np.array([500.0, 1000.0])
        w = raw_svi_total_variance(k, TYPICAL)
        empirical = float((w[1] - w[0]) / (k[1] - k[0]))
        assert empirical == pytest.approx(TYPICAL.b * (1 + TYPICAL.rho), rel=1e-3)

    def test_min_g_reports_where_it_occurred(self):
        from quant.volatility.svi import min_durrleman_g

        value, location = min_durrleman_g(TYPICAL, -1.0, 1.0, 201)
        assert value > 0
        assert -1.0 <= location <= 1.0

    def test_g_is_undefined_at_zero_variance(self):
        from quant.volatility.svi import durrleman_g

        degenerate = SVIParameters(a=0.0, b=0.0, rho=0.0, m=0.0, sigma=0.1)
        with pytest.raises(ValueError, match="total variance"):
            durrleman_g(0.0, degenerate)
