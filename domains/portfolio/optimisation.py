"""Turning views and history into a target portfolio, with its risk stated.

The path the acceptance criterion names — *signals → optimiser → target
portfolio → risk metrics* — runs through :meth:`PortfolioOptimisationService.optimise`.

The service's own contribution is the part the numerics cannot do: deciding
what a return forecast is allowed to be, and refusing to supply one that has not
been asked for. Mean-variance optimisation maximises the error in its inputs, so
where the expected returns came from matters more than which objective was run,
and it is recorded on every result.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.service import InstrumentService
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from domains.warehouse.enums import DatasetKind, DatasetLayer
from domains.warehouse.query import QueryRequest
from domains.warehouse.service import WarehouseService
from infrastructure.settings import Settings
from infrastructure.storage.base import ObjectStore
from quant.portfolio import black_litterman as bl
from quant.portfolio.constraints import Constraints, GroupLimit, InfeasibleProblem
from quant.portfolio.cvar import CVaRFailed, minimum_cvar
from quant.portfolio.optimisation import (
    ExpectedReturns,
    Objective,
    OptimisationFailed,
    ReturnSource,
    maximum_sharpe,
    mean_variance,
    minimum_variance,
    risk_parity,
)
from quant.statistics.covariance import (
    CovarianceEstimator,
    ledoit_wolf_covariance,
    sample_covariance,
)
from quant.statistics.var import historical_tail_risk, losses_from_pnl

#: Below this many aligned observations the covariance is not worth optimising
#: on, whatever the estimator says. Twenty is not a theorem; it is the point
#: below which the answer is arithmetic rather than an estimate.
MIN_OBSERVATIONS = 20


class OptimisationWarningCode:
    HISTORICAL_MEANS_USED = "PORTFOLIO_HISTORICAL_MEANS_USED"
    FEW_OBSERVATIONS = "PORTFOLIO_FEW_OBSERVATIONS"
    INSTRUMENTS_DROPPED = "PORTFOLIO_INSTRUMENTS_DROPPED"
    NO_EXPECTED_RETURNS = "PORTFOLIO_NO_EXPECTED_RETURNS"
    SHRINKAGE_APPLIED = "PORTFOLIO_SHRINKAGE_APPLIED"


class OptimisationError(ValueError):
    """The request could not be turned into a solvable problem."""


class ConstraintsRefused(OptimisationError):
    """The stated constraints contradict each other, or the instruments they name.

    Kept distinct from the general refusal because it is answerable: the caller
    can change the constraints and ask again, whereas a covariance with too few
    observations behind it cannot be argued with.
    """


@dataclass(frozen=True, slots=True)
class GroupInput:
    """A limit on a named subset of the requested instruments.

    ``indices`` are positions in the request's own ``instrument_ids``, which is
    why the caller never has to know how the optimiser orders its columns.
    """

    name: str
    indices: tuple[int, ...]
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class ConstraintsInput:
    """The constraints as the caller stated them.

    This is deliberately the caller's language rather than the solver's: the
    number of assets is not one of the fields, because it is the length of the
    instrument list and asking for it twice invites the two to disagree.
    """

    budget: float = 1.0
    long_only: bool = True
    minimum_weight: float | None = None
    maximum_weight: float | None = None
    maximum_gross_exposure: float | None = None
    minimum_net_exposure: float | None = None
    maximum_net_exposure: float | None = None
    groups: tuple[GroupInput, ...] = ()
    maximum_turnover: float | None = None
    current_weights: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class ViewInput:
    """One statement about returns, in instrument terms rather than indices."""

    weights: dict[uuid.UUID, float]
    expected_return: float
    uncertainty: float
    description: str = ""


@dataclass(frozen=True, slots=True)
class OptimisationRequest:
    """Everything a target portfolio depends on."""

    instrument_ids: tuple[uuid.UUID, ...]
    objective: Objective
    start: datetime | None = None
    end: datetime | None = None
    exchange: str | None = None
    #: ``SAMPLE`` or ``LEDOIT_WOLF``. Shrinkage is asked for by name, never
    #: applied because a matrix looked awkward.
    covariance_estimator: CovarianceEstimator = CovarianceEstimator.SAMPLE
    constraints: ConstraintsInput | None = None
    #: A stated forecast, one entry per instrument in order. Supplying this is
    #: the only way to get a return-seeking objective without the historical-mean
    #: warning.
    expected_returns: tuple[float, ...] | None = None
    #: Fall back to the sample mean of the estimation window. Off by default:
    #: it has to be asked for, because it is a poor forecast and the optimiser
    #: will maximise its error.
    use_historical_means: bool = False
    risk_aversion: float | None = None
    risk_free_rate: float = 0.0
    confidence: float = 0.95
    #: Black-Litterman inputs. The prior portfolio is required rather than
    #: assumed to be market-cap weighted: the platform holds no market caps.
    prior_weights: tuple[float, ...] | None = None
    tau: float | None = None
    views: tuple[ViewInput, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetHolding:
    instrument_id: uuid.UUID
    symbol: str
    weight: float
    risk_contribution: float

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "symbol": self.symbol,
            "weight": self.weight,
            "risk_contribution": self.risk_contribution,
        }


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    """The risk of the portfolio that was produced, not of the one requested."""

    volatility: float
    #: Historical VaR and expected shortfall of the portfolio's own return
    #: sample — no distributional assumption, the same estimator the risk
    #: domain uses.
    tail: dict
    effective_assets: float
    gross_exposure: float
    net_exposure: float
    largest_weight: float
    largest_risk_contribution: float
    observations: int

    def to_dict(self) -> dict:
        return {
            "volatility": self.volatility,
            "tail": self.tail,
            "effective_assets": self.effective_assets,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "largest_weight": self.largest_weight,
            "largest_risk_contribution": self.largest_risk_contribution,
            "observations": self.observations,
            "interpretation": {
                "effective_assets": (
                    "How many assets the portfolio is genuinely spread across. A "
                    "twenty-name book with an effective count of two holds one bet."
                ),
                "largest_risk_contribution": (
                    "The biggest single share of portfolio variance. Compare it with "
                    "the largest weight: they are often very different numbers."
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class TargetPortfolio:
    """The answer, and everything that shaped it."""

    objective: Objective
    holdings: tuple[TargetHolding, ...]
    risk: PortfolioRisk
    expected_return: float | None
    sharpe: float | None
    return_source: str | None
    covariance: dict
    solver: dict
    black_litterman: dict | None = None
    constraints: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "objective": str(self.objective),
            "holdings": [holding.to_dict() for holding in self.holdings],
            "risk": self.risk.to_dict(),
            "expected_return": self.expected_return,
            "sharpe": self.sharpe,
            "return_source": self.return_source,
            "covariance": self.covariance,
            "solver": self.solver,
            "black_litterman": self.black_litterman,
            "constraints": self.constraints,
        }


def views_from_signals(
    signals: Sequence[dict],
    instrument_ids: Sequence[uuid.UUID],
    return_scale: float,
    uncertainty: float,
) -> tuple[ViewInput, ...]:
    """Turn strategy signals into Black-Litterman views.

    ``return_scale`` is **required and has no default**, and that is the whole
    point of this function. A signal says "hold 40% of the book long"; it does
    not say what return is expected. Converting a target weight into an expected
    return needs a statement of what a full-weight signal is worth, and that
    statement belongs to whoever is modelling — a platform that picked one would
    be inventing the view, not translating it.
    """
    if return_scale <= 0:
        raise OptimisationError(
            "a return scale is required: a target weight is not a return forecast, "
            "and converting one into the other needs a stated conversion"
        )
    views: list[ViewInput] = []
    for signal in signals:
        instrument_id = uuid.UUID(str(signal["instrument_id"]))
        if instrument_id not in instrument_ids:
            continue
        weight = float(signal.get("target_weight") or 0.0)
        if weight == 0:
            continue
        confidence = signal.get("confidence")
        scaled_uncertainty = (
            uncertainty / max(float(confidence), 1e-6) if confidence else uncertainty
        )
        views.append(
            ViewInput(
                weights={instrument_id: 1.0},
                expected_return=weight * return_scale,
                uncertainty=scaled_uncertainty,
                description=str(signal.get("reason") or "from a strategy signal"),
            )
        )
    return tuple(views)


def _solver_constraints(
    stated: ConstraintsInput | None, requested: int, with_history: int
) -> Constraints:
    """Turn stated constraints into the solver's form, at the size that survived.

    An instrument dropped for having no overlapping history moves every index
    after it, so a group limit or a turnover baseline written against the
    requested list no longer means what it said. Rather than silently reindex
    it — which would apply a limit to the wrong instruments — the request is
    refused and the caller is told which instrument to drop.
    """
    if stated is None:
        return Constraints(size=with_history, budget=1.0, long_only=True)
    if requested != with_history:
        raise ConstraintsRefused(
            f"the constraints were written for {requested} instruments and "
            f"{with_history} have overlapping history; the positions they refer to "
            "would no longer be the instruments they were written for"
        )
    try:
        return Constraints(
            size=with_history,
            budget=stated.budget,
            long_only=stated.long_only,
            minimum_weight=stated.minimum_weight,
            maximum_weight=stated.maximum_weight,
            maximum_gross_exposure=stated.maximum_gross_exposure,
            minimum_net_exposure=stated.minimum_net_exposure,
            maximum_net_exposure=stated.maximum_net_exposure,
            groups=tuple(
                GroupLimit(
                    name=group.name,
                    indices=tuple(group.indices),
                    minimum=group.minimum,
                    maximum=group.maximum,
                )
                for group in stated.groups
            ),
            maximum_turnover=stated.maximum_turnover,
            current_weights=(tuple(stated.current_weights) if stated.current_weights else None),
        )
    except (ValueError, InfeasibleProblem) as exc:
        raise ConstraintsRefused(str(exc)) from exc


class PortfolioOptimisationService:
    def __init__(
        self, session: AsyncSession, settings: Settings, object_store: ObjectStore
    ) -> None:
        self._session = session
        self._settings = settings
        self.instruments = InstrumentService(session)
        self.warehouse = WarehouseService(session, settings, object_store)

    async def optimise(
        self, user_id: uuid.UUID, request: OptimisationRequest
    ) -> AnalyticalResult[TargetPortfolio]:
        warnings: list[AnalyticalWarning] = []

        instruments, returns, dropped = await self._returns_matrix(user_id, request)
        if dropped:
            warnings.append(
                AnalyticalWarning.warn(
                    OptimisationWarningCode.INSTRUMENTS_DROPPED,
                    f"{len(dropped)} instrument(s) had no usable history over this window "
                    "and are not in the portfolio. A weight of zero and an absent asset "
                    "are different things, and this is the second.",
                    instruments=[str(item) for item in dropped],
                )
            )
        size = len(instruments)
        if size < 2:
            raise OptimisationError(
                f"at least two instruments with overlapping history are needed; {size} had any"
            )
        if returns.shape[0] < MIN_OBSERVATIONS:
            warnings.append(
                AnalyticalWarning.warn(
                    OptimisationWarningCode.FEW_OBSERVATIONS,
                    f"{returns.shape[0]} aligned observations is too few to estimate a "
                    "covariance worth optimising on; the answer is arithmetic rather "
                    "than an estimate.",
                    observations=returns.shape[0],
                )
            )

        names = tuple(item.symbol for item in instruments)
        estimate = (
            ledoit_wolf_covariance(names, returns)
            if request.covariance_estimator is CovarianceEstimator.LEDOIT_WOLF
            else sample_covariance(names, returns)
        )
        if estimate.shrinkage_intensity:
            warnings.append(
                AnalyticalWarning.info(
                    OptimisationWarningCode.SHRINKAGE_APPLIED,
                    f"the covariance was shrunk with intensity "
                    f"{estimate.shrinkage_intensity:.3f} towards a constant-correlation "
                    "target, which was asked for rather than applied automatically.",
                    intensity=estimate.shrinkage_intensity,
                )
            )

        constraints = _solver_constraints(request.constraints, len(request.instrument_ids), size)

        expected, posterior, covariance = self._expected_returns(
            request, estimate.covariance, estimate.mean, size, warnings
        )

        try:
            result, cvar = self._run(request, covariance, expected, constraints, returns)
        except InfeasibleProblem as exc:
            raise OptimisationError(str(exc)) from exc
        except (OptimisationFailed, CVaRFailed) as exc:
            raise OptimisationError(str(exc)) from exc

        weights = result.weights if result is not None else cvar.weights
        contributions = (
            result.risk_contributions if result is not None else _contributions(weights, covariance)
        )
        portfolio_returns = returns @ weights

        risk = PortfolioRisk(
            volatility=float(np.sqrt(max(weights @ covariance @ weights, 0.0))),
            tail=historical_tail_risk(
                losses_from_pnl(portfolio_returns), request.confidence
            ).to_dict(),
            effective_assets=(
                result.effective_assets if result is not None else _effective(weights)
            ),
            gross_exposure=float(np.sum(np.abs(weights))),
            net_exposure=float(np.sum(weights)),
            largest_weight=float(np.max(np.abs(weights))) if size else 0.0,
            largest_risk_contribution=float(np.max(contributions)) if size else 0.0,
            observations=returns.shape[0],
        )

        target = TargetPortfolio(
            objective=request.objective,
            holdings=tuple(
                TargetHolding(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    weight=float(weights[index]),
                    risk_contribution=float(contributions[index]),
                )
                for index, instrument in enumerate(instruments)
            ),
            risk=risk,
            expected_return=(
                result.expected_return if result is not None else cvar.expected_return
            ),
            sharpe=result.sharpe if result is not None else None,
            return_source=(str(expected.source) if expected is not None else None),
            covariance=estimate.to_dict(),
            solver=result.to_dict() if result is not None else cvar.to_dict(),
            black_litterman=posterior.to_dict() if posterior is not None else None,
            constraints=constraints.to_dict(),
        )

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            parameters={
                "objective": str(request.objective),
                "covariance_estimator": str(request.covariance_estimator),
                "observations": returns.shape[0],
                "instruments": [str(item.id) for item in instruments],
                "risk_free_rate": request.risk_free_rate,
                "risk_aversion": request.risk_aversion,
                "return_source": str(expected.source) if expected else None,
            },
        )
        return AnalyticalResult.ok(target, provenance, tuple(warnings))

    # ------------------------------------------------------------ internals
    def _expected_returns(
        self,
        request: OptimisationRequest,
        covariance: np.ndarray,
        sample_mean: np.ndarray,
        size: int,
        warnings: list[AnalyticalWarning],
    ):
        """Decide what the forecast is, and say where it came from."""
        needs_returns = request.objective in {
            Objective.MAXIMUM_SHARPE,
            Objective.MEAN_VARIANCE,
        }

        if request.prior_weights is not None:
            if request.tau is None or request.risk_aversion is None:
                raise OptimisationError(
                    "Black-Litterman needs both tau and a risk aversion; neither has a "
                    "consensus value and the platform will not choose one"
                )
            prior = np.asarray(request.prior_weights, dtype=float)
            if len(prior) != size:
                raise OptimisationError("prior_weights does not match the asset count")
            views = tuple(
                bl.View(
                    weights={
                        index: weight
                        for index, weight in enumerate(_view_row(view, size, request))
                        if weight != 0
                    },
                    expected_return=view.expected_return,
                    uncertainty=view.uncertainty,
                    description=view.description,
                )
                for view in request.views
            )
            posterior = bl.blend(covariance, prior, views, request.risk_aversion, request.tau)
            return (
                ExpectedReturns(
                    posterior.posterior_returns,
                    ReturnSource.BLACK_LITTERMAN if views else ReturnSource.EQUILIBRIUM,
                    f"blended from a supplied prior with {len(views)} view(s)",
                ),
                posterior,
                posterior.posterior_covariance,
            )

        if request.expected_returns is not None:
            values = np.asarray(request.expected_returns, dtype=float)
            if len(values) != size:
                raise OptimisationError("expected_returns does not match the asset count")
            return (
                ExpectedReturns(values, ReturnSource.SUPPLIED, "stated by the caller"),
                None,
                covariance,
            )

        if request.use_historical_means:
            warnings.append(
                AnalyticalWarning.warn(
                    OptimisationWarningCode.HISTORICAL_MEANS_USED,
                    "expected returns are the sample means of the estimation window. "
                    "Historical means are a poor forecast of future means, and a "
                    "mean-variance optimiser puts the most weight exactly where that "
                    "error is largest. Treat the weights as an illustration.",
                )
            )
            return (
                ExpectedReturns(
                    sample_mean,
                    ReturnSource.HISTORICAL_MEAN,
                    "sample mean of the estimation window",
                ),
                None,
                covariance,
            )

        if needs_returns:
            raise OptimisationError(
                f"{request.objective} needs expected returns. Supply them, supply a "
                "Black-Litterman prior and views, or ask for historical means "
                "explicitly — the platform will not estimate a forecast you did not "
                "ask for. Minimum variance and risk parity need none."
            )

        warnings.append(
            AnalyticalWarning.info(
                OptimisationWarningCode.NO_EXPECTED_RETURNS,
                f"{request.objective} needs no return forecast, so none was used and "
                "none is reported.",
            )
        )
        return None, None, covariance

    def _run(self, request, covariance, expected, constraints, returns):
        objective = request.objective
        if objective is Objective.MINIMUM_VARIANCE:
            return minimum_variance(covariance, constraints, expected, request.risk_free_rate), None
        if objective is Objective.RISK_PARITY:
            return risk_parity(covariance, constraints, expected, request.risk_free_rate), None
        if objective is Objective.MAXIMUM_SHARPE:
            return maximum_sharpe(covariance, expected, constraints, request.risk_free_rate), None
        if objective is Objective.MEAN_VARIANCE:
            if request.risk_aversion is None:
                raise OptimisationError(
                    "mean-variance needs a risk aversion. It is a statement about a "
                    "person's tolerance rather than a property of the market, and the "
                    "platform will not choose one on the user's behalf."
                )
            return mean_variance(
                covariance,
                expected,
                constraints,
                request.risk_aversion,
                request.risk_free_rate,
            ), None
        if objective is Objective.MINIMUM_CVAR:
            return None, minimum_cvar(
                returns,
                constraints,
                request.confidence,
                expected.values if expected is not None else None,
            )
        raise OptimisationError(f"unknown objective {objective}")

    async def _returns_matrix(self, user_id: uuid.UUID, request: OptimisationRequest):
        """Aligned daily returns for the requested instruments.

        Alignment is an inner join on timestamp. An instrument whose history does
        not overlap the others is **dropped and reported** rather than
        forward-filled: a filled return is an invented observation, and a
        covariance estimated from invented observations understates every
        correlation it touches.
        """
        series: dict[uuid.UUID, dict[datetime, float]] = {}
        instruments = []
        dropped: list[uuid.UUID] = []

        for instrument_id in request.instrument_ids:
            instrument = await self.instruments.get(instrument_id)
            if instrument is None:
                dropped.append(instrument_id)
                continue

            result = await self.warehouse.run_query(
                user_id,
                QueryRequest(
                    layer=DatasetLayer.NORMALIZED,
                    kind=DatasetKind.BARS,
                    exchange=request.exchange,
                    instrument_ids=(instrument_id,),
                    start=request.start,
                    end=request.end,
                    columns=("exchange_timestamp", "close"),
                ),
            )
            rows = result.table.to_pylist() if result.rows else []
            if len(rows) < 2:
                dropped.append(instrument_id)
                continue

            ordered = sorted(rows, key=lambda row: row["exchange_timestamp"])
            closes = [float(Decimal(str(row["close"]))) for row in ordered]
            stamps = [row["exchange_timestamp"] for row in ordered]
            series[instrument_id] = {
                stamps[index]: closes[index] / closes[index - 1] - 1.0
                for index in range(1, len(closes))
                if closes[index - 1] > 0
            }
            instruments.append(instrument)

        if not series:
            return [], np.zeros((0, 0)), dropped

        common = set.intersection(*(set(values) for values in series.values()))
        if len(common) < 2:
            return [], np.zeros((0, 0)), dropped + [item.id for item in instruments]

        stamps = sorted(common)
        matrix = np.array(
            [[series[item.id][stamp] for item in instruments] for stamp in stamps],
            dtype=float,
        )
        return instruments, matrix, dropped


def _view_row(view: ViewInput, size: int, request: OptimisationRequest) -> list[float]:
    row = [0.0] * size
    for instrument_id, weight in view.weights.items():
        if instrument_id in request.instrument_ids:
            row[request.instrument_ids.index(instrument_id)] = float(weight)
    return row


def _contributions(weights: np.ndarray, covariance: np.ndarray) -> np.ndarray:
    variance = float(weights @ covariance @ weights)
    if variance <= 0:
        return np.zeros_like(weights)
    return (weights * (covariance @ weights)) / variance


def _effective(weights: np.ndarray) -> float:
    gross = float(np.sum(np.abs(weights)))
    if gross <= 0:
        return 0.0
    shares = np.abs(weights) / gross
    concentration = float(np.sum(shares**2))
    return 1.0 / concentration if concentration > 0 else 0.0
