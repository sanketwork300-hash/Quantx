"""SVI calibration.

The headline check is parameter recovery: generate a slice from known
parameters, fit it back, get them. But the more useful finding is where that
*fails* — SVI's five parameters are not identifiable from a narrow strike
window, and the tests below pin that down rather than papering over it, because
the platform warns the user about exactly this.
"""

from __future__ import annotations

import numpy as np

from quant.volatility.svi import (
    LEE_WING_BOUND,
    SVIParameters,
    is_butterfly_free,
    raw_svi_total_variance,
)
from quant.volatility.svi_calibration import (
    MIN_OBSERVATIONS,
    CalibrationStatus,
    calibrate_svi,
)

TRUTH = SVIParameters(a=0.010, b=0.045, rho=-0.55, m=0.015, sigma=0.10)
TAU = 0.25

PARAMETER_NAMES = ("a", "b", "rho", "m", "sigma")


def slice_for(k: np.ndarray, params: SVIParameters = TRUTH) -> np.ndarray:
    return raw_svi_total_variance(k, params)


def max_parameter_error(fitted: SVIParameters, truth: SVIParameters = TRUTH) -> float:
    return max(abs(getattr(fitted, name) - getattr(truth, name)) for name in PARAMETER_NAMES)


def curve_error_vol_points(
    fitted: SVIParameters, k: np.ndarray, truth: SVIParameters = TRUTH
) -> float:
    fitted_vol = np.sqrt(raw_svi_total_variance(k, fitted) / TAU)
    true_vol = np.sqrt(raw_svi_total_variance(k, truth) / TAU)
    return float(np.max(np.abs(fitted_vol - true_vol)) * 100.0)


class TestParameterRecovery:
    def test_a_wide_slice_recovers_the_parameters(self):
        k = np.linspace(-0.8, 0.8, 25)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.status is CalibrationStatus.CONVERGED
        assert max_parameter_error(result.parameters) < 1e-4

    def test_a_medium_slice_recovers_the_parameters(self):
        k = np.linspace(-0.25, 0.25, 25)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.status is CalibrationStatus.CONVERGED
        assert max_parameter_error(result.parameters) < 1e-3

    def test_the_fit_is_essentially_exact_in_sample(self):
        k = np.linspace(-0.4, 0.4, 21)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.rmse_vol_points < 1e-3
        assert result.max_error_vol_points < 1e-2

    def test_metrics_are_reported_in_both_units(self):
        k = np.linspace(-0.4, 0.4, 21)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.rmse_total_variance is not None
        assert result.weighted_rmse is not None
        # Volatility points are the unit practitioners actually read.
        assert result.rmse_vol_points is not None
        assert result.max_error_vol_points >= result.rmse_vol_points


class TestIdentifiability:
    """What a narrow strike window can and cannot tell you."""

    def test_a_narrow_window_fits_well_but_does_not_identify_the_parameters(self):
        """The finding that motivates SURFACE_NARROW_STRIKE_RANGE.

        On a realistic retail chain spanning about 0.1 in log-moneyness, many
        parameter sets fit the observed arc equally well. The curve is right
        where there is data and unconstrained where there is not.
        """
        k = np.linspace(-0.07, 0.03, 25)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.status is CalibrationStatus.CONVERGED

        assert curve_error_vol_points(result.parameters, k) < 0.05, "in sample: excellent"
        assert max_parameter_error(result.parameters) > 1e-3, (
            "parameters are not identifiable over so narrow a window"
        )

    def test_widening_the_window_improves_wing_accuracy(self):
        wings = np.array([-0.5, 0.5])
        narrow = calibrate_svi(
            np.linspace(-0.07, 0.03, 25), slice_for(np.linspace(-0.07, 0.03, 25)), TAU
        )
        wide = calibrate_svi(np.linspace(-0.6, 0.6, 25), slice_for(np.linspace(-0.6, 0.6, 25)), TAU)
        assert curve_error_vol_points(wide.parameters, wings) < curve_error_vol_points(
            narrow.parameters, wings
        )


class TestConstraints:
    def test_fitted_parameters_are_always_admissible(self):
        k = np.linspace(-0.5, 0.5, 21)
        result = calibrate_svi(k, slice_for(k), TAU)
        params = result.parameters
        assert params.b >= 0
        assert abs(params.rho) < 1
        assert params.sigma > 0
        assert params.minimum_total_variance() >= -1e-12
        assert params.b * (1 + abs(params.rho)) <= LEE_WING_BOUND + 1e-9

    def test_the_fitted_slice_is_butterfly_free(self):
        k = np.linspace(-0.5, 0.5, 21)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.constraints_satisfied
        assert result.min_durrleman_g > 0
        assert is_butterfly_free(result.parameters)

    def test_a_convex_but_inadmissible_target_still_yields_admissible_parameters(self):
        """Constraints are enforced during the fit, not checked afterwards."""
        k = np.linspace(-0.5, 0.5, 21)
        steep = SVIParameters(a=0.05, b=1.9, rho=-0.95, m=0.0, sigma=0.05)
        result = calibrate_svi(k, raw_svi_total_variance(k, steep), TAU)
        assert result.parameters is not None
        slope = result.parameters.b * (1 + abs(result.parameters.rho))
        assert slope <= LEE_WING_BOUND + 1e-9

    def test_total_variance_stays_non_negative(self):
        k = np.linspace(-0.4, 0.4, 21)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert np.all(raw_svi_total_variance(np.linspace(-3, 3, 301), result.parameters) >= 0)


class TestNoisyQuotes:
    def test_noise_degrades_the_fit_but_not_admissibility(self):
        k = np.linspace(-0.4, 0.4, 25)
        rng = np.random.default_rng(11)
        noisy = slice_for(k) * (1 + rng.normal(0, 0.02, size=k.shape))
        result = calibrate_svi(k, noisy, TAU)
        assert result.status is CalibrationStatus.CONVERGED
        assert result.rmse_vol_points > 0.01
        assert result.constraints_satisfied

    def test_weights_move_the_fit_toward_the_trusted_quotes(self):
        k = np.linspace(-0.4, 0.4, 21)
        w = np.array(slice_for(k), dtype=float)
        corrupted = np.array(w)
        corrupted[0] *= 1.5  # one badly wrong wing quote

        weights = np.ones_like(k)
        weights[0] = 1e-6

        weighted = calibrate_svi(k, corrupted, TAU, weights)
        unweighted = calibrate_svi(k, corrupted, TAU)
        assert curve_error_vol_points(weighted.parameters, k[1:]) < (
            curve_error_vol_points(unweighted.parameters, k[1:])
        )


class TestDeterminism:
    def test_the_same_input_always_fits_identically(self):
        """A surface that refitted differently on each run could not be
        reproduced, and reproducibility is the point of storing it."""
        k = np.linspace(-0.4, 0.4, 21)
        rng = np.random.default_rng(5)
        noisy = slice_for(k) * (1 + rng.normal(0, 0.03, size=k.shape))
        first = calibrate_svi(k, noisy, TAU)
        second = calibrate_svi(k, noisy, TAU)
        assert first.parameters == second.parameters
        assert first.rmse_vol_points == second.rmse_vol_points

    def test_a_different_seed_may_differ_but_stays_admissible(self):
        k = np.linspace(-0.4, 0.4, 21)
        rng = np.random.default_rng(5)
        noisy = slice_for(k) * (1 + rng.normal(0, 0.03, size=k.shape))
        other = calibrate_svi(k, noisy, TAU, seed=999)
        assert other.constraints_satisfied

    def test_multi_start_is_reported(self):
        k = np.linspace(-0.4, 0.4, 21)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.starts_attempted >= 5
        assert 0 < result.starts_feasible <= result.starts_attempted


class TestStructuredFailures:
    def test_too_few_observations(self):
        k = np.linspace(-0.2, 0.2, MIN_OBSERVATIONS - 1)
        result = calibrate_svi(k, slice_for(k), TAU)
        assert result.status is CalibrationStatus.INSUFFICIENT_OBSERVATIONS
        assert result.parameters is None
        assert str(MIN_OBSERVATIONS) in result.error

    def test_non_positive_maturity(self):
        k = np.linspace(-0.2, 0.2, 11)
        result = calibrate_svi(k, slice_for(k), 0.0)
        assert result.status is CalibrationStatus.FAILED
        assert result.parameters is None

    def test_a_failure_never_returns_parameters(self):
        for result in (
            calibrate_svi(np.linspace(-0.2, 0.2, 3), np.full(3, 0.01), TAU),
            calibrate_svi(np.linspace(-0.2, 0.2, 11), slice_for(np.linspace(-0.2, 0.2, 11)), -1.0),
        ):
            assert result.parameters is None
            assert not result.ok

    def test_result_serialises_for_persistence(self):
        k = np.linspace(-0.4, 0.4, 21)
        payload = calibrate_svi(k, slice_for(k), TAU).to_dict()
        for key in (
            "parameters",
            "status",
            "rmse_vol_points",
            "min_durrleman_g",
            "wing_slope",
            "constraints_satisfied",
            "starts_attempted",
        ):
            assert key in payload
