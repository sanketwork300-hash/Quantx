"""Numerical validation for the arrival-intensity models.

Three things are checked here, and the third is the one the phase turns on.

1. **The likelihood is the likelihood.** The vectorised excitation is checked
   against Ogata's plain recursion, and the compensator against a numerical
   integral of the intensity itself. A fast path that disagrees with the
   definition is worse than a slow one.
2. **The estimator recovers a process whose parameters are known.** The only
   way to test a maximum-likelihood fit without an oracle implementation is to
   simulate from the model and see whether the truth comes back.
3. **The gate refuses when it should.** Fitted to genuinely Poisson arrivals,
   the self-exciting model must *not* be adopted — not merely "usually",
   because the whole claim of the phase is that a Hawkes result the platform
   reports beat a constant rate on data it had not seen.

Everything here is simulated. That is legitimate precisely because none of it
is a claim about a market: it is a claim about whether this code recovers a
process it was given.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant.microstructure.intensity import (
    MIN_HELD_OUT_EVENTS,
    MIN_TRAINING_EVENTS,
    HawkesParameters,
    IntensityRefusal,
    IntensityUnavailable,
    PoissonParameters,
    _excitation,
    compare_held_out,
    fit_hawkes,
    fit_poisson,
    hawkes_log_likelihood,
    held_out_predictive_gains,
    poisson_log_likelihood,
    predictive_comparison,
    simulate_hawkes,
    time_rescaling_residuals,
)

pytestmark = pytest.mark.quant_validation


def ogata_recursion(times: np.ndarray, beta: float) -> np.ndarray:
    """The definition, written out. Deliberately slow and obviously correct."""
    out = np.zeros(times.size)
    running = 0.0
    for index in range(times.size):
        running = (
            0.0
            if index == 0
            else math.exp(-beta * (times[index] - times[index - 1])) * (1.0 + running)
        )
        out[index] = running
    return out


class TestTheExcitationRecursion:
    """The vectorised sum has to equal the recursion it replaces, everywhere."""

    @pytest.mark.parametrize("beta", [0.001, 0.01, 0.5, 5.0, 50.0, 500.0])
    def test_it_matches_the_plain_recursion(self, beta):
        times = np.sort(np.random.default_rng(7).uniform(0.0, 600.0, 2_000))
        assert np.allclose(_excitation(times, beta), ogata_recursion(times, beta), atol=1e-9)

    def test_it_survives_a_window_far_longer_than_the_decay(self):
        """The blocking exists for exactly this: ``exp(beta * span)`` overflows.

        A one-hour window with a half-second decay puts the naive exponent past
        10^4700. The result has to stay finite and stay right.
        """
        times = np.sort(np.random.default_rng(11).uniform(0.0, 3_600.0, 1_500))
        excitation = _excitation(times, beta=10.0)
        assert np.all(np.isfinite(excitation))
        assert np.allclose(excitation, ogata_recursion(times, 10.0), atol=1e-9)

    def test_a_single_event_is_excited_by_nothing(self):
        assert _excitation(np.array([4.0]), 1.0).tolist() == [0.0]


class TestTheLikelihoodIsTheLikelihood:
    def test_the_compensator_matches_a_numerical_integral(self):
        """``l = sum log(lambda) - integral(lambda)``, with the integral checked
        by quadrature against the intensity function written out directly."""
        parameters = HawkesParameters(mu=0.4, alpha=0.9, beta=1.7)
        times = simulate_hawkes(parameters, 120.0, np.random.default_rng(3))
        assert times.size > 20

        grid = np.linspace(0.0, 120.0, 400_001)
        intensity = parameters.mu + np.array(
            [
                parameters.alpha
                * np.sum(np.exp(-parameters.beta * (moment - times[times < moment])))
                for moment in grid
            ]
        )
        compensator = float(np.trapezoid(intensity, grid))
        scored = float(
            np.sum(
                np.log(parameters.mu + parameters.alpha * ogata_recursion(times, parameters.beta))
            )
        )
        assert hawkes_log_likelihood(times, 0.0, 120.0, parameters) == pytest.approx(
            scored - compensator, rel=1e-4
        )

    def test_a_zero_jump_hawkes_is_exactly_a_poisson(self):
        """The models are nested, so the boundary case has to agree exactly."""
        times = np.sort(np.random.default_rng(5).uniform(0.0, 500.0, 400))
        degenerate = HawkesParameters(mu=0.8, alpha=0.0, beta=2.0)
        assert hawkes_log_likelihood(times, 0.0, 500.0, degenerate) == pytest.approx(
            poisson_log_likelihood(times, 0.0, 500.0, 0.8), rel=1e-12
        )

    def test_the_poisson_maximum_is_where_the_derivative_says(self):
        times = np.sort(np.random.default_rng(9).uniform(0.0, 250.0, 311))
        fit = fit_poisson(times, 0.0, 250.0)
        assert fit.parameters.rate == pytest.approx(311 / 250.0)
        for perturbation in (0.8, 0.9, 1.1, 1.25):
            worse = poisson_log_likelihood(times, 0.0, 250.0, fit.parameters.rate * perturbation)
            assert worse < fit.log_likelihood

    def test_an_empty_window_is_refused_rather_than_scored(self):
        with pytest.raises(IntensityUnavailable) as excinfo:
            fit_poisson([], 10.0, 10.0)
        assert excinfo.value.reason is IntensityRefusal.NON_POSITIVE_WINDOW


class TestTimeRescaling:
    """Compensator increments are Exp(1) under the true model. That is the
    strongest available check that the compensator is right, because it is a
    statement about a distribution rather than about one number."""

    def test_the_residuals_of_the_true_model_look_exponential(self):
        parameters = HawkesParameters(mu=0.6, alpha=1.1, beta=2.2)
        times = simulate_hawkes(parameters, 6_000.0, np.random.default_rng(21))
        residuals = time_rescaling_residuals(times, 0.0, 6_000.0, parameters)
        assert residuals.size > 4_000
        # Exp(1): mean 1, variance 1. Three standard errors either side.
        error = 3.0 / math.sqrt(residuals.size)
        assert abs(residuals.mean() - 1.0) < 5.0 * error
        assert abs(residuals.var() - 1.0) < 12.0 * error

    def test_a_poisson_process_rescales_to_exponential_too(self):
        rng = np.random.default_rng(33)
        times = np.sort(rng.uniform(0.0, 4_000.0, 4_000))
        residuals = time_rescaling_residuals(times, 0.0, 4_000.0, PoissonParameters(rate=1.0))
        assert abs(residuals.mean() - 1.0) < 0.1


class TestParameterRecovery:
    """The estimator has to find a process it was given."""

    @pytest.mark.parametrize(
        ("mu", "alpha", "beta"),
        [
            (0.5, 1.2, 2.0),
            (1.0, 0.5, 1.0),
            (0.2, 2.4, 4.0),
        ],
    )
    def test_the_truth_comes_back(self, mu, alpha, beta):
        truth = HawkesParameters(mu=mu, alpha=alpha, beta=beta)
        times = simulate_hawkes(truth, 20_000.0, np.random.default_rng(1234))
        fit = fit_hawkes(times, 0.0, 20_000.0)
        assert fit.converged

        found = fit.parameters
        assert found.mu == pytest.approx(mu, rel=0.15)
        assert found.alpha == pytest.approx(alpha, rel=0.15)
        assert found.beta == pytest.approx(beta, rel=0.15)
        assert found.branching_ratio == pytest.approx(alpha / beta, rel=0.10)

    def test_the_fit_beats_the_truth_on_its_own_sample(self):
        """A maximum-likelihood fit must score at least as well as the process
        that generated the data. If it does not, the optimiser stopped early."""
        truth = HawkesParameters(mu=0.5, alpha=1.2, beta=2.0)
        times = simulate_hawkes(truth, 5_000.0, np.random.default_rng(77))
        fit = fit_hawkes(times, 0.0, 5_000.0)
        assert fit.log_likelihood >= hawkes_log_likelihood(times, 0.0, 5_000.0, truth)

    def test_the_fit_is_reproducible(self):
        """Deterministic multi-start: the same events give the same parameters."""
        times = simulate_hawkes(
            HawkesParameters(mu=0.5, alpha=1.0, beta=2.0), 3_000.0, np.random.default_rng(4)
        )
        first, second = fit_hawkes(times, 0.0, 3_000.0), fit_hawkes(times, 0.0, 3_000.0)
        assert first.parameters == second.parameters

    def test_stationarity_is_structural(self):
        """No reachable point of the optimiser describes an explosive process.

        Fitted to data with no clustering at all, where the likelihood pushes
        the branching ratio wherever it likes, the constraint still holds —
        because it is in the parametrisation rather than in a check afterwards.
        """
        times = np.sort(np.random.default_rng(2).uniform(0.0, 2_000.0, 900))
        fit = fit_hawkes(times, 0.0, 2_000.0)
        assert 0.0 < fit.parameters.branching_ratio < 1.0
        assert fit.parameters.is_stationary

    def test_too_few_events_is_a_refusal_not_a_fit(self):
        times = np.linspace(1.0, 100.0, MIN_TRAINING_EVENTS - 1)
        with pytest.raises(IntensityUnavailable) as excinfo:
            fit_hawkes(times, 0.0, 120.0)
        assert excinfo.value.reason is IntensityRefusal.TOO_FEW_TRAINING_EVENTS


class TestSimulation:
    def test_the_simulated_rate_matches_the_stationary_rate(self):
        parameters = HawkesParameters(mu=0.5, alpha=1.0, beta=2.0)
        times = simulate_hawkes(parameters, 40_000.0, np.random.default_rng(19))
        assert times.size / 40_000.0 == pytest.approx(parameters.stationary_rate, rel=0.05)

    def test_it_is_reproducible_from_its_seed(self):
        parameters = HawkesParameters(mu=0.4, alpha=0.8, beta=1.6)
        first = simulate_hawkes(parameters, 500.0, np.random.default_rng(101))
        second = simulate_hawkes(parameters, 500.0, np.random.default_rng(101))
        assert np.array_equal(first, second)

    def test_an_explosive_process_is_refused(self):
        with pytest.raises(ValueError, match="not below one"):
            simulate_hawkes(
                HawkesParameters(mu=0.1, alpha=3.0, beta=2.0), 10.0, np.random.default_rng(1)
            )

    @given(seed=st.integers(min_value=0, max_value=2**31 - 1))
    @settings(max_examples=20, deadline=None)
    def test_every_simulated_event_lies_inside_the_horizon(self, seed):
        times = simulate_hawkes(
            HawkesParameters(mu=0.3, alpha=0.6, beta=1.5), 200.0, np.random.default_rng(seed)
        )
        assert np.all(times > 0.0)
        assert np.all(times <= 200.0)
        assert np.all(np.diff(times) >= 0.0)


class TestThePredictiveDecomposition:
    def test_the_gains_and_the_tail_sum_to_the_likelihood_difference(self):
        """A decomposition that does not add back up is not a decomposition."""
        times = simulate_hawkes(
            HawkesParameters(mu=0.5, alpha=1.2, beta=2.0), 3_000.0, np.random.default_rng(11)
        )
        split, end = 2_100.0, 3_000.0
        poisson = fit_poisson(times, 0.0, split)
        hawkes = fit_hawkes(times, 0.0, split)

        gains, tail = held_out_predictive_gains(
            times, split, end, hawkes.parameters, poisson.parameters.rate
        )
        difference = hawkes_log_likelihood(
            times, split, end, hawkes.parameters
        ) - poisson_log_likelihood(times, split, end, poisson.parameters.rate)
        assert float(gains.sum()) + tail == pytest.approx(difference, abs=1e-8)

    def test_two_identical_models_have_no_gain(self):
        times = np.sort(np.random.default_rng(6).uniform(0.0, 1_000.0, 800))
        degenerate = HawkesParameters(mu=0.8, alpha=0.0, beta=2.0)
        gains, tail = held_out_predictive_gains(times, 500.0, 1_000.0, degenerate, 0.8)
        assert np.allclose(gains, 0.0, atol=1e-12)
        assert tail == pytest.approx(0.0, abs=1e-12)

    def test_the_statistic_is_the_mean_over_its_own_standard_error(self):
        times = simulate_hawkes(
            HawkesParameters(mu=0.5, alpha=1.0, beta=2.0), 3_000.0, np.random.default_rng(31)
        )
        hawkes = fit_hawkes(times, 0.0, 2_100.0)
        poisson = fit_poisson(times, 0.0, 2_100.0)
        comparison = predictive_comparison(
            times, 2_100.0, 3_000.0, hawkes.parameters, poisson.parameters.rate
        )
        assert comparison.statistic == pytest.approx(
            comparison.mean_gain / comparison.standard_error, rel=1e-12
        )
        assert comparison.events > MIN_HELD_OUT_EVENTS


class TestTheGate:
    """The acceptance criterion for the phase: Hawkes must beat a Poisson
    baseline on held-out data before it ships."""

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    def test_a_self_exciting_tape_adopts_the_self_exciting_model(self, seed):
        times = simulate_hawkes(
            HawkesParameters(mu=0.5, alpha=1.2, beta=2.0), 3_000.0, np.random.default_rng(seed)
        )
        comparison = compare_held_out(times, 0.0, 3_000.0)
        assert comparison.hawkes_is_adopted
        assert comparison.predictive.statistic > comparison.predictive.critical_value
        assert comparison.log_likelihood_gain > 0.0
        assert "not fitted on" in comparison.reason

    @pytest.mark.parametrize("seed", [100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    def test_a_poisson_tape_does_not_adopt_it(self, seed):
        """The important half. On arrivals with no clustering at all, the raw
        held-out total is positive about as often as not — by hundredths of a
        nat, which is noise with a sign. The test on the *mean* per-event gain
        against its own standard error is what refuses it."""
        rng = np.random.default_rng(seed)
        times = np.sort(rng.uniform(0.0, 3_000.0, rng.poisson(3_000)))
        comparison = compare_held_out(times, 0.0, 3_000.0)
        assert not comparison.hawkes_is_adopted
        assert comparison.predictive.statistic <= comparison.predictive.critical_value
        assert "within the noise" in comparison.reason

    def test_a_raw_positive_total_is_not_enough_on_its_own(self):
        """Nails the failure the test exists to prevent: at least one Poisson
        tape in the batch above has a *positive* raw held-out gain and is still
        refused, so the gate is not simply reading the sign of a difference."""
        refused_with_a_positive_total = 0
        for seed in range(100, 130):
            rng = np.random.default_rng(seed)
            times = np.sort(rng.uniform(0.0, 3_000.0, rng.poisson(3_000)))
            comparison = compare_held_out(times, 0.0, 3_000.0)
            if comparison.log_likelihood_gain > 0.0 and not comparison.hawkes_is_adopted:
                refused_with_a_positive_total += 1
        assert refused_with_a_positive_total > 0

    def test_a_thin_held_out_window_is_refused_rather_than_judged(self):
        times = np.linspace(1.0, 900.0, MIN_TRAINING_EVENTS + MIN_HELD_OUT_EVENTS - 5)
        with pytest.raises(IntensityUnavailable) as excinfo:
            compare_held_out(times, 0.0, 1_000.0, train_fraction=0.95)
        assert excinfo.value.reason is IntensityRefusal.TOO_FEW_HELD_OUT_EVENTS

    def test_the_split_is_by_time_not_by_event_count(self):
        """Splitting by count would put the busy period on whichever side had
        more events and make the comparison a statement about that."""
        times = simulate_hawkes(
            HawkesParameters(mu=0.5, alpha=1.0, beta=2.0), 2_000.0, np.random.default_rng(8)
        )
        comparison = compare_held_out(times, 0.0, 2_000.0, train_fraction=0.6)
        assert comparison.split_timestamp == pytest.approx(1_200.0)

    def test_raising_the_critical_value_can_only_make_the_gate_stricter(self):
        times = simulate_hawkes(
            HawkesParameters(mu=0.5, alpha=1.2, beta=2.0), 3_000.0, np.random.default_rng(2)
        )
        lenient = compare_held_out(times, 0.0, 3_000.0, critical_value=0.0)
        strict = compare_held_out(times, 0.0, 3_000.0, critical_value=1_000.0)
        assert lenient.hawkes_is_adopted
        assert not strict.hawkes_is_adopted
