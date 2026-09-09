"""Portfolio optimisation: objectives, constraints and the target portfolio."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from api.schemas.common import APIModel
from quant.portfolio.optimisation import Objective
from quant.statistics.covariance import CovarianceEstimator


class GroupLimitIn(APIModel):
    """A bound on a named set of assets — a sector limit, usually."""

    name: str = Field(min_length=1, max_length=64)
    #: Positions in ``instrument_ids``, not instrument ids: the optimiser works
    #: in indices and the mapping is the caller's own ordering.
    indices: list[int] = Field(min_length=1)
    minimum: float | None = None
    maximum: float | None = None


class ConstraintsIn(APIModel):
    """The feasible set. Nothing here is defaulted to a plausible limit.

    A portfolio with no stated gross-exposure limit has none; inventing 1.0
    because it is the common case would silently change what was asked for.
    """

    #: Sum of weights. Null means unconstrained — a book not required to be
    #: fully invested.
    budget: float | None = 1.0
    long_only: bool = True
    minimum_weight: float | list[float] | None = None
    maximum_weight: float | list[float] | None = None
    maximum_gross_exposure: float | None = None
    minimum_net_exposure: float | None = None
    maximum_net_exposure: float | None = None
    groups: list[GroupLimitIn] = Field(default_factory=list)
    #: Needs ``current_weights``: turnover without a starting point is
    #: meaningless, and the request is refused rather than reinterpreted.
    maximum_turnover: float | None = None
    current_weights: list[float] | None = None


class ViewIn(APIModel):
    """One Black-Litterman view, in instrument terms."""

    #: ``{instrument_id: coefficient}``. One entry is an absolute view; two with
    #: opposite signs is the relative view anyone actually holds.
    weights: dict[uuid.UUID, float]
    expected_return: float
    #: The view's own error. Required: a confidence the platform inferred would
    #: be the platform's view rather than the caller's.
    uncertainty: float = Field(gt=0)
    description: str = Field(default="", max_length=256)


class OptimiseRequest(APIModel):
    instrument_ids: list[uuid.UUID] = Field(min_length=2, max_length=200)
    objective: Objective
    start: datetime | None = None
    end: datetime | None = None
    exchange: str | None = Field(default=None, max_length=32)
    #: Shrinkage is asked for by name and never applied because a matrix looked
    #: awkward; the intensity it chose is reported on the result.
    covariance_estimator: CovarianceEstimator = CovarianceEstimator.SAMPLE
    constraints: ConstraintsIn | None = None
    #: A stated forecast, in the order of ``instrument_ids``.
    expected_returns: list[float] | None = None
    #: Use the estimation window's sample means. Off by default and warned about
    #: when on: historical means are a poor forecast, and a mean-variance
    #: optimiser puts the most weight exactly where that error is largest.
    use_historical_means: bool = False
    #: A statement about a person's tolerance, not a property of the market.
    #: Required by mean-variance and by Black-Litterman.
    risk_aversion: float | None = Field(default=None, gt=0)
    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    #: The Black-Litterman prior portfolio. Required rather than assumed to be
    #: market-cap weighted: the platform holds no market caps.
    prior_weights: list[float] | None = None
    #: How uncertain the prior is held to be. No consensus value exists — it is
    #: quoted anywhere from 0.01 to 1 — so it is supplied and recorded.
    tau: float | None = Field(default=None, gt=0)
    views: list[ViewIn] = Field(default_factory=list)


class HoldingOut(APIModel):
    instrument_id: uuid.UUID
    symbol: str
    weight: float
    #: Share of portfolio variance. Compare with the weight: they are often very
    #: different, and the difference is where the portfolio's real bet is.
    risk_contribution: float


class PortfolioRiskOut(APIModel):
    volatility: float
    #: Historical VaR and expected shortfall of the portfolio's own return
    #: sample. No distributional assumption.
    tail: dict
    effective_assets: float
    gross_exposure: float
    net_exposure: float
    largest_weight: float
    largest_risk_contribution: float
    observations: int
    interpretation: dict


class TargetPortfolioOut(APIModel):
    objective: Objective
    holdings: list[HoldingOut]
    risk: PortfolioRiskOut
    expected_return: float | None
    sharpe: float | None
    #: Where the return forecast came from. Two portfolios with the same
    #: covariance and different return sources are different objects.
    return_source: str | None
    covariance: dict
    solver: dict
    black_litterman: dict | None
    constraints: dict
