"""Black-Litterman: blending a market prior with stated views.

The model's appeal is that it answers the objection to mean-variance directly.
Rather than asking a user for expected returns — which they do not have, and
which the optimiser will then maximise the error of — it starts from the returns
*implied* by a prior portfolio and moves them only as far as an explicit view,
weighted by how confident that view is.

Two inputs the platform cannot supply, and does not pretend to:

**The market portfolio.** The textbook prior is market-capitalisation weights,
and the platform holds no market caps. So the prior portfolio is an argument. A
user with no market weights can pass equal weights and will get equal-weight
equilibrium returns, which is a defensible prior and a different one — and the
result says which was used.

**Risk aversion and the prior's uncertainty.** ``delta`` scales the whole
equilibrium vector and ``tau`` sets how far a view can move it. Neither has a
consensus value; ``tau`` in particular is quoted anywhere between 0.01 and 1 in
the literature. Both are required arguments and both are recorded.

Reference: Black & Litterman (1992), *Global Portfolio Optimization*, and the
restatement in Idzorek (2005).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BLACK_LITTERMAN_VERSION = "black-litterman@1.0.0"


class ViewError(ValueError):
    """A view could not be expressed against these assets."""


@dataclass(frozen=True, slots=True)
class View:
    """One statement about returns.

    ``weights`` picks the assets: ``{0: 1.0}`` says "asset 0 will return
    ``expected_return``"; ``{0: 1.0, 1: -1.0}`` says "asset 0 will beat asset 1
    by that much". A relative view is the kind anyone actually holds, which is
    why the picker is a mapping rather than a single index.
    """

    weights: dict[int, float]
    expected_return: float
    #: Standard deviation of the view's own error. Smaller means more confident.
    #: Required rather than derived: a confidence the platform inferred would be
    #: the platform's view, not the user's.
    uncertainty: float
    description: str = ""

    def __post_init__(self) -> None:
        if self.uncertainty <= 0:
            raise ViewError(
                "a view's uncertainty must be positive; a view held with certainty "
                "is a constraint, not a view"
            )
        if not self.weights:
            raise ViewError("a view must name at least one asset")

    def to_dict(self) -> dict:
        return {
            "weights": {str(index): value for index, value in self.weights.items()},
            "expected_return": self.expected_return,
            "uncertainty": self.uncertainty,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class BlackLittermanResult:
    """The posterior, and the prior it moved from."""

    posterior_returns: np.ndarray
    posterior_covariance: np.ndarray
    equilibrium_returns: np.ndarray
    #: How far each asset's expected return moved. The interesting output: a
    #: view on one asset moves its correlated neighbours too, and that is the
    #: model working rather than a bug.
    shift: np.ndarray
    prior_weights: np.ndarray
    risk_aversion: float
    tau: float
    views: tuple[View, ...]
    model_version: str = BLACK_LITTERMAN_VERSION

    def to_dict(self) -> dict:
        return {
            "posterior_returns": [float(value) for value in self.posterior_returns],
            "equilibrium_returns": [float(value) for value in self.equilibrium_returns],
            "shift": [float(value) for value in self.shift],
            "prior_weights": [float(value) for value in self.prior_weights],
            "risk_aversion": self.risk_aversion,
            "tau": self.tau,
            "views": [view.to_dict() for view in self.views],
            "model_version": self.model_version,
            "interpretation": {
                "shift": (
                    "How far each asset's expected return moved from the prior. An "
                    "asset nobody expressed a view on still moves, through its "
                    "correlation with one that was — which is the model working."
                ),
                "tau": (
                    "How uncertain the prior is held to be. There is no consensus "
                    "value; it was supplied, and it scales how far views move the "
                    "answer."
                ),
            },
        }


def equilibrium_returns(
    covariance: np.ndarray, prior_weights: np.ndarray, risk_aversion: float
) -> np.ndarray:
    """``Pi = delta * Sigma * w``: the returns a prior portfolio implies.

    Reverse optimisation. If the prior portfolio is what a risk-aversion-``delta``
    investor would hold, these are the returns they must be expecting. It is the
    one place in mean-variance where a return vector can be derived rather than
    guessed, which is why the model starts here.
    """
    if risk_aversion <= 0:
        raise ValueError("risk aversion must be positive")
    return risk_aversion * (covariance @ prior_weights)


def blend(
    covariance: np.ndarray,
    prior_weights: np.ndarray,
    views: tuple[View, ...],
    risk_aversion: float,
    tau: float,
) -> BlackLittermanResult:
    """Blend the equilibrium with the views.

    ```
    posterior = [(tau Sigma)^-1 + P' Omega^-1 P]^-1 [(tau Sigma)^-1 Pi + P' Omega^-1 Q]
    ```

    with ``Omega`` diagonal from the views' own stated uncertainties. Diagonal
    because the alternative — a full view-covariance — asks the user to state
    how their views correlate, which nobody can, and filling it in for them
    would be the platform inventing an opinion.
    """
    size = len(prior_weights)
    if covariance.shape != (size, size):
        raise ViewError(f"covariance is {covariance.shape} for {size} prior weights")
    if tau <= 0:
        raise ValueError("tau must be positive")

    pi = equilibrium_returns(covariance, prior_weights, risk_aversion)
    if not views:
        # No views: the posterior *is* the equilibrium. Returned rather than
        # refused, because "what does my prior imply" is a real question.
        return BlackLittermanResult(
            posterior_returns=pi,
            posterior_covariance=covariance.copy(),
            equilibrium_returns=pi,
            shift=np.zeros(size),
            prior_weights=prior_weights,
            risk_aversion=risk_aversion,
            tau=tau,
            views=(),
        )

    picker = np.zeros((len(views), size))
    view_returns = np.zeros(len(views))
    omega = np.zeros((len(views), len(views)))

    for row, view in enumerate(views):
        for index, weight in view.weights.items():
            if not 0 <= index < size:
                raise ViewError(f"view names asset {index}, which is outside 0..{size - 1}")
            picker[row, index] = weight
        view_returns[row] = view.expected_return
        omega[row, row] = view.uncertainty**2

    tau_sigma_inverse = np.linalg.pinv(tau * covariance)
    omega_inverse = np.linalg.pinv(omega)

    precision = tau_sigma_inverse + picker.T @ omega_inverse @ picker
    posterior_covariance_of_mean = np.linalg.pinv(precision)
    posterior = posterior_covariance_of_mean @ (
        tau_sigma_inverse @ pi + picker.T @ omega_inverse @ view_returns
    )

    return BlackLittermanResult(
        posterior_returns=posterior,
        # The covariance of returns, not of the mean: the estimation uncertainty
        # in the mean adds to it. Conflating the two understates portfolio risk.
        posterior_covariance=covariance + posterior_covariance_of_mean,
        equilibrium_returns=pi,
        shift=posterior - pi,
        prior_weights=prior_weights,
        risk_aversion=risk_aversion,
        tau=tau,
        views=views,
    )
