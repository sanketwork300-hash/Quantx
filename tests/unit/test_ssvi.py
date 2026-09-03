"""SSVI, its calibration, and the Dupire surface derived from it."""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant.volatility.local_vol import (
    IMPLAUSIBLE_LOCAL_VOL,
    LocalVolFlag,
    SurfaceLocalVol,
    dupire_denominator,
    local_volatility_point,
    local_volatility_surface,
)
from quant.volatility.ssvi import (
    BUTTERFLY_BOUND,
    SSVIError,
    SSVIParameters,
    SSVISurface,
    ThetaTermStructure,
    butterfly_bounds,
    min_durrleman_g,
    satisfies_butterfly_bounds,
    ssvi_derivatives,
    ssvi_total_variance,
)
from quant.volatility.ssvi_calibration import (
    SSVISliceObservations,
    calibrate_ssvi,
)
from quant.volatility.svi_calibration import CalibrationStatus

PARAMETERS = SSVIParameters(rho=-0.6, eta=1.0, gamma=0.45)
TERM = ThetaTermStructure(maturities=(0.1, 0.5, 1.0, 2.0), thetas=(0.004, 0.02, 0.04, 0.08))
SURFACE = SSVISurface(parameters=PARAMETERS, term_structure=TERM)


class TestParameters:
    @pytest.mark.parametrize(
        "rho,eta,gamma",
        [(-1.0, 1.0, 0.5), (1.0, 1.0, 0.5), (0.0, 0.0, 0.5), (0.0, 1.0, 0.0), (0.0, 1.0, 1.0)],
    )
    def test_an_inadmissible_parameter_set_is_refused_at_construction(self, rho, eta, gamma):
        with pytest.raises(SSVIError):
            SSVIParameters(rho=rho, eta=eta, gamma=gamma)

    def test_phi_stays_bounded_as_maturity_grows(self):
        """The (1 + theta) factor in the power law is what keeps theta*phi
        bounded, which is what keeps the butterfly condition satisfiable at the
        long end rather than only near the front."""
        thetas = np.array([1e-4, 1e-2, 1.0, 10.0, 100.0])
        products = thetas * PARAMETERS.phi(thetas)
        assert np.all(np.isfinite(products))
        assert products[-1] < 10.0


class TestSurfaceShape:
    def test_total_variance_at_the_money_is_exactly_theta(self):
        for theta in (0.004, 0.02, 0.04, 0.08):
            assert float(ssvi_total_variance(0.0, theta, PARAMETERS)) == pytest.approx(
                theta, rel=0, abs=1e-15
            )

    @pytest.mark.parametrize("theta", [0.004, 0.04, 0.16])
    def test_the_analytic_derivatives_match_finite_differences(self, theta):
        k = np.linspace(-0.8, 0.8, 17)
        _w, dw, d2w = ssvi_derivatives(k, theta, PARAMETERS)

        # The second difference is squeezed from both sides: dividing by h^2
        # amplifies cancellation at a small step, and a short-dated slice has a
        # sharply peaked curvature that a large step truncates. Neither single
        # step size resolves it to better than 1e-4, so the reference is
        # Richardson-extrapolated to fourth order — which measures the formula
        # rather than the arithmetic around it.
        def second_difference(h: float) -> np.ndarray:
            return (
                ssvi_total_variance(k + h, theta, PARAMETERS)
                - 2 * ssvi_total_variance(k, theta, PARAMETERS)
                + ssvi_total_variance(k - h, theta, PARAMETERS)
            ) / h**2

        h = 1e-5
        first = (
            ssvi_total_variance(k + h, theta, PARAMETERS)
            - ssvi_total_variance(k - h, theta, PARAMETERS)
        ) / (2 * h)
        coarse, fine = 2e-3, 1e-3
        second = (4.0 * second_difference(fine) - second_difference(coarse)) / 3.0

        # The absolute tolerance matters for the first: dw/dk crosses zero at
        # the smile's minimum, and a purely relative comparison there compares
        # two roundings.
        np.testing.assert_allclose(dw, first, rtol=1e-6, atol=1e-10)
        np.testing.assert_allclose(d2w, second, rtol=1e-7, atol=1e-10)

    def test_total_variance_is_positive_everywhere(self):
        k = np.linspace(-3.0, 3.0, 201)
        assert np.all(np.asarray(SURFACE.total_variance(k, 1.0)) > 0)

    def test_a_negative_correlation_produces_a_downward_skew(self):
        vols = np.asarray(SURFACE.implied_volatility(np.array([-0.2, 0.0, 0.2]), 1.0))
        assert vols[0] > vols[1] > vols[2]


class TestTermStructure:
    def test_maturities_must_be_ordered_and_positive(self):
        with pytest.raises(SSVIError):
            ThetaTermStructure(maturities=(1.0, 0.5), thetas=(0.04, 0.02))
        with pytest.raises(SSVIError):
            ThetaTermStructure(maturities=(0.0, 1.0), thetas=(0.04, 0.08))
        with pytest.raises(SSVIError):
            ThetaTermStructure(maturities=(0.5, 1.0), thetas=(0.0, 0.08))

    def test_variance_runs_to_zero_at_the_origin(self):
        """Before the first expiry total variance is proportional to maturity,
        because a zero-length period has zero variance. Clamping flat instead
        would make dtheta/dT zero across the whole front, and Dupire divides
        into that."""
        assert float(TERM.theta(0.05)) == pytest.approx(0.002)
        assert float(TERM.theta_derivative(0.05)) == pytest.approx(0.04)
        assert float(TERM.theta(1e-9)) < 1e-9

    def test_variance_is_flat_after_the_last_expiry(self):
        """A refusal to extrapolate: continuing the slope past the observed
        range would invent a term structure. Flat cannot introduce calendar
        arbitrage, and the lookup is flagged either way."""
        assert float(TERM.theta(5.0)) == pytest.approx(TERM.thetas[-1])
        assert float(TERM.theta_derivative(5.0)) == 0.0
        assert TERM.is_extrapolated(5.0)
        assert TERM.is_extrapolated(0.05)
        assert not TERM.is_extrapolated(0.5)

    def test_a_single_expiry_reads_as_flat_implied_volatility(self):
        single = ThetaTermStructure(maturities=(0.5,), thetas=(0.02,))
        assert float(single.theta(1.0)) == pytest.approx(0.04)
        assert float(single.theta(0.25)) == pytest.approx(0.01)

    def test_monotone_interpolation_cannot_dip_between_knots(self):
        grid = np.linspace(0.1, 2.0, 401)
        values = np.asarray(TERM.theta(grid))
        assert np.all(np.diff(values) >= -1e-12)


class TestArbitrageConditions:
    def test_the_two_butterfly_quantities_are_reported_separately(self):
        first, second = butterfly_bounds(0.04, PARAMETERS)
        assert first > 0 and second > 0
        assert satisfies_butterfly_bounds(0.04, PARAMETERS) == (
            first < BUTTERFLY_BOUND and second <= BUTTERFLY_BOUND
        )

    def test_durrleman_is_evaluated_as_well_as_the_closed_form_bounds(self):
        """The bounds are sufficient, not necessary. A surface can fail them and
        still have a non-negative density, so both are computed and reported."""
        steep = SSVIParameters(rho=-0.9, eta=4.0, gamma=0.1)
        assert not satisfies_butterfly_bounds(0.5, steep)
        # The sufficient condition failing says nothing on its own; the actual
        # condition is the one below, and it is a number, not a verdict.
        assert isinstance(min_durrleman_g(0.5, steep), float)

    def test_a_monotone_term_structure_is_calendar_arbitrage_free(self):
        assert TERM.is_monotone
        assert SURFACE.is_arbitrage_free([0.1, 0.5, 1.0, 2.0])

    def test_flags_name_every_problem_at_a_maturity(self):
        assert "SSVI_EXTRAPOLATED_MATURITY" in SURFACE.flags(5.0)
        assert SURFACE.flags(1.0) == ()


class TestCalibration:
    @staticmethod
    def _slices(truth: SSVIParameters, thetas, maturities):
        return [
            SSVISliceObservations(
                maturity=tau,
                log_moneyness=np.linspace(-0.35, 0.30, 21),
                total_variance=np.asarray(
                    ssvi_total_variance(np.linspace(-0.35, 0.30, 21), theta, truth)
                ),
                label=f"T={tau}",
            )
            for tau, theta in zip(maturities, thetas, strict=True)
        ]

    def test_known_parameters_are_recovered(self):
        truth = SSVIParameters(rho=-0.65, eta=1.2, gamma=0.45)
        maturities = [0.08, 0.25, 0.5, 1.0]
        thetas = [0.04 * t for t in maturities]
        result = calibrate_ssvi(self._slices(truth, thetas, maturities))

        assert result.status is CalibrationStatus.CONVERGED
        assert result.parameters.rho == pytest.approx(truth.rho, abs=1e-3)
        assert result.parameters.eta == pytest.approx(truth.eta, abs=1e-3)
        assert result.parameters.gamma == pytest.approx(truth.gamma, abs=1e-3)
        assert result.rmse_vol_points < 0.01
        assert result.calendar_arbitrage_free
        assert result.term_structure.thetas == pytest.approx(thetas, abs=1e-6)

    def test_the_calibration_is_reproducible(self):
        truth = SSVIParameters(rho=-0.5, eta=0.8, gamma=0.5)
        maturities = [0.1, 0.5, 1.0]
        thetas = [0.03 * t for t in maturities]
        slices = self._slices(truth, thetas, maturities)
        assert (
            calibrate_ssvi(slices, seed=11).parameters == calibrate_ssvi(slices, seed=11).parameters
        )

    def test_an_inverted_observed_term_structure_is_made_monotone(self):
        """The fit imposes non-decreasing variance, which is the calendar
        condition. The surface therefore will not reproduce an inversion in the
        raw quotes — that inversion is arbitrage in the market, and the raw
        arbitrage report is where it is named."""
        truth = SSVIParameters(rho=-0.5, eta=1.0, gamma=0.5)
        maturities = [0.25, 0.5, 1.0]
        thetas = [0.02, 0.01, 0.05]  # the middle expiry is inverted
        result = calibrate_ssvi(self._slices(truth, thetas, maturities))
        assert result.parameters is not None
        fitted = list(result.term_structure.thetas)
        assert fitted == sorted(fitted)
        assert result.calendar_arbitrage_free

    def test_too_few_observations_is_reported_rather_than_fitted(self):
        result = calibrate_ssvi(
            [
                SSVISliceObservations(
                    maturity=1.0,
                    log_moneyness=np.array([-0.1, 0.0]),
                    total_variance=np.array([0.045, 0.04]),
                )
            ]
        )
        assert result.status is CalibrationStatus.INSUFFICIENT_OBSERVATIONS
        assert result.parameters is None

    def test_a_repeated_maturity_is_refused(self):
        truth = SSVIParameters(rho=-0.5, eta=1.0, gamma=0.5)
        slices = self._slices(truth, [0.02, 0.02], [1.0, 1.0])
        result = calibrate_ssvi(slices)
        assert result.status is CalibrationStatus.FAILED
        assert "maturity" in (result.error or "")

    def test_the_fitted_surface_is_butterfly_free_where_it_was_constrained(self):
        truth = SSVIParameters(rho=-0.7, eta=1.5, gamma=0.4)
        maturities = [0.1, 0.5, 1.0]
        thetas = [0.05 * t for t in maturities]
        result = calibrate_ssvi(self._slices(truth, thetas, maturities))
        assert result.min_durrleman_g >= -1e-9
        for slice_ in result.slices:
            assert slice_.n_observations == 21
            assert slice_.theta > 0


class TestLocalVolatility:
    def test_a_flat_surface_gives_back_its_own_volatility(self):
        """The one case where Dupire has a closed-form answer: with no smile and
        no term structure the local volatility *is* the implied volatility."""
        flat = SSVISurface(
            parameters=SSVIParameters(rho=0.0, eta=1e-4, gamma=0.5),
            term_structure=ThetaTermStructure(maturities=(0.5, 1.0), thetas=(0.02, 0.04)),
        )
        point = local_volatility_point(flat, 0.0, 1.0)
        assert point.local_volatility == pytest.approx(0.2, abs=1e-6)
        assert point.denominator == pytest.approx(1.0, abs=1e-6)
        assert point.flags == ()

    def test_the_denominator_is_one_when_the_smile_is_flat(self):
        assert dupire_denominator(0.3, 0.04, 0.0, 0.0) == pytest.approx(1.0)

    def test_an_invalid_point_is_a_hole_with_a_reason(self):
        surface = local_volatility_surface(SURFACE, np.linspace(-1.0, 1.0, 21), [0.5, 1.0])
        grid = surface.grid()
        assert grid.shape == (2, 21)
        for point in surface.points:
            assert (point.local_volatility is None) == (not point.is_valid)
            if not point.is_valid:
                assert point.flags, "a hole without a reason is a silent drop"
        assert len(surface.points) == len(surface.valid) + sum(
            1 for p in surface.points if not p.is_valid
        )

    def test_an_implausible_magnitude_is_flagged_not_clipped(self):
        steep = SSVISurface(
            parameters=SSVIParameters(rho=-0.95, eta=8.0, gamma=0.05),
            term_structure=ThetaTermStructure(maturities=(0.02, 1.0), thetas=(1e-4, 2.0)),
        )
        points = local_volatility_surface(steep, np.linspace(-2.0, 2.0, 41), [0.02, 0.5, 1.0])
        flagged = [p for p in points.points if LocalVolFlag.IMPLAUSIBLE_MAGNITUDE in p.flags]
        for point in flagged:
            assert point.local_volatility is None or point.local_volatility > IMPLAUSIBLE_LOCAL_VOL

    def test_the_pde_coefficient_uses_the_forward_to_each_time(self):
        """The surface is parameterised in log-moneyness against the forward to
        *that* maturity, so evaluating it at an intermediate time needs the
        forward to that time. A single fixed forward shifts every lookup along
        the smile by the carry."""
        local = SurfaceLocalVol(surface=SURFACE, spot=100.0, carry=0.05, fallback=0.2)
        assert local.forward(0.0) == pytest.approx(100.0)
        assert local.forward(1.0) == pytest.approx(100.0 * math.exp(0.05))
        near = local(np.array([100.0]), 0.01)
        far = local(np.array([100.0]), 1.0)
        assert near[0] != far[0]

    def test_the_fallback_share_travels_with_the_result(self):
        local = SurfaceLocalVol(surface=SURFACE, spot=100.0, carry=0.02, fallback=0.2)
        share = local.fallback_fraction(np.linspace(60.0, 160.0, 51), 0.5)
        assert 0.0 <= share <= 1.0
