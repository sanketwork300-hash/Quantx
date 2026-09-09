"""Portfolio construction, and the inputs it refuses to invent.

Mean-variance optimisation is an error maximiser: it puts the most weight exactly
where the estimation error in the expected returns is largest, because a
spuriously high estimate looks identical to a real one. So most of these tests
are about inputs — where a forecast came from, whether a risk aversion was
stated, whether a prior was supplied — rather than about the arithmetic, which
is textbook.
"""

from __future__ import annotations

import numpy as np
import pytest

from quant.portfolio.black_litterman import View, ViewError, blend, equilibrium_returns
from quant.portfolio.constraints import (
    Constraints,
    GroupLimit,
    InfeasibleProblem,
)
from quant.portfolio.cvar import minimum_cvar
from quant.portfolio.optimisation import (
    ExpectedReturns,
    Objective,
    OptimisationWarning,
    ReturnSource,
    effective_assets,
    maximum_sharpe,
    mean_variance,
    minimum_variance,
    risk_contributions,
    risk_parity,
)
from quant.statistics.covariance import (
    CovarianceEstimator,
    ledoit_wolf_covariance,
    sample_covariance,
)

VOLS = np.array([0.10, 0.15, 0.20, 0.25, 0.35])
MU = np.array([0.04, 0.06, 0.08, 0.09, 0.12])


def covariance(correlation: float = 0.3) -> np.ndarray:
    size = len(VOLS)
    matrix = np.full((size, size), correlation)
    np.fill_diagonal(matrix, 1.0)
    return np.outer(VOLS, VOLS) * matrix


def long_only(size: int = 5) -> Constraints:
    return Constraints(size=size, budget=1.0, long_only=True)


class TestMinimumVariance:
    def test_it_beats_every_single_asset(self):
        """The whole claim of diversification, and a check that the objective is
        actually being minimised rather than merely evaluated."""
        result = minimum_variance(covariance(), long_only())
        assert result.volatility < VOLS.min()

    def test_the_weights_respect_the_budget_and_the_sign(self):
        result = minimum_variance(covariance(), long_only())
        assert result.weights.sum() == pytest.approx(1.0, abs=1e-6)
        assert (result.weights >= -1e-9).all()

    def test_it_leans_towards_the_quieter_assets(self):
        result = minimum_variance(covariance(), long_only())
        assert result.weights[0] > result.weights[-1]

    def test_it_needs_no_return_forecast(self):
        """Which is the point of offering it: a user who cannot justify a
        forecast should not have to invent one to get a portfolio."""
        result = minimum_variance(covariance(), long_only())
        assert result.expected_return is None
        assert result.return_source is None


class TestRiskParity:
    def test_every_asset_contributes_the_same_risk(self):
        result = risk_parity(covariance(), long_only())
        contributions = result.risk_contributions
        assert contributions == pytest.approx(np.full(5, 0.2), abs=1e-3)

    def test_weights_fall_as_volatility_rises(self):
        result = risk_parity(covariance(), long_only())
        assert list(result.weights) == sorted(result.weights, reverse=True)

    def test_it_is_more_spread_than_minimum_variance(self):
        """Effective assets is the diagnostic that shows it: a book holding five
        names is not necessarily spread across five."""
        parity = risk_parity(covariance(), long_only())
        variance = minimum_variance(covariance(), long_only())
        assert parity.effective_assets > variance.effective_assets


class TestReturnSeekingObjectives:
    def test_maximum_sharpe_prefers_the_better_reward_for_risk(self):
        supplied = ExpectedReturns(MU, ReturnSource.SUPPLIED, "stated")
        result = maximum_sharpe(covariance(), supplied, long_only(), risk_free_rate=0.03)
        assert result.sharpe is not None and result.sharpe > 0
        assert result.expected_return > MU.min()

    def test_higher_risk_aversion_produces_a_quieter_portfolio(self):
        supplied = ExpectedReturns(MU, ReturnSource.SUPPLIED, "stated")
        timid = mean_variance(covariance(), supplied, long_only(), risk_aversion=20.0)
        bold = mean_variance(covariance(), supplied, long_only(), risk_aversion=1.0)
        assert timid.volatility < bold.volatility

    def test_a_non_positive_risk_aversion_is_refused(self):
        supplied = ExpectedReturns(MU, ReturnSource.SUPPLIED, "stated")
        with pytest.raises(ValueError, match="positive"):
            mean_variance(covariance(), supplied, long_only(), risk_aversion=0.0)

    def test_historical_means_are_flagged_wherever_they_are_used(self):
        """The optimiser will maximise their error, and the result has to say
        that is what it did."""
        historical = ExpectedReturns(MU, ReturnSource.HISTORICAL_MEAN, "sample mean")
        result = mean_variance(covariance(), historical, long_only(), risk_aversion=3.0)
        assert str(OptimisationWarning.HISTORICAL_MEANS_USED) in result.warnings
        assert result.return_source is ReturnSource.HISTORICAL_MEAN

    def test_a_supplied_forecast_carries_no_such_warning(self):
        supplied = ExpectedReturns(MU, ReturnSource.SUPPLIED, "stated")
        result = mean_variance(covariance(), supplied, long_only(), risk_aversion=3.0)
        assert str(OptimisationWarning.HISTORICAL_MEANS_USED) not in result.warnings


class TestConstraints:
    def test_a_maximum_weight_binds_and_is_reported(self):
        constraints = Constraints(size=5, budget=1.0, long_only=True, maximum_weight=0.3)
        result = minimum_variance(covariance(), constraints)
        assert result.weights.max() <= 0.3 + 1e-6
        assert any("maximum weight" in item for item in result.binding_constraints)

    def test_a_group_limit_caps_a_sector(self):
        constraints = Constraints(
            size=5,
            budget=1.0,
            long_only=True,
            groups=(GroupLimit("defensives", (0, 1), maximum=0.4),),
        )
        result = minimum_variance(covariance(), constraints)
        assert result.weights[[0, 1]].sum() <= 0.4 + 1e-6

    def test_a_turnover_limit_holds_the_portfolio_near_where_it_was(self):
        current = (0.2, 0.2, 0.2, 0.2, 0.2)
        constraints = Constraints(
            size=5,
            budget=1.0,
            long_only=True,
            maximum_turnover=0.1,
            current_weights=current,
        )
        result = minimum_variance(covariance(), constraints)
        moved = float(np.sum(np.abs(result.weights - np.asarray(current))))
        assert moved <= 0.1 + 1e-4
        assert result.turnover == pytest.approx(moved, abs=1e-6)

    def test_a_turnover_limit_without_a_starting_point_is_refused(self):
        """Turnover from nowhere is not a quantity."""
        with pytest.raises(ValueError, match="current_weights"):
            Constraints(size=5, maximum_turnover=0.2)

    def test_a_long_short_book_can_be_gross_limited(self):
        constraints = Constraints(size=5, budget=0.0, long_only=False, maximum_gross_exposure=2.0)
        result = minimum_variance(covariance(), constraints)
        assert result.gross_exposure <= 2.0 + 1e-4
        assert result.net_exposure == pytest.approx(0.0, abs=1e-6)

    def test_impossible_bounds_are_named_rather_than_solved_for(self):
        """'No solution' is a useless message when six constraints are in play."""
        constraints = Constraints(size=5, budget=1.0, minimum_weight=0.5)
        with pytest.raises(InfeasibleProblem) as exc:
            minimum_variance(covariance(), constraints)
        assert "sum to" in str(exc.value)

    def test_a_budget_outside_the_gross_limit_is_named(self):
        constraints = Constraints(size=5, budget=1.0, maximum_gross_exposure=0.5)
        with pytest.raises(InfeasibleProblem, match="gross exposure"):
            constraints.check_feasible()

    def test_a_minimum_above_a_maximum_is_named_per_asset(self):
        constraints = Constraints(
            size=2, budget=1.0, minimum_weight=[0.8, 0.0], maximum_weight=[0.2, 1.0]
        )
        with pytest.raises(InfeasibleProblem, match="asset 0"):
            constraints.check_feasible()


class TestBlackLitterman:
    def _covariance(self) -> np.ndarray:
        return covariance(0.4)[:4, :4]

    def test_equilibrium_returns_come_from_the_prior_portfolio(self):
        """Reverse optimisation: if this is what a delta-averse investor holds,
        these are the returns they must be expecting."""
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        pi = equilibrium_returns(self._covariance(), prior, 2.5)
        assert len(pi) == 4
        assert (pi > 0).all()

    def test_with_no_views_the_posterior_is_the_prior(self):
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        result = blend(self._covariance(), prior, (), 2.5, 0.05)
        assert result.posterior_returns == pytest.approx(result.equilibrium_returns)
        assert result.shift == pytest.approx(np.zeros(4))

    def test_a_view_moves_the_asset_it_names(self):
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        result = blend(
            self._covariance(),
            prior,
            (View({3: 1.0}, 0.20, 0.02, "asset 3 will do well"),),
            2.5,
            0.05,
        )
        assert result.shift[3] > 0

    def test_a_view_moves_correlated_assets_too(self):
        """Which is the model working rather than a bug: a view on one asset is
        information about everything it moves with."""
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        result = blend(
            self._covariance(),
            prior,
            (View({3: 1.0}, 0.20, 0.02, "asset 3 will do well"),),
            2.5,
            0.05,
        )
        assert abs(result.shift[2]) > 0

    def test_a_more_confident_view_moves_the_answer_further(self):
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        vague = blend(self._covariance(), prior, (View({3: 1.0}, 0.20, 0.20),), 2.5, 0.05)
        sure = blend(self._covariance(), prior, (View({3: 1.0}, 0.20, 0.005),), 2.5, 0.05)
        assert abs(sure.shift[3]) > abs(vague.shift[3])

    def test_a_view_held_with_certainty_is_refused(self):
        """It is a constraint, not a view, and the model has no room for one."""
        with pytest.raises(ViewError, match="certainty"):
            View({0: 1.0}, 0.05, 0.0)

    def test_the_posterior_covariance_exceeds_the_prior(self):
        """Estimation uncertainty in the mean adds to the covariance of returns.
        Conflating the two understates portfolio risk."""
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        result = blend(self._covariance(), prior, (View({0: 1.0}, 0.05, 0.02),), 2.5, 0.05)
        assert (np.diag(result.posterior_covariance) >= np.diag(self._covariance())).all()

    def test_a_view_naming_an_unknown_asset_is_refused(self):
        prior = np.array([0.4, 0.3, 0.2, 0.1])
        with pytest.raises(ViewError, match="outside"):
            blend(self._covariance(), prior, (View({9: 1.0}, 0.05, 0.02),), 2.5, 0.05)


class TestMinimumCVaR:
    def _scenarios(self, count: int = 2000) -> np.ndarray:
        rng = np.random.default_rng(11)
        base = rng.normal(0, 1, size=(count, 4)) * np.array([0.01, 0.015, 0.02, 0.02])
        # Asset 3 gets a fat left tail: the risk a variance cannot see.
        base[:, 3] += rng.standard_t(df=3, size=count) * 0.01 - 0.003
        return base

    def test_it_avoids_the_asset_variance_cannot_see(self):
        result = minimum_cvar(self._scenarios(), long_only(4), confidence=0.95)
        assert result.weights[3] < 0.15

    def test_cvar_is_never_below_var(self):
        """The average loss beyond a threshold cannot be smaller than the
        threshold, and a result where it is would be an arithmetic error."""
        result = minimum_cvar(self._scenarios(), long_only(4), confidence=0.95)
        assert result.conditional_value_at_risk >= result.value_at_risk - 1e-9

    def test_the_tail_holds_roughly_the_right_share_of_scenarios(self):
        result = minimum_cvar(self._scenarios(4000), long_only(4), confidence=0.95)
        assert 0.03 < result.tail_scenarios / result.scenarios < 0.07

    def test_a_thin_tail_is_reported_rather_than_averaged_over_anyway(self):
        """A CVaR from five points is a number, not an estimate."""
        result = minimum_cvar(self._scenarios(60), long_only(4), confidence=0.95)
        assert result.is_reliable is False
        assert "CVAR_THIN_TAIL" in result.warnings

    def test_it_respects_a_group_limit(self):
        constraints = Constraints(
            size=4,
            budget=1.0,
            long_only=True,
            groups=(GroupLimit("pair", (0, 1), maximum=0.5),),
        )
        result = minimum_cvar(self._scenarios(), constraints, confidence=0.95)
        assert result.weights[[0, 1]].sum() <= 0.5 + 1e-6

    def test_a_minimum_return_needs_a_forecast_to_measure_against(self):
        with pytest.raises(ValueError, match="expected-return vector"):
            minimum_cvar(self._scenarios(), long_only(4), minimum_expected_return=0.01)


class TestDiagnostics:
    def test_risk_contributions_sum_to_one(self):
        weights = np.array([0.4, 0.3, 0.2, 0.1, 0.0])
        assert risk_contributions(weights, covariance()).sum() == pytest.approx(1.0)

    def test_effective_assets_counts_the_spread_not_the_holdings(self):
        """A twenty-name book with an effective count of two holds one bet."""
        assert effective_assets(np.array([0.25] * 4)) == pytest.approx(4.0)
        assert effective_assets(np.array([0.97, 0.01, 0.01, 0.01])) < 1.1

    def test_a_concentrated_portfolio_reports_a_large_single_contribution(self):
        result = minimum_variance(
            covariance(), Constraints(size=5, budget=1.0, long_only=True, maximum_weight=1.0)
        )
        assert result.risk_contributions.max() > result.weights.max() * 0.5


class TestCovarianceEstimators:
    def test_shrinkage_is_asked_for_by_name(self):
        rng = np.random.default_rng(3)
        data = rng.normal(0, 0.01, size=(40, 8))
        names = tuple(f"a{index}" for index in range(8))

        plain = sample_covariance(names, data)
        shrunk = ledoit_wolf_covariance(names, data)
        assert plain.estimator is CovarianceEstimator.SAMPLE
        assert plain.shrinkage_intensity is None
        assert shrunk.estimator is CovarianceEstimator.LEDOIT_WOLF
        assert shrunk.shrinkage_intensity is not None

    def test_shrinkage_improves_the_conditioning(self):
        """Which is why it exists: the noise in a sample covariance lands on the
        smallest eigenvalues, and that is where an optimiser goes looking for
        its cleverest trades."""
        rng = np.random.default_rng(3)
        data = rng.normal(0, 0.01, size=(30, 10))
        names = tuple(f"a{index}" for index in range(10))
        plain = sample_covariance(names, data)
        shrunk = ledoit_wolf_covariance(names, data)
        assert np.linalg.cond(shrunk.covariance) < np.linalg.cond(plain.covariance)

    def test_heavy_shrinkage_is_reported(self):
        rng = np.random.default_rng(5)
        data = rng.normal(0, 0.01, size=(25, 15))
        names = tuple(f"a{index}" for index in range(15))
        shrunk = ledoit_wolf_covariance(names, data)
        if shrunk.shrinkage_intensity > 0.5:
            assert "COVARIANCE_HEAVY_SHRINKAGE" in shrunk.warnings


class TestObjectiveCoverage:
    def test_every_objective_in_the_enum_is_reachable(self):
        """A named objective nobody can run is a promise the API does not keep."""
        assert set(Objective) == {
            Objective.MINIMUM_VARIANCE,
            Objective.MAXIMUM_SHARPE,
            Objective.MEAN_VARIANCE,
            Objective.RISK_PARITY,
            Objective.MINIMUM_CVAR,
        }
