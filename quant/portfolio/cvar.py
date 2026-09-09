"""Minimum-CVaR portfolios, by linear programming.

Rockafellar & Uryasev (2000) showed that conditional value at risk can be
minimised as a linear program over a return sample, without assuming a
distribution:

```
minimise   zeta + 1/((1-alpha) m) * sum_j u_j
subject to u_j >= -(r_j . w) - zeta,   u_j >= 0
```

At the optimum ``zeta`` is the value at risk and the objective is the CVaR. That
it needs no distributional assumption is the point: a mean-variance portfolio
optimises a symmetric risk measure over asymmetric returns, and an option book's
returns are about as asymmetric as they come.

The catch is stated rather than hidden: **the answer is only as good as the
scenario sample.** CVaR at 95% from 100 scenarios is an average over five points,
and five points do not describe a tail. The scenario count and the number of
observations in the tail are both reported, and a thin tail is warned about with
the same discipline the historical VaR estimator uses.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from quant.portfolio.constraints import Constraints

CVAR_VERSION = "cvar-optimiser@1.0.0"

#: Below this many scenarios in the tail, the CVaR is an average over too few
#: points to mean much. The same threshold the historical tail-risk estimator
#: applies, for the same reason.
MIN_TAIL_SCENARIOS = 10


class CVaRWarning:
    FEW_SCENARIOS = "CVAR_FEW_SCENARIOS"
    THIN_TAIL = "CVAR_THIN_TAIL"


class CVaRFailed(RuntimeError):
    """The linear program did not solve.

    An exception rather than a fallback: this problem is convex and bounded when
    it is feasible, so a failure means the constraints are contradictory in a way
    the pre-check missed, and returning some other portfolio would hide that.
    """

    def __init__(self, message: str) -> None:
        super().__init__(f"the CVaR program did not solve: {message}")


@dataclass(frozen=True, slots=True)
class CVaRResult:
    weights: np.ndarray
    #: The optimal ``zeta``: the value at risk at this confidence, as a loss.
    value_at_risk: float
    #: The objective: the average loss in the tail beyond that threshold.
    conditional_value_at_risk: float
    confidence: float
    scenarios: int
    #: Scenarios whose loss exceeded the threshold. The sample the CVaR is
    #: actually an average over.
    tail_scenarios: int
    expected_return: float | None
    gross_exposure: float
    net_exposure: float
    binding_constraints: tuple[str, ...]
    solver_message: str
    warnings: tuple[str, ...] = ()
    model_version: str = CVAR_VERSION

    @property
    def is_reliable(self) -> bool:
        return self.tail_scenarios >= MIN_TAIL_SCENARIOS

    def to_dict(self) -> dict:
        return {
            "weights": [float(value) for value in self.weights],
            "value_at_risk": self.value_at_risk,
            "conditional_value_at_risk": self.conditional_value_at_risk,
            "confidence": self.confidence,
            "scenarios": self.scenarios,
            "tail_scenarios": self.tail_scenarios,
            "is_reliable": self.is_reliable,
            "expected_return": self.expected_return,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "binding_constraints": list(self.binding_constraints),
            "solver_message": self.solver_message,
            "warnings": list(self.warnings),
            "model_version": self.model_version,
            "interpretation": {
                "value_at_risk": (
                    "A threshold loss: exceeded with probability 1 - confidence over "
                    "the horizon the scenarios were drawn on."
                ),
                "conditional_value_at_risk": (
                    "The average loss in the cases that exceeded it, which is why it "
                    "is never smaller than the value at risk."
                ),
                "tail_scenarios": (
                    "How many sample points the CVaR is an average over. A CVaR from "
                    "five points is a number, not an estimate."
                ),
            },
        }


def minimum_cvar(
    scenarios: np.ndarray,
    constraints: Constraints,
    confidence: float = 0.95,
    expected_returns: np.ndarray | None = None,
    minimum_expected_return: float | None = None,
) -> CVaRResult:
    """Least conditional value at risk, subject to the constraints.

    ``scenarios`` is ``(m, n)``: one row per scenario, one column per asset,
    holding **returns** rather than prices. Where they come from is the caller's
    problem and is recorded there — historical windows, a bootstrap and a Monte
    Carlo simulation give three different answers, and the difference is a
    modelling choice rather than a detail.

    ``minimum_expected_return`` adds the efficient-frontier constraint. Supplying
    it requires ``expected_returns``, and the same warning applies as everywhere
    else: a mean estimated from the same sample the scenarios came from is not a
    forecast.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")

    sample = np.asarray(scenarios, dtype=float)
    if sample.ndim != 2:
        raise ValueError("scenarios must be a 2-D (scenarios, assets) array")
    count, assets = sample.shape
    if assets != constraints.size:
        raise ValueError(
            f"scenarios have {assets} assets and the constraints describe {constraints.size}"
        )
    if count < 2:
        raise ValueError("at least two scenarios are needed")

    constraints.check_feasible()
    warnings: list[str] = []

    needs_gross = constraints.maximum_gross_exposure is not None
    needs_turnover = (
        constraints.maximum_turnover is not None and constraints.current_weights is not None
    )

    # Variable layout: w (n) | zeta (1) | u (m) | t (n, |w|) | s (n, |w - w0|)
    n_w, n_zeta, n_u = assets, 1, count
    n_t = assets if needs_gross else 0
    n_s = assets if needs_turnover else 0
    total = n_w + n_zeta + n_u + n_t + n_s

    offset_zeta = n_w
    offset_u = offset_zeta + n_zeta
    offset_t = offset_u + n_u
    offset_s = offset_t + n_t

    objective = np.zeros(total)
    objective[offset_zeta] = 1.0
    objective[offset_u : offset_u + n_u] = 1.0 / ((1.0 - confidence) * count)

    rows: list[np.ndarray] = []
    bounds_ub: list[float] = []

    # -(r_j . w) - zeta - u_j <= 0
    for index in range(count):
        row = np.zeros(total)
        row[:n_w] = -sample[index]
        row[offset_zeta] = -1.0
        row[offset_u + index] = -1.0
        rows.append(row)
        bounds_ub.append(0.0)

    def _linear(coefficients: np.ndarray, limit: float) -> None:
        row = np.zeros(total)
        row[:n_w] = coefficients
        rows.append(row)
        bounds_ub.append(limit)

    if constraints.maximum_net_exposure is not None:
        _linear(np.ones(assets), float(constraints.maximum_net_exposure))
    if constraints.minimum_net_exposure is not None:
        _linear(-np.ones(assets), -float(constraints.minimum_net_exposure))

    for group in constraints.groups:
        picker = np.zeros(assets)
        picker[list(group.indices)] = 1.0
        if group.maximum is not None:
            _linear(picker, float(group.maximum))
        if group.minimum is not None:
            _linear(-picker, -float(group.minimum))

    if minimum_expected_return is not None:
        if expected_returns is None:
            raise ValueError(
                "a minimum expected return needs an expected-return vector; the "
                "platform will not estimate one for this constraint"
            )
        _linear(-np.asarray(expected_returns, dtype=float), -float(minimum_expected_return))

    if needs_gross:
        # |w_i| <= t_i, written as the two linear halves an LP can take.
        for index in range(assets):
            for sign in (1.0, -1.0):
                row = np.zeros(total)
                row[index] = sign
                row[offset_t + index] = -1.0
                rows.append(row)
                bounds_ub.append(0.0)
        row = np.zeros(total)
        row[offset_t : offset_t + n_t] = 1.0
        rows.append(row)
        bounds_ub.append(float(constraints.maximum_gross_exposure))

    if needs_turnover:
        current = np.asarray(constraints.current_weights, dtype=float)
        for index in range(assets):
            for sign in (1.0, -1.0):
                row = np.zeros(total)
                row[index] = sign
                row[offset_s + index] = -1.0
                rows.append(row)
                bounds_ub.append(float(sign * current[index]))
        row = np.zeros(total)
        row[offset_s : offset_s + n_s] = 1.0
        rows.append(row)
        bounds_ub.append(float(constraints.maximum_turnover))

    equalities = None
    equality_values = None
    if constraints.budget is not None:
        equality = np.zeros((1, total))
        equality[0, :n_w] = 1.0
        equalities = equality
        equality_values = np.array([float(constraints.budget)])

    variable_bounds: list[tuple[float | None, float | None]] = list(constraints.bounds())
    variable_bounds.append((None, None))  # zeta is free: a loss threshold may be negative
    variable_bounds.extend([(0.0, None)] * n_u)
    variable_bounds.extend([(0.0, None)] * n_t)
    variable_bounds.extend([(0.0, None)] * n_s)

    outcome = linprog(
        objective,
        A_ub=np.vstack(rows) if rows else None,
        b_ub=np.asarray(bounds_ub) if bounds_ub else None,
        A_eq=equalities,
        b_eq=equality_values,
        bounds=variable_bounds,
        method="highs",
    )
    if not outcome.success:
        raise CVaRFailed(str(outcome.message))

    weights = np.asarray(outcome.x[:n_w], dtype=float)
    zeta = float(outcome.x[offset_zeta])
    losses = -(sample @ weights)
    tail = int(np.sum(losses > zeta + 1e-12))

    if count < 100:
        warnings.append(CVaRWarning.FEW_SCENARIOS)
    if tail < MIN_TAIL_SCENARIOS:
        warnings.append(CVaRWarning.THIN_TAIL)

    return CVaRResult(
        weights=weights,
        value_at_risk=zeta,
        conditional_value_at_risk=float(outcome.fun),
        confidence=confidence,
        scenarios=count,
        tail_scenarios=tail,
        expected_return=(
            float(np.asarray(expected_returns, dtype=float) @ weights)
            if expected_returns is not None
            else None
        ),
        gross_exposure=float(np.sum(np.abs(weights))),
        net_exposure=float(np.sum(weights)),
        binding_constraints=tuple(constraints.binding(weights)),
        solver_message=str(outcome.message),
        warnings=tuple(sorted(set(warnings))),
    )
