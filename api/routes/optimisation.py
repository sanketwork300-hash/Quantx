"""Portfolio construction.

Turns a covariance estimate, a stated forecast and a set of constraints into a
target portfolio, with the risk of *that* portfolio reported beside it.

The endpoint refuses more than it computes, and deliberately so. A return-seeking
objective with no stated forecast is refused rather than given sample means; a
mean-variance run with no risk aversion is refused rather than given a
conventional one; a Black-Litterman run with no prior is refused rather than
given market-cap weights the platform does not hold.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.dependencies.core import CurrentUser, PortfolioOptimisationDep
from api.errors import UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.optimisation import OptimiseRequest, TargetPortfolioOut
from domains.portfolio.optimisation import (
    ConstraintsInput,
    ConstraintsRefused,
    GroupInput,
    OptimisationError,
    OptimisationRequest,
    ViewInput,
)

router = APIRouter(prefix="/portfolio-optimisation", tags=["portfolio-optimisation"])


def _constraints(payload: OptimiseRequest) -> ConstraintsInput | None:
    if payload.constraints is None:
        return None
    supplied = payload.constraints
    return ConstraintsInput(
        budget=supplied.budget,
        long_only=supplied.long_only,
        minimum_weight=supplied.minimum_weight,
        maximum_weight=supplied.maximum_weight,
        maximum_gross_exposure=supplied.maximum_gross_exposure,
        minimum_net_exposure=supplied.minimum_net_exposure,
        maximum_net_exposure=supplied.maximum_net_exposure,
        groups=tuple(
            GroupInput(
                name=group.name,
                indices=tuple(group.indices),
                minimum=group.minimum,
                maximum=group.maximum,
            )
            for group in supplied.groups
        ),
        maximum_turnover=supplied.maximum_turnover,
        current_weights=(tuple(supplied.current_weights) if supplied.current_weights else None),
    )


@router.post("/target", response_model=Envelope)
async def optimise(
    payload: OptimiseRequest,
    user: CurrentUser,
    optimiser: PortfolioOptimisationDep,
) -> Envelope:
    """A target portfolio, and the risk of the portfolio that came back.

    ``return_source`` on the result is the field to read first. Two portfolios
    built from the same covariance and different return sources are different
    objects, and mean-variance maximises the error in whichever was used.
    """
    constraints = _constraints(payload)

    request = OptimisationRequest(
        instrument_ids=tuple(payload.instrument_ids),
        objective=payload.objective,
        start=payload.start,
        end=payload.end,
        exchange=payload.exchange,
        covariance_estimator=payload.covariance_estimator,
        constraints=constraints,
        expected_returns=(tuple(payload.expected_returns) if payload.expected_returns else None),
        use_historical_means=payload.use_historical_means,
        risk_aversion=payload.risk_aversion,
        risk_free_rate=payload.risk_free_rate,
        confidence=payload.confidence,
        prior_weights=tuple(payload.prior_weights) if payload.prior_weights else None,
        tau=payload.tau,
        views=tuple(
            ViewInput(
                weights=dict(view.weights),
                expected_return=view.expected_return,
                uncertainty=view.uncertainty,
                description=view.description,
            )
            for view in payload.views
        ),
    )

    for moment, name in ((payload.start, "start"), (payload.end, "end")):
        if moment is not None and moment.tzinfo is None:
            raise UnprocessableEntity(
                "TIMESTAMP_NOT_TIMEZONE_AWARE",
                f"{name} must carry a UTC offset; a naive timestamp does not name a moment.",
            )

    try:
        result = await optimiser.optimise(user.id, request)
    except ConstraintsRefused as exc:
        raise UnprocessableEntity("INVALID_CONSTRAINTS", str(exc)) from exc
    except OptimisationError as exc:
        raise UnprocessableEntity("OPTIMISATION_REFUSED", str(exc)) from exc

    return Envelope(
        status=str(result.status),
        results=TargetPortfolioOut.model_validate(result.results.to_dict()).model_dump(),
        warnings=[warning.to_dict() for warning in result.warnings],
        provenance=ProvenanceOut(**result.provenance.to_dict()),
    )
