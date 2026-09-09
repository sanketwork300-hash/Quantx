"""Portfolio optimisation.

The mathematics here is standard and the decisions around it are not. Two are
worth stating before the code.

**Expected returns are the problem, not the covariance.** Mean-variance
optimisation is an error maximiser: it puts the most weight exactly where the
estimation error is largest, because a spuriously high estimated return looks
identical to a real one. Historical sample means are a famously poor forecast of
future means — the estimation error swamps the signal at any sample length a
practitioner has — and a portfolio built on them is a portfolio built on noise.

So expected returns are never invented here. They arrive as an
:class:`ExpectedReturns` carrying a **declared source**, and an objective that
does not need them (minimum variance, risk parity) is available precisely so a
user can decline to supply them. A run using historical means is labelled with
what that means.

**A failed solve is reported, never replaced.** There is no fallback to equal
weights. Equal weights is a legitimate portfolio and a terrible error message:
returned silently it looks like an answer, and the user acts on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from scipy.optimize import minimize

from quant.portfolio.constraints import Constraints

OPTIMISER_VERSION = "portfolio-optimiser@1.0.0"

#: Solver attempts from different starting points. Several of these objectives
#: are not convex over the feasible set, so one start is one local answer; the
#: number tried and the number that converged are both reported.
DEFAULT_STARTS = 5

#: Below this, a variance is treated as zero and a Sharpe ratio is not formed.
MIN_VARIANCE = 1e-16


class Objective(StrEnum):
    """What the optimiser is being asked for."""

    #: Least variance, subject to the constraints. Needs no expected returns,
    #: which is why it is the honest default when none can be justified.
    MINIMUM_VARIANCE = "MINIMUM_VARIANCE"
    #: Highest excess return per unit of volatility. Needs expected returns.
    MAXIMUM_SHARPE = "MAXIMUM_SHARPE"
    #: ``mu'w - (lambda/2) w'Sigma w``. Needs expected returns and a stated
    #: risk aversion, which is a statement about a person rather than a market.
    MEAN_VARIANCE = "MEAN_VARIANCE"
    #: Equal risk contribution. Needs no expected returns.
    RISK_PARITY = "RISK_PARITY"
    #: Least conditional value at risk over a scenario sample. Needs scenarios
    #: rather than a covariance, and makes no distributional assumption.
    MINIMUM_CVAR = "MINIMUM_CVAR"


class ReturnSource(StrEnum):
    """Where an expected-return vector came from.

    Carried on every result. Two portfolios built from the same covariance and
    different return sources are different objects, and a report that did not
    say which is which would be comparing them as though they were not.
    """

    #: The user stated a view.
    SUPPLIED = "SUPPLIED"
    #: Sample mean of historical returns. Reported with its known weakness.
    HISTORICAL_MEAN = "HISTORICAL_MEAN"
    #: Reverse-optimised from a prior portfolio: ``Pi = delta * Sigma * w``.
    EQUILIBRIUM = "EQUILIBRIUM"
    #: Posterior of a Black-Litterman blend.
    BLACK_LITTERMAN = "BLACK_LITTERMAN"


class OptimisationWarning(StrEnum):
    HISTORICAL_MEANS_USED = "OPTIMISER_HISTORICAL_MEANS_USED"
    SOME_STARTS_FAILED = "OPTIMISER_SOME_STARTS_FAILED"
    COVARIANCE_ADJUSTED = "OPTIMISER_COVARIANCE_ADJUSTED"
    SOLUTION_AT_MANY_BOUNDS = "OPTIMISER_SOLUTION_AT_MANY_BOUNDS"
    NO_RISK_FREE_RATE = "OPTIMISER_NO_RISK_FREE_RATE"
    SCENARIOS_FEW = "OPTIMISER_SCENARIOS_FEW"


class OptimisationFailed(RuntimeError):
    """No start converged to a feasible point.

    Deliberately an exception rather than a degraded result. Returning equal
    weights, or the best infeasible point, would look like an answer.
    """

    def __init__(self, objective: Objective, starts: int, message: str) -> None:
        super().__init__(
            f"{objective} did not converge from any of {starts} starting points: {message}"
        )
        self.objective = objective
        self.starts = starts


@dataclass(frozen=True, slots=True)
class ExpectedReturns:
    """A return forecast and where it came from."""

    values: np.ndarray
    source: ReturnSource
    #: Free text: the view, the estimation window, the prior. Recorded verbatim.
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "description": self.description,
            "values": [float(value) for value in self.values],
        }


@dataclass(frozen=True, slots=True)
class OptimisationResult:
    """The portfolio, and everything needed to argue with it."""

    objective: Objective
    weights: np.ndarray
    expected_return: float | None
    volatility: float
    sharpe: float | None
    #: Fraction of total risk each asset contributes. Sums to one, and is the
    #: number that shows a "diversified" portfolio carrying one real bet.
    risk_contributions: np.ndarray
    #: ``1 / sum(w^2)`` on the normalised absolute weights: how many assets the
    #: portfolio is really spread across, which is usually fewer than it holds.
    effective_assets: float
    gross_exposure: float
    net_exposure: float
    turnover: float | None
    binding_constraints: tuple[str, ...]
    starts_attempted: int
    starts_converged: int
    iterations: int
    solver_message: str
    return_source: ReturnSource | None
    warnings: tuple[str, ...] = ()
    model_version: str = OPTIMISER_VERSION

    def to_dict(self) -> dict:
        return {
            "objective": str(self.objective),
            "weights": [float(value) for value in self.weights],
            "expected_return": self.expected_return,
            "volatility": self.volatility,
            "sharpe": self.sharpe,
            "risk_contributions": [float(value) for value in self.risk_contributions],
            "effective_assets": self.effective_assets,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "turnover": self.turnover,
            "binding_constraints": list(self.binding_constraints),
            "starts_attempted": self.starts_attempted,
            "starts_converged": self.starts_converged,
            "iterations": self.iterations,
            "solver_message": self.solver_message,
            "return_source": str(self.return_source) if self.return_source else None,
            "warnings": list(self.warnings),
            "model_version": self.model_version,
            "interpretation": {
                "risk_contributions": (
                    "Fraction of portfolio variance each asset accounts for. A holding "
                    "with 5% of the weight and 40% of the risk is the portfolio's real "
                    "position, whatever the weights say."
                ),
                "effective_assets": (
                    "Inverse Herfindahl of the absolute weights: how many assets the "
                    "portfolio is genuinely spread across."
                ),
            },
        }


# ------------------------------------------------------------------ helpers
def portfolio_variance(weights: np.ndarray, covariance: np.ndarray) -> float:
    return float(weights @ covariance @ weights)


def risk_contributions(weights: np.ndarray, covariance: np.ndarray) -> np.ndarray:
    """Each asset's share of portfolio variance.

    ``w_i (Sigma w)_i / (w' Sigma w)``. Sums to one by construction, and is the
    diagnostic that shows a portfolio holding forty names and carrying one bet.
    """
    variance = portfolio_variance(weights, covariance)
    if variance <= MIN_VARIANCE:
        return np.zeros_like(weights)
    return (weights * (covariance @ weights)) / variance


def effective_assets(weights: np.ndarray) -> float:
    gross = float(np.sum(np.abs(weights)))
    if gross <= 0:
        return 0.0
    shares = np.abs(weights) / gross
    concentration = float(np.sum(shares**2))
    return 1.0 / concentration if concentration > 0 else 0.0


def _starting_points(constraints: Constraints, count: int, seed: int) -> list[np.ndarray]:
    """Feasible-ish starts: the equal-weight point, then randomised ones.

    Equal weight first because it is the natural centre of most feasible sets
    and converges fastest; random after, because several of these objectives are
    not convex and one start is one local answer.
    """
    size = constraints.size
    budget = constraints.budget if constraints.budget is not None else 1.0
    generator = np.random.default_rng(seed)

    starts = [np.full(size, budget / size)]
    for _ in range(max(count - 1, 0)):
        draw = generator.random(size)
        if not constraints.long_only:
            draw = draw - 0.5
        total = np.sum(np.abs(draw))
        starts.append(draw / total * abs(budget) if total > 0 else np.full(size, budget / size))
    return starts


def _solve(
    objective_function,
    constraints: Constraints,
    starts: Sequence[np.ndarray],
) -> tuple[np.ndarray, int, int, str]:
    """Run SLSQP from each start, keep the best feasible answer.

    Returns ``(weights, converged, iterations, message)``. Raises when nothing
    converged: a portfolio that did not solve is not a portfolio.
    """
    scipy_constraints = constraints.scipy_constraints()
    bounds = constraints.bounds()

    best: np.ndarray | None = None
    best_value = float("inf")
    converged = 0
    iterations = 0
    message = "no start converged"

    for start in starts:
        outcome = minimize(
            objective_function,
            start,
            method="SLSQP",
            bounds=bounds,
            constraints=scipy_constraints,
            options={"maxiter": 500, "ftol": 1e-12},
        )
        iterations += int(outcome.nit)
        if not outcome.success:
            continue
        converged += 1
        value = float(outcome.fun)
        if value < best_value:
            best_value = value
            best = np.asarray(outcome.x, dtype=float)
            message = str(outcome.message)

    if best is None:
        raise OptimisationFailed(Objective.MINIMUM_VARIANCE, len(starts), message)
    return best, converged, iterations, message


def _finalise(
    objective: Objective,
    weights: np.ndarray,
    covariance: np.ndarray,
    expected: ExpectedReturns | None,
    constraints: Constraints,
    risk_free_rate: float,
    starts: int,
    converged: int,
    iterations: int,
    message: str,
    warnings: list[str],
) -> OptimisationResult:
    variance = portfolio_variance(weights, covariance)
    volatility = float(np.sqrt(max(variance, 0.0)))
    expected_return = float(expected.values @ weights) if expected is not None else None
    sharpe = None
    if expected_return is not None and volatility > np.sqrt(MIN_VARIANCE):
        sharpe = (expected_return - risk_free_rate) / volatility

    binding = constraints.binding(weights)
    if len(binding) > max(2, constraints.size // 2):
        # More than half the assets pinned means the constraints, not the
        # objective, chose this portfolio. Worth saying out loud.
        warnings.append(str(OptimisationWarning.SOLUTION_AT_MANY_BOUNDS))
    if converged < starts:
        warnings.append(str(OptimisationWarning.SOME_STARTS_FAILED))

    turnover = None
    if constraints.current_weights is not None:
        turnover = float(
            np.sum(np.abs(weights - np.asarray(constraints.current_weights, dtype=float)))
        )

    return OptimisationResult(
        objective=objective,
        weights=weights,
        expected_return=expected_return,
        volatility=volatility,
        sharpe=sharpe,
        risk_contributions=risk_contributions(weights, covariance),
        effective_assets=effective_assets(weights),
        gross_exposure=float(np.sum(np.abs(weights))),
        net_exposure=float(np.sum(weights)),
        turnover=turnover,
        binding_constraints=tuple(binding),
        starts_attempted=starts,
        starts_converged=converged,
        iterations=iterations,
        solver_message=message,
        return_source=expected.source if expected is not None else None,
        warnings=tuple(sorted(set(warnings))),
    )


def _prepare(covariance: np.ndarray, warnings: list[str]) -> np.ndarray:
    """Symmetrise, and project to the nearest PSD matrix if it is not one.

    A sample covariance can come back with a tiny negative eigenvalue from
    floating-point error, and SLSQP will happily walk off into a negative
    variance. The projection is recorded rather than done quietly, because a
    matrix that needed one may be telling the user their estimate is degenerate.
    """
    from quant.statistics.covariance import nearest_positive_semidefinite

    symmetric = 0.5 * (covariance + covariance.T)
    adjusted, changed = nearest_positive_semidefinite(symmetric)
    if changed:
        warnings.append(str(OptimisationWarning.COVARIANCE_ADJUSTED))
    return adjusted


# --------------------------------------------------------------- objectives
def minimum_variance(
    covariance: np.ndarray,
    constraints: Constraints,
    expected: ExpectedReturns | None = None,
    risk_free_rate: float = 0.0,
    starts: int = DEFAULT_STARTS,
    seed: int = 20_260_924,
) -> OptimisationResult:
    """Least variance. Needs no return forecast, which is the point of it."""
    constraints.check_feasible()
    warnings: list[str] = []
    matrix = _prepare(covariance, warnings)

    weights, converged, iterations, message = _solve(
        lambda w: portfolio_variance(w, matrix),
        constraints,
        _starting_points(constraints, starts, seed),
    )
    return _finalise(
        Objective.MINIMUM_VARIANCE,
        weights,
        matrix,
        expected,
        constraints,
        risk_free_rate,
        starts,
        converged,
        iterations,
        message,
        warnings,
    )


def mean_variance(
    covariance: np.ndarray,
    expected: ExpectedReturns,
    constraints: Constraints,
    risk_aversion: float,
    risk_free_rate: float = 0.0,
    starts: int = DEFAULT_STARTS,
    seed: int = 20_260_924,
) -> OptimisationResult:
    """Maximise ``mu'w - (lambda/2) w'Sigma w``.

    ``risk_aversion`` is required and has no default. It is a statement about a
    person's tolerance, not a property of the market, and a platform that picked
    one would be choosing a portfolio on the user's behalf.
    """
    if risk_aversion <= 0:
        raise ValueError("risk aversion must be positive")
    constraints.check_feasible()
    warnings = _return_warnings(expected)
    matrix = _prepare(covariance, warnings)

    weights, converged, iterations, message = _solve(
        lambda w: -(expected.values @ w) + 0.5 * risk_aversion * portfolio_variance(w, matrix),
        constraints,
        _starting_points(constraints, starts, seed),
    )
    return _finalise(
        Objective.MEAN_VARIANCE,
        weights,
        matrix,
        expected,
        constraints,
        risk_free_rate,
        starts,
        converged,
        iterations,
        message,
        warnings,
    )


def maximum_sharpe(
    covariance: np.ndarray,
    expected: ExpectedReturns,
    constraints: Constraints,
    risk_free_rate: float = 0.0,
    starts: int = DEFAULT_STARTS,
    seed: int = 20_260_924,
) -> OptimisationResult:
    """Highest excess return per unit of volatility.

    Solved directly rather than through the usual homogeneous transformation,
    because the transformation only holds for a budget-and-long-only feasible
    set and quietly gives the wrong answer under a gross-exposure or turnover
    limit. Directly is slower and stays correct under every constraint here.
    """
    constraints.check_feasible()
    warnings = _return_warnings(expected)
    if risk_free_rate == 0.0:
        warnings.append(str(OptimisationWarning.NO_RISK_FREE_RATE))
    matrix = _prepare(covariance, warnings)

    def negative_sharpe(w: np.ndarray) -> float:
        variance = portfolio_variance(w, matrix)
        if variance <= MIN_VARIANCE:
            return 1e6
        return -((expected.values @ w) - risk_free_rate) / float(np.sqrt(variance))

    weights, converged, iterations, message = _solve(
        negative_sharpe, constraints, _starting_points(constraints, starts, seed)
    )
    return _finalise(
        Objective.MAXIMUM_SHARPE,
        weights,
        matrix,
        expected,
        constraints,
        risk_free_rate,
        starts,
        converged,
        iterations,
        message,
        warnings,
    )


def risk_parity(
    covariance: np.ndarray,
    constraints: Constraints,
    expected: ExpectedReturns | None = None,
    risk_free_rate: float = 0.0,
    starts: int = DEFAULT_STARTS,
    seed: int = 20_260_924,
) -> OptimisationResult:
    """Equal risk contribution from every asset.

    The objective is the dispersion of risk contributions about their mean.
    Needs no return forecast — which is most of its appeal, and the reason it is
    offered beside minimum variance rather than as an alternative to a forecast.
    """
    constraints.check_feasible()
    warnings: list[str] = []
    matrix = _prepare(covariance, warnings)
    target = 1.0 / constraints.size

    def dispersion(w: np.ndarray) -> float:
        contributions = risk_contributions(w, matrix)
        return float(np.sum((contributions - target) ** 2))

    weights, converged, iterations, message = _solve(
        dispersion, constraints, _starting_points(constraints, starts, seed)
    )
    return _finalise(
        Objective.RISK_PARITY,
        weights,
        matrix,
        expected,
        constraints,
        risk_free_rate,
        starts,
        converged,
        iterations,
        message,
        warnings,
    )


def _return_warnings(expected: ExpectedReturns) -> list[str]:
    if expected.source is ReturnSource.HISTORICAL_MEAN:
        return [str(OptimisationWarning.HISTORICAL_MEANS_USED)]
    return []
