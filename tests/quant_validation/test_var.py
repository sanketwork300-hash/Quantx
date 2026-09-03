"""VaR, Expected Shortfall and the simulation engine, against closed forms.

Carries the Phase 5 acceptance criteria from docs/backlog.md that VaR recovers
the analytic quantile on synthetic distributions and that Monte Carlo is
seed-reproducible.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy import stats

from quant.simulation.paths import Distribution, FactorModel, simulate_factor_returns
from quant.statistics.covariance import (
    CovarianceWarning,
    nearest_positive_semidefinite,
    sample_covariance,
)
from quant.statistics.var import (
    MIN_RELIABLE_OBSERVATIONS,
    QuantileMethod,
    VaRWarning,
    bootstrap_interval,
    historical_tail_risk,
    losses_from_pnl,
    normal_quantile,
    parametric_tail_risk,
    scale_to_horizon,
)

#: Large enough that the sampling error at the 99th percentile is well inside
#: the tolerance below, and the comparison is testing the estimator rather than
#: the luck of the draw.
LARGE_SAMPLE = 400_000
CONFIDENCES = (0.90, 0.95, 0.975, 0.99)


def analytic_expected_shortfall(confidence: float) -> float:
    """E[Z | Z > z_alpha] for a standard normal."""
    z = normal_quantile(confidence)
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi) / (1.0 - confidence)


class TestNormalQuantile:
    @pytest.mark.parametrize(
        "probability",
        [
            1e-12,
            1e-9,
            1e-6,
            0.001,
            0.01,
            # Either side of the approximation's own branch boundary at 0.02425.
            0.024,
            0.025,
            0.1,
            0.5,
            0.9,
            0.975,
            0.99,
            0.999,
            1 - 1e-6,
            1 - 1e-9,
            1 - 1e-12,
        ],
    )
    def test_matches_scipy_to_machine_precision(self, probability):
        """The rational approximation plus one Halley step, against the oracle."""
        assert normal_quantile(probability) == pytest.approx(
            float(stats.norm.ppf(probability)), rel=1e-12, abs=1e-12
        )

    def test_is_the_inverse_of_the_cdf(self):
        for probability in (0.005, 0.05, 0.5, 0.95, 0.995):
            x = normal_quantile(probability)
            assert 0.5 * math.erfc(-x / math.sqrt(2.0)) == pytest.approx(probability, abs=1e-14)

    @pytest.mark.parametrize("probability", [0.0, 1.0, -0.1, 1.1])
    def test_a_probability_outside_the_open_unit_interval_is_refused(self, probability):
        with pytest.raises(ValueError):
            normal_quantile(probability)


class TestHistoricalRecoversTheAnalyticQuantile:
    """Phase 5 acceptance: the empirical estimator converges to the truth."""

    @pytest.fixture(scope="class")
    @classmethod
    def standard_normal_losses(cls):
        return np.random.default_rng(20_260_924).normal(0.0, 1.0, LARGE_SAMPLE)

    @pytest.mark.parametrize("confidence", CONFIDENCES)
    def test_var_recovers_the_normal_quantile(self, standard_normal_losses, confidence):
        result = historical_tail_risk(standard_normal_losses, confidence)
        # Three standard errors of the sample quantile, which is the honest
        # tolerance for an estimator whose error is sampling noise.
        density = math.exp(-0.5 * normal_quantile(confidence) ** 2) / math.sqrt(2 * math.pi)
        standard_error = math.sqrt(confidence * (1 - confidence) / LARGE_SAMPLE) / density
        assert result.value_at_risk == pytest.approx(
            normal_quantile(confidence), abs=3 * standard_error
        )

    @pytest.mark.parametrize("confidence", CONFIDENCES)
    def test_expected_shortfall_recovers_its_analytic_value(
        self, standard_normal_losses, confidence
    ):
        result = historical_tail_risk(standard_normal_losses, confidence)
        assert result.expected_shortfall == pytest.approx(
            analytic_expected_shortfall(confidence), rel=0.02
        )

    def test_a_shifted_and_scaled_sample_shifts_and_scales_the_answer(self, standard_normal_losses):
        base = historical_tail_risk(standard_normal_losses, 0.99)
        moved = historical_tail_risk(3.0 + 2.0 * standard_normal_losses, 0.99)
        assert moved.value_at_risk == pytest.approx(3.0 + 2.0 * base.value_at_risk, rel=1e-12)

    @pytest.mark.parametrize("confidence", CONFIDENCES)
    def test_the_uniform_case_is_exact(self, confidence):
        """A uniform sample has a quantile with no approximation in it."""
        sample = np.linspace(0.0, 1.0, 100_001)
        result = historical_tail_risk(sample, confidence)
        assert result.value_at_risk == pytest.approx(confidence, abs=1e-9)


class TestParametricMatchesTheClosedForm:
    @pytest.mark.parametrize("confidence", CONFIDENCES)
    def test_var_is_the_normal_quantile(self, confidence):
        result = parametric_tail_risk(0.0, 1.0, confidence, observations=500)
        assert result.value_at_risk == pytest.approx(normal_quantile(confidence), rel=1e-12)
        assert result.quantile_method is QuantileMethod.NORMAL_ANALYTIC

    @pytest.mark.parametrize("confidence", CONFIDENCES)
    def test_expected_shortfall_is_the_closed_form(self, confidence):
        result = parametric_tail_risk(0.0, 1.0, confidence, observations=500)
        assert result.expected_shortfall == pytest.approx(
            analytic_expected_shortfall(confidence), rel=1e-12
        )

    @pytest.mark.parametrize("confidence", CONFIDENCES)
    def test_it_agrees_with_the_historical_estimator_on_a_normal_sample(self, confidence):
        """The two methods only agree when the sample really is normal.

        That they do here is the check that the parametric branch is right; that
        they disagree on an option book is the reason both are offered.
        """
        sample = np.random.default_rng(4).normal(0.0, 1.0, LARGE_SAMPLE)
        empirical = historical_tail_risk(sample, confidence)
        analytic = parametric_tail_risk(
            float(sample.mean()), float(sample.std(ddof=1)), confidence, sample.size
        )
        assert empirical.value_at_risk == pytest.approx(analytic.value_at_risk, rel=0.02)


class TestWhatTheEstimatorsRefuseToHide:
    def test_expected_shortfall_is_never_below_value_at_risk(self):
        for confidence in CONFIDENCES:
            sample = np.random.default_rng(9).standard_t(4, 5_000)
            result = historical_tail_risk(sample, confidence)
            assert result.expected_shortfall >= result.value_at_risk

    def test_a_thin_sample_is_flagged_rather_than_refused(self):
        result = historical_tail_risk(np.random.default_rng(2).normal(size=30), 0.99)
        assert VaRWarning.THIN_SAMPLE in result.warnings
        assert result.is_reliable is False
        assert result.observations == 30

    def test_a_sample_at_the_reliability_threshold_is_not_flagged(self):
        sample = np.random.default_rng(2).normal(size=MIN_RELIABLE_OBSERVATIONS)
        assert VaRWarning.THIN_SAMPLE not in historical_tail_risk(sample, 0.90).warnings

    def test_a_constant_sample_is_reported_as_degenerate(self):
        result = historical_tail_risk(np.full(500, 7.0), 0.99)
        assert VaRWarning.DEGENERATE_SAMPLE in result.warnings
        assert result.value_at_risk == pytest.approx(7.0)
        assert result.expected_shortfall == pytest.approx(7.0)

    def test_the_tail_observation_count_travels_with_the_answer(self):
        result = historical_tail_risk(np.random.default_rng(5).normal(size=1_000), 0.99)
        assert 5 <= result.tail_observations <= 20

    def test_the_loss_convention_is_applied_in_exactly_one_place(self):
        pnl = np.array([-5.0, 1.0, 3.0])
        assert list(losses_from_pnl(pnl)) == [5.0, -1.0, -3.0]

    @pytest.mark.parametrize("confidence", [0.0, 1.0, -0.5, 2.0])
    def test_an_impossible_confidence_is_refused(self, confidence):
        with pytest.raises(ValueError):
            historical_tail_risk(np.arange(10.0), confidence)

    def test_an_empty_sample_is_refused_rather_than_returning_zero(self):
        with pytest.raises(ValueError):
            historical_tail_risk(np.empty(0), 0.95)


class TestBootstrapInterval:
    def test_the_interval_brackets_the_point_estimate(self):
        sample = np.random.default_rng(6).normal(size=250)
        point = historical_tail_risk(sample, 0.95).value_at_risk
        low, high = bootstrap_interval(sample, 0.95, seed=1)
        assert low <= point <= high

    def test_it_narrows_as_the_sample_grows(self):
        rng = np.random.default_rng(7)
        narrow = bootstrap_interval(rng.normal(size=20_000), 0.95, seed=1)
        wide = bootstrap_interval(rng.normal(size=200), 0.95, seed=1)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_it_is_reproducible_from_its_seed(self):
        sample = np.random.default_rng(8).normal(size=300)
        assert bootstrap_interval(sample, 0.99, seed=3) == bootstrap_interval(sample, 0.99, seed=3)


class TestHorizonScaling:
    def test_square_root_of_time(self):
        assert scale_to_horizon(10.0, 1, 4) == pytest.approx(20.0)
        assert scale_to_horizon(10.0, 4, 1) == pytest.approx(5.0)

    @pytest.mark.parametrize(("start", "end"), [(0, 1), (1, 0), (-1, 1)])
    def test_a_non_positive_horizon_is_refused(self, start, end):
        with pytest.raises(ValueError):
            scale_to_horizon(1.0, start, end)


class TestCovariance:
    def test_it_recovers_the_generating_covariance(self):
        truth = np.array([[0.0004, 0.00016], [0.00016, 0.0009]])
        draws = np.random.default_rng(12).multivariate_normal([0.0, 0.0], truth, 200_000)
        estimate = sample_covariance(("a", "b"), draws)
        assert estimate.covariance == pytest.approx(truth, rel=0.03)
        assert estimate.correlation()[0, 1] == pytest.approx(
            truth[0, 1] / math.sqrt(truth[0, 0] * truth[1, 1]), rel=0.03
        )

    def test_a_short_sample_is_flagged(self):
        draws = np.random.default_rng(13).normal(size=(15, 2))
        assert CovarianceWarning.FEW_OBSERVATIONS in sample_covariance(("a", "b"), draws).warnings

    def test_more_factors_than_observations_is_flagged_as_rank_deficient(self):
        draws = np.random.default_rng(14).normal(size=(3, 5))
        estimate = sample_covariance(tuple("abcde"), draws)
        assert CovarianceWarning.RANK_DEFICIENT in estimate.warnings

    def test_a_repair_reports_that_it_happened(self):
        indefinite = np.array([[1.0, 2.0], [2.0, 1.0]])
        repaired, was_repaired = nearest_positive_semidefinite(indefinite)
        assert was_repaired is True
        assert float(np.linalg.eigvalsh(repaired).min()) >= -1e-12

    def test_a_valid_matrix_is_left_alone(self):
        valid = np.array([[1.0, 0.2], [0.2, 1.0]])
        repaired, was_repaired = nearest_positive_semidefinite(valid)
        assert was_repaired is False
        assert repaired == pytest.approx(valid)


class TestSimulation:
    @pytest.fixture
    def model(self):
        return FactorModel(
            factors=("a", "b"),
            mean=np.zeros(2),
            covariance=np.array([[0.0004, 0.00016], [0.00016, 0.0009]]),
        )

    def test_the_same_seed_reproduces_the_draws_exactly(self, model):
        """Phase 5 acceptance: a Monte Carlo answer can be recomputed later."""
        first = simulate_factor_returns(model, paths=5_000, seed=99)
        second = simulate_factor_returns(model, paths=5_000, seed=99)
        assert np.array_equal(first.draws, second.draws)

    def test_a_different_seed_gives_different_draws(self, model):
        first = simulate_factor_returns(model, paths=5_000, seed=99)
        second = simulate_factor_returns(model, paths=5_000, seed=100)
        assert not np.array_equal(first.draws, second.draws)

    def test_the_draws_have_the_covariance_they_were_given(self, model):
        draws = simulate_factor_returns(model, paths=200_000, seed=1).draws
        assert np.cov(draws, rowvar=False) == pytest.approx(model.covariance, rel=0.03)

    def test_antithetic_variates_zero_the_sample_mean_exactly(self, model):
        draws = simulate_factor_returns(model, paths=10_000, seed=2).draws
        assert float(np.abs(draws.mean(axis=0)).max()) < 1e-15

    def test_an_odd_path_count_is_rounded_up_not_silently_halved(self, model):
        assert simulate_factor_returns(model, paths=1_001, seed=2).paths == 1_002

    def test_student_t_keeps_the_variance_but_fattens_the_tails(self, model):
        heavy = FactorModel(
            factors=model.factors,
            mean=model.mean,
            covariance=model.covariance,
            distribution=Distribution.STUDENT_T,
            degrees_of_freedom=5.0,
        )
        draws = simulate_factor_returns(heavy, paths=400_000, seed=3).draws
        assert np.cov(draws, rowvar=False) == pytest.approx(model.covariance, rel=0.10)

        standardised = (draws[:, 0] - draws[:, 0].mean()) / draws[:, 0].std()
        assert float((standardised**4).mean()) > 4.0

    def test_student_t_without_a_variance_is_refused(self, model):
        heavy = FactorModel(
            factors=model.factors,
            mean=model.mean,
            covariance=model.covariance,
            distribution=Distribution.STUDENT_T,
            degrees_of_freedom=2.0,
        )
        with pytest.raises(ValueError):
            simulate_factor_returns(heavy, paths=100, seed=1)

    def test_a_singular_covariance_still_simulates(self, model):
        """One factor a copy of another must not break the factorisation."""
        singular = FactorModel(
            factors=("a", "b"),
            mean=np.zeros(2),
            covariance=np.array([[0.01, 0.01], [0.01, 0.01]]),
        )
        draws = simulate_factor_returns(singular, paths=2_000, seed=4).draws
        assert draws[:, 0] == pytest.approx(draws[:, 1], abs=1e-12)

    @given(
        paths=st.integers(min_value=2, max_value=2_000),
        seed=st.integers(min_value=0, max_value=10_000),
    )
    @settings(max_examples=25, deadline=None)
    def test_every_run_is_reproducible(self, paths, seed):
        model = FactorModel(factors=("a",), mean=np.zeros(1), covariance=np.array([[0.01]]))
        first = simulate_factor_returns(model, paths=paths, seed=seed)
        second = simulate_factor_returns(model, paths=paths, seed=seed)
        assert np.array_equal(first.draws, second.draws)
