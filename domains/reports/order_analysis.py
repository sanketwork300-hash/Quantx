"""Unified order analysis: five engines, one snapshot, no recommendation.

Phase 11, and the reason the previous ten were built the way they were.

A user asking "what happens if I sell five thousand of these" is asking five
questions at once — what is it worth, is its volatility out of line, what will
it cost to execute, what does it do to my risk, what does it do to my margin —
and the answers are only comparable if they were all computed from the same
market. So this module builds **one** ``MarketState``, values the proposed
position inside it, and hands that same state to every branch. Every branch's
provenance names the same ``market_state_id``, and a test asserts it: the delta
a user reads between "current" and "proposed" is then attributable to their
order rather than to five calculations catching the market at five moments.

Branches degrade independently and are *never* silently dropped. If the surface
calibration failed, that branch is ``FAILED`` with the reason, the other four
still answer, and the analysis as a whole is ``PARTIAL``. A missing branch is
always a named absence.

**There is no recommendation field, and there is nowhere for one to go.** No
action, no signal, no rating, no score, no ranking of the execution schedules.
The response is a set of measurements and model estimates, each labelled as one
or the other; what to do about them is the reader's judgement, and the platform
is not in a position to make it. ``domains.reports`` composes; it does not
advise, and it does not calculate — every number below is produced by the
engine that owns it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.advanced import AdvancedDerivativesService, ContractConsensusParams
from domains.derivatives.anomaly import ANOMALY_MODEL_VERSION, AnomalyPolicy
from domains.derivatives.application import DerivativesService
from domains.derivatives.consensus import CONSENSUS_MODEL_VERSION
from domains.execution.application import ExecutionApplicationService
from domains.execution.models import OrderType, Side
from domains.execution.orders import IMMEDIATE, OrderCostError, OrderCostRequest
from domains.instruments.enums import AssetClass
from domains.instruments.service import InstrumentService
from domains.market_data.service import MarketDataService
from domains.portfolio.application import PortfolioApplicationService, ValuePortfolioParams
from domains.portfolio.service import PortfolioNotFound
from domains.reports.composition import FactorHistoryComposer, ValuationContextComposer
from domains.reports.envelope import AnalyticalResult, ResultStatus
from domains.reports.provenance import Provenance
from domains.reports.repository import ReportsRepository
from domains.reports.warnings import AnalyticalWarning
from domains.risk.application import MarginRunParams, RiskApplicationService, RiskRunParams
from domains.risk.var import VaRMethod
from domains.scenarios.service import ScenarioNotFound, ScenarioService
from infrastructure.settings import Settings
from infrastructure.storage.base import ObjectStore

ORDER_ANALYSIS_MODEL_VERSION = "order-analysis@1.0.0"


class Branch(StrEnum):
    """The five questions. Fixed, so a missing one is visible as a missing one."""

    VALUATION = "VALUATION"
    SURFACE = "SURFACE"
    EXECUTION = "EXECUTION"
    RISK = "RISK"
    MARGIN = "MARGIN"


class OrderAnalysisWarning:
    NO_MARKET_DATA = "ORDER_ANALYSIS_NO_MARKET_DATA"
    BRANCH_FAILED = "ORDER_ANALYSIS_BRANCH_FAILED"
    BRANCH_RAISED = "ORDER_ANALYSIS_BRANCH_RAISED"
    NOT_AN_OPTION = "ORDER_ANALYSIS_NOT_A_VANILLA_OPTION"
    NO_SURFACE = "ORDER_ANALYSIS_NO_CALIBRATED_SURFACE"
    NO_SPOT = "ORDER_ANALYSIS_NO_UNDERLYING_LEVEL"
    NO_TIME_TO_EXPIRY = "ORDER_ANALYSIS_NO_TIME_TO_EXPIRY"
    NO_OBSERVED_MARKET = "ORDER_ANALYSIS_NO_OBSERVED_TWO_SIDED_MARKET"


class OrderAnalysisError(Exception):
    """The request cannot be set up. A 4xx, not a degraded result."""


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    """What the caller must supply before an execution cost can be estimated.

    The platform holds no average daily volume and no intraday volume profile,
    so these are inputs rather than lookups. Left at their defaults, the impact
    half of the estimate reports itself absent instead of being invented.
    """

    horizon_seconds: float = 1800.0
    intervals: int = 6
    #: POV and VWAP are not defaults: both need a volume forecast the platform
    #: does not hold, so asking for them unprompted would guarantee a refusal.
    strategies: tuple[str, ...] = (IMMEDIATE, "TWAP")
    impact_model: str = "SquareRootImpactModel"
    permanent_coefficient: float = 1.0
    temporary_coefficient: float = 1.0
    volatility: float = 0.0
    average_daily_volume: float = 0.0
    participation_rate: float = 0.10
    expected_volumes: tuple[float, ...] | None = None

    def to_provenance(self) -> dict:
        return {
            "horizon_seconds": self.horizon_seconds,
            "intervals": self.intervals,
            "strategies": list(self.strategies),
            "impact_model": self.impact_model,
            "permanent_coefficient": self.permanent_coefficient,
            "temporary_coefficient": self.temporary_coefficient,
            "volatility": self.volatility,
            "average_daily_volume": self.average_daily_volume,
            "participation_rate": self.participation_rate,
            "expected_volumes": (
                list(self.expected_volumes) if self.expected_volumes is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class OrderAnalysisRequest:
    """One proposed order and every parameter that decides the answer."""

    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    side: Side
    #: A magnitude. The side carries the sign, in one place.
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None

    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    settlement_time_utc: time | None = None
    as_of: datetime | None = None

    var_method: VaRMethod = VaRMethod.HISTORICAL
    scenario: str | None = None
    lookback: int | None = None
    horizon_days: int = 1
    seed: int = 20_260_924

    execution: ExecutionAssumptions = field(default_factory=ExecutionAssumptions)
    margin: MarginRunParams = field(default_factory=MarginRunParams)
    anomaly_policy: AnomalyPolicy = field(default_factory=AnomalyPolicy)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise OrderAnalysisError("an order quantity is a magnitude; the side carries the sign")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise OrderAnalysisError("a limit order without a limit price is not a limit order")

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side is Side.BUY else -self.quantity

    def to_risk_params(self) -> RiskRunParams:
        return RiskRunParams(
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            settlement_time_utc=self.settlement_time_utc,
            as_of=self.as_of,
            lookback=self.lookback,
            horizon_days=self.horizon_days,
            seed=self.seed,
        )

    def to_valuation_params(self) -> ValuePortfolioParams:
        return ValuePortfolioParams(
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            settlement_time_utc=self.settlement_time_utc,
            as_of=self.as_of,
        )

    def to_provenance(self) -> dict:
        return {
            "portfolio_id": str(self.portfolio_id),
            "instrument_id": str(self.instrument_id),
            "side": str(self.side),
            "quantity": format(self.quantity, "f"),
            "order_type": str(self.order_type),
            "limit_price": format(self.limit_price, "f") if self.limit_price else None,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "settlement_time_utc": (
                self.settlement_time_utc.isoformat() if self.settlement_time_utc else None
            ),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "var_method": str(self.var_method),
            "scenario": self.scenario,
            "lookback": self.lookback,
            "horizon_days": self.horizon_days,
            "seed": self.seed,
            "execution": self.execution.to_provenance(),
            "margin": self.margin.to_provenance(),
            "anomaly_policy": self.anomaly_policy.to_provenance(),
        }


@dataclass(frozen=True, slots=True)
class BranchOutcome:
    """One engine's answer, or the stated reason there is not one."""

    branch: Branch
    result: AnalyticalResult[dict]

    @property
    def ok(self) -> bool:
        return self.result.status is not ResultStatus.FAILED

    @property
    def market_state_id(self) -> str | None:
        return self.result.provenance.market_state_id

    def to_dict(self) -> dict:
        payload = self.result.to_dict()
        payload["branch"] = str(self.branch)
        return payload


@dataclass(frozen=True, slots=True)
class OrderAnalysis:
    """Five branches over one snapshot, and nothing that reads as advice."""

    order: dict
    market_state: dict
    branches: tuple[BranchOutcome, ...]

    @property
    def status(self) -> ResultStatus:
        if not any(branch.ok for branch in self.branches):
            return ResultStatus.FAILED
        if all(branch.ok for branch in self.branches):
            return ResultStatus.OK
        return ResultStatus.PARTIAL

    @property
    def branches_ok(self) -> int:
        return sum(1 for branch in self.branches if branch.ok)

    @property
    def branches_failed(self) -> int:
        return len(self.branches) - self.branches_ok

    @property
    def market_state_ids(self) -> set[str]:
        """Every snapshot id any branch claims. One element, or a bug."""
        return {
            branch.market_state_id for branch in self.branches if branch.market_state_id is not None
        }

    def warnings(self) -> tuple[AnalyticalWarning, ...]:
        merged: list[AnalyticalWarning] = []
        for branch in self.branches:
            if not branch.ok:
                merged.append(
                    AnalyticalWarning.warn(
                        OrderAnalysisWarning.BRANCH_FAILED,
                        f"The {branch.branch} branch produced nothing. The rest of "
                        "this analysis stands; this part of it is absent, with the "
                        "reason on the branch itself.",
                        branch=str(branch.branch),
                    )
                )
            merged.extend(branch.result.warnings)
        return tuple(merged)

    def to_dict(self) -> dict:
        return {
            "model_version": ORDER_ANALYSIS_MODEL_VERSION,
            "order": self.order,
            "market_state": self.market_state,
            # Published rather than merely true: a reader can check the
            # phase's central claim without trusting this module's word for
            # it. More than one element here is a bug, not a degraded result.
            "market_state_ids": sorted(self.market_state_ids),
            "branch_status": {str(b.branch): str(b.result.status) for b in self.branches},
            "branches": {str(b.branch): b.to_dict() for b in self.branches},
            "counts": {"ok": self.branches_ok, "failed": self.branches_failed},
            "interpretation": (
                "Five analyses of one proposed order, all computed from the single "
                "market snapshot named in every provenance block below, so the "
                "differences shown between the current and the proposed book are "
                "attributable to the order and not to the market moving between "
                "calculations. Reference values are model output and are not what "
                "the contract is worth; the execution figures are estimates under "
                "a stated impact model; the margin figures are this platform's own "
                "model and are not any broker's number. Nothing here is a "
                "recommendation to trade or not to trade."
            ),
        }


class OrderAnalysisService:
    """The one place in the platform permitted to fan out across all five engines."""

    def __init__(
        self, session: AsyncSession, settings: Settings, object_store: ObjectStore
    ) -> None:
        self._session = session
        self._settings = settings
        self.repository = ReportsRepository(session)
        self._instruments = InstrumentService(session)
        self._market_data = MarketDataService(session, settings, object_store)
        self._derivatives = DerivativesService(session, settings)
        self._advanced = AdvancedDerivativesService(session, settings)
        self._portfolios = PortfolioApplicationService(session, settings)
        self._risk = RiskApplicationService(session, settings)
        self._execution = ExecutionApplicationService(session, settings)
        self._scenarios = ScenarioService(session)
        self._valuation_composer = ValuationContextComposer(self._market_data, self._derivatives)
        self._history_composer = FactorHistoryComposer(self._market_data, self._derivatives)

    # ------------------------------------------------------------------ entry
    async def analyse(
        self, user_id: uuid.UUID, request: OrderAnalysisRequest
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        instrument = await self._instruments.get(request.instrument_id)
        if instrument is None:
            raise OrderAnalysisError(f"instrument {request.instrument_id} is not in the master")

        underlying_id = instrument.underlying_id or instrument.id

        # A portfolio that is not this caller's raises ``PortfolioNotFound``
        # from here, and it is left to propagate: that is a 404, not a branch
        # that could not compute.
        computed = await self._portfolios.compute_valuation(
            user_id,
            request.portfolio_id,
            request.to_valuation_params(),
            self._valuation_composer,
            extra_underlyings={underlying_id},
        )
        if computed is None:
            return await self._nothing_to_analyse(user_id, request, instrument)

        context = computed.context
        state = context.market_state
        proposed = await self._portfolios.value_proposed_position(
            request.portfolio_id, instrument.id, request.signed_quantity, context
        )
        quote = state.quotes.get(instrument.id)
        quality = state.quality.get(instrument.id)
        surface = context.surfaces.get(underlying_id)

        book = self._risk.incremental_book(computed, proposed)

        branches = (
            await self._guard(
                Branch.VALUATION,
                state,
                lambda: self._valuation_branch(
                    user_id, request, instrument, underlying_id, context, proposed, quote, surface
                ),
            ),
            await self._guard(
                Branch.SURFACE,
                state,
                lambda: self._surface_branch(request, instrument, quote, quality, surface, state),
            ),
            await self._guard(
                Branch.EXECUTION,
                state,
                lambda: self._execution_branch(request, instrument, context, quote),
            ),
            await self._guard(
                Branch.RISK,
                state,
                lambda: self._risk_branch(user_id, request, computed, book),
            ),
            await self._guard(
                Branch.MARGIN,
                state,
                lambda: self._margin_branch(request, computed, book),
            ),
        )

        analysis = OrderAnalysis(
            order=self._order_payload(request, instrument, proposed),
            market_state=state.to_dict(),
            branches=branches,
        )
        provenance = self._provenance(request, state)
        row = await self._persist(user_id, request, instrument, analysis, provenance, context)

        payload = analysis.to_dict()
        payload["order_analysis_id"] = str(row.id)
        return (
            AnalyticalResult(
                status=analysis.status,
                results=payload,
                provenance=provenance,
                warnings=analysis.warnings(),
            ),
            row.id,
        )

    # --------------------------------------------------------------- branches
    async def _valuation_branch(
        self,
        user_id: uuid.UUID,
        request: OrderAnalysisRequest,
        instrument,
        underlying_id: uuid.UUID,
        context,
        proposed,
        quote,
        surface,
    ) -> AnalyticalResult[dict]:
        """The observed market, and a reference range across models around it."""
        state = context.market_state
        position = proposed.position_valuation

        if instrument.asset_class is not AssetClass.OPTION or instrument.strike is None:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NOT_AN_OPTION,
                f"Model consensus prices vanilla options; {instrument.symbol} is a "
                f"{instrument.asset_class}. The observed market and the position "
                "valuation in this branch's results still stand; there is no "
                "model reference range around them.",
                {"consensus": None, "position": position.to_dict()},
            )
        if surface is None:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NO_SURFACE,
                "No calibrated volatility surface is available for this "
                "underlying, so no model reference value could be produced. "
                "Calibrate one from an ingested chain first.",
                {"consensus": None, "position": position.to_dict()},
            )
        spot = state.spot_prices.get(underlying_id)
        if spot is None or spot <= 0:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NO_SPOT,
                "The snapshot carries no level for this underlying, and every "
                "model here needs one.",
                {"consensus": None, "position": position.to_dict()},
            )

        reference = surface.reference(instrument.strike, instrument.expiry, instrument.option_type)
        tau = reference.time_to_expiry or position.time_to_expiry
        if tau is None or tau <= 0:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NO_TIME_TO_EXPIRY,
                "Time to expiry is undefined for this contract — supply a "
                "settlement time, or the contract has expired — so no model can "
                "price it.",
                {"consensus": None, "position": position.to_dict()},
            )

        consensus = await self._advanced.contract_consensus(
            user_id,
            instrument,
            ContractConsensusParams(
                spot=float(spot),
                tau=tau,
                rate=request.risk_free_rate,
                dividend=request.dividend_yield,
                reference=reference,
                surface_id=surface.surface_id,
                slice_fit=surface.slice_for(instrument.expiry),
                # The observed mid only. ``Quote.mid_price`` is ``None`` on a
                # one-sided market rather than falling back to a print, so the
                # deviation between the models and the market is absent rather
                # than measured against something that is not a market.
                market_price=(float(quote.mid_price) if quote and quote.mid_price else None),
                seed=request.seed,
            ),
        )
        payload = {
            "observed": _observed(quote, state.as_of, state.quality.get(instrument.id)),
            "position": position.to_dict(),
            "consensus": consensus.to_dict(),
        }
        return AnalyticalResult.ok(
            payload,
            self._branch_provenance(
                request,
                state,
                {"consensus": CONSENSUS_MODEL_VERSION, "surface": surface.model_version},
                surface_id=surface.surface_id,
            ),
            consensus.warnings,
        )

    async def _surface_branch(
        self, request: OrderAnalysisRequest, instrument, quote, quality, surface, state
    ) -> AnalyticalResult[dict]:
        """This contract's observed volatility against the fitted surface."""
        if instrument.asset_class is not AssetClass.OPTION or instrument.strike is None:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NOT_AN_OPTION,
                "A surface deviation is a statement about an option's implied "
                f"volatility; {instrument.symbol} is a {instrument.asset_class}.",
                None,
            )
        if surface is None:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NO_SURFACE,
                "No calibrated volatility surface is available for this "
                "underlying, so there is nothing to measure the market against.",
                None,
            )

        deviation = await self._derivatives.contract_deviation(
            instrument, quote, quality, surface, request.anomaly_policy
        )
        provenance = self._branch_provenance(
            request,
            state,
            {"anomaly": ANOMALY_MODEL_VERSION, "surface": surface.model_version},
            surface_id=surface.surface_id,
        )
        payload = {"observed": _observed(quote, state.as_of, quality), **deviation.to_dict()}
        if not deviation.ok:
            # The branch's whole output is the deviation. Without one there is
            # no partial answer to give, and reporting a difference of zero
            # would be a claim that the market agrees with the surface.
            return AnalyticalResult(
                status=ResultStatus.FAILED,
                results=payload,
                provenance=provenance,
                warnings=deviation.warnings,
            )
        return AnalyticalResult.ok(payload, provenance, deviation.warnings)

    async def _execution_branch(
        self, request: OrderAnalysisRequest, instrument, context, quote
    ) -> AnalyticalResult[dict]:
        """Estimated slippage for this order, under a stated impact model."""
        state = context.market_state
        mid = quote.mid_price if quote is not None else None
        if mid is None:
            return self._branch_failure(
                state,
                request,
                OrderAnalysisWarning.NO_OBSERVED_MARKET,
                "There is no two-sided observed market for this contract in the "
                "snapshot, so there is no reference price to estimate slippage "
                "against. A last-trade print is not substituted for one.",
                None,
            )

        assumptions = request.execution
        try:
            cost_request = OrderCostRequest(
                instrument_id=instrument.id,
                side=request.side,
                quantity=request.quantity,
                multiplier=instrument.multiplier,
                currency=instrument.currency,
                reference_price=mid,
                as_of=state.as_of,
                bid=quote.bid_price,
                ask=quote.ask_price,
                order_type=request.order_type,
                limit_price=request.limit_price,
                lot_size=instrument.lot_size,
                horizon_seconds=assumptions.horizon_seconds,
                intervals=assumptions.intervals,
                strategies=assumptions.strategies,
                impact_model=assumptions.impact_model,
                volatility=assumptions.volatility,
                average_daily_volume=assumptions.average_daily_volume,
                participation_rate=assumptions.participation_rate,
                expected_volumes=assumptions.expected_volumes,
            )
        except OrderCostError as exc:
            raise OrderAnalysisError(str(exc)) from exc

        estimate, warnings = self._execution.estimate_order_cost(
            cost_request,
            permanent_coefficient=assumptions.permanent_coefficient,
            temporary_coefficient=assumptions.temporary_coefficient,
        )
        return AnalyticalResult.ok(
            {
                "observed": _observed(quote, state.as_of, state.quality.get(instrument.id)),
                **estimate.to_dict(),
            },
            self._branch_provenance(
                request,
                state,
                {"order_cost": estimate.model_version, "impact": assumptions.impact_model},
            ),
            warnings,
        )

    async def _risk_branch(
        self, user_id: uuid.UUID, request: OrderAnalysisRequest, computed, book
    ) -> AnalyticalResult[dict]:
        scenario = None
        if request.scenario:
            try:
                scenario = await self._scenarios.resolve(user_id, request.scenario)
            except ScenarioNotFound as exc:
                raise OrderAnalysisError(f"scenario {request.scenario!r} not found") from exc
        return await self._risk.run_incremental_risk(
            user_id,
            request.portfolio_id,
            computed,
            book,
            request.to_risk_params(),
            request.var_method,
            self._history_composer,
            scenario=scenario,
        )

    async def _margin_branch(
        self, request: OrderAnalysisRequest, computed, book
    ) -> AnalyticalResult[dict]:
        return self._risk.run_incremental_margin(
            request.portfolio_id, computed, book, request.to_risk_params(), request.margin
        )

    # ---------------------------------------------------------------- support
    async def _guard(self, branch: Branch, state, factory) -> BranchOutcome:
        """Run one branch. A branch that raises is a failed branch, not a 500.

        The five engines fail in five vocabularies, and a unified analysis that
        turned any one of them into a 500 would take the other four down with
        it. What must *not* be caught is a set-up error — a portfolio that is
        not the caller's, an instrument that does not exist — which is a bad
        request rather than a branch that could not compute.
        """
        try:
            return BranchOutcome(branch, await factory())
        except (OrderAnalysisError, PortfolioNotFound):
            raise
        except Exception as exc:  # noqa: BLE001 - a branch failure is a result
            return BranchOutcome(
                branch,
                AnalyticalResult.failed(
                    self._bare_provenance(state),
                    (
                        AnalyticalWarning.error(
                            OrderAnalysisWarning.BRANCH_RAISED,
                            f"The {branch} branch could not be computed: "
                            f"{type(exc).__name__}: {exc}",
                            branch=str(branch),
                        ),
                    ),
                ),
            )

    def _branch_failure(
        self, state, request: OrderAnalysisRequest, code: str, message: str, payload: dict | None
    ) -> AnalyticalResult[dict]:
        """A branch that could not answer, with what it could still say attached."""
        provenance = self._branch_provenance(request, state, {})
        return AnalyticalResult(
            status=ResultStatus.FAILED,
            results=payload,
            provenance=provenance,
            warnings=(AnalyticalWarning.error(code, message),),
        )

    def _branch_provenance(
        self,
        request: OrderAnalysisRequest,
        state,
        model_versions: dict,
        surface_id: str | None = None,
    ) -> Provenance:
        return Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_id=state.state_id,
            market_state_timestamp=state.as_of,
            market_data_sources=state.sources,
            dataset_versions=dict(state.data_versions),
            surface_id=surface_id,
            model_versions={"order_analysis": ORDER_ANALYSIS_MODEL_VERSION, **model_versions},
            parameters=request.to_provenance(),
        )

    def _bare_provenance(self, state) -> Provenance:
        return Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_id=state.state_id if state is not None else None,
            market_state_timestamp=state.as_of if state is not None else None,
            model_versions={"order_analysis": ORDER_ANALYSIS_MODEL_VERSION},
        )

    def _provenance(self, request: OrderAnalysisRequest, state) -> Provenance:
        return self._branch_provenance(request, state, {})

    @staticmethod
    def _order_payload(request: OrderAnalysisRequest, instrument, proposed) -> dict:
        return {
            "portfolio_id": str(request.portfolio_id),
            "instrument_id": str(instrument.id),
            "canonical_key": instrument.canonical_key,
            "asset_class": str(instrument.asset_class),
            "side": str(request.side),
            "quantity": format(request.quantity, "f"),
            "signed_quantity": format(request.signed_quantity, "f"),
            "order_type": str(request.order_type),
            "limit_price": (
                format(request.limit_price, "f") if request.limit_price is not None else None
            ),
            "multiplier": format(instrument.multiplier, "f"),
            "currency": instrument.currency,
            "proposed_position_id": str(proposed.position.id),
        }

    async def _nothing_to_analyse(
        self, user_id: uuid.UUID, request: OrderAnalysisRequest, instrument
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            model_versions={"order_analysis": ORDER_ANALYSIS_MODEL_VERSION},
            parameters=request.to_provenance(),
        )
        result: AnalyticalResult[dict] = AnalyticalResult.failed(
            provenance,
            (
                AnalyticalWarning.error(
                    OrderAnalysisWarning.NO_MARKET_DATA,
                    "No market data is available for any underlying this portfolio "
                    "or this order touches, so there is no snapshot to analyse "
                    "either of them against.",
                ),
            ),
        )
        row = await self.repository.create_order_analysis(
            user_id=user_id,
            portfolio_id=request.portfolio_id,
            instrument_id=instrument.id,
            side=str(request.side),
            quantity=request.quantity,
            order_type=str(request.order_type),
            limit_price=request.limit_price,
            as_of_timestamp=request.as_of,
            market_state_id=None,
            base_currency=None,
            status=str(ResultStatus.FAILED),
            branches_ok=0,
            branches_failed=len(Branch),
            branch_status={str(branch): str(ResultStatus.FAILED) for branch in Branch},
            results={},
            warnings=[warning.to_dict() for warning in result.warnings],
            provenance=provenance.to_dict(),
        )
        return result, row.id

    async def _persist(
        self,
        user_id: uuid.UUID,
        request: OrderAnalysisRequest,
        instrument,
        analysis: OrderAnalysis,
        provenance: Provenance,
        context,
    ):
        return await self.repository.create_order_analysis(
            user_id=user_id,
            portfolio_id=request.portfolio_id,
            instrument_id=instrument.id,
            side=str(request.side),
            quantity=request.quantity,
            order_type=str(request.order_type),
            limit_price=request.limit_price,
            as_of_timestamp=context.as_of,
            market_state_id=context.market_state.state_id,
            base_currency=context.base_currency,
            status=str(analysis.status),
            branches_ok=analysis.branches_ok,
            branches_failed=analysis.branches_failed,
            branch_status={str(b.branch): str(b.result.status) for b in analysis.branches},
            results=analysis.to_dict(),
            warnings=[warning.to_dict() for warning in analysis.warnings()],
            provenance=provenance.to_dict(),
        )

    # ------------------------------------------------------------------ reads
    async def get(self, analysis_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_order_analysis(analysis_id, user_id)

    async def list(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        return await self.repository.list_order_analyses(
            user_id, portfolio_id=portfolio_id, limit=limit, offset=offset
        )


def _observed(quote, as_of: datetime, quality) -> dict:
    """The market as the snapshot holds it. Observations only, never estimates."""
    if quote is None:
        return {"available": False, "reason": "the snapshot holds no quote for this contract"}
    return {
        "available": True,
        "bid": format(quote.bid_price, "f") if quote.bid_price is not None else None,
        "ask": format(quote.ask_price, "f") if quote.ask_price is not None else None,
        "mid": format(quote.mid_price, "f") if quote.mid_price is not None else None,
        "last": format(quote.last_price, "f") if quote.last_price is not None else None,
        "spread": format(quote.spread, "f") if quote.spread is not None else None,
        "exchange_timestamp": quote.exchange_timestamp.isoformat(),
        "age_seconds": quote.age_seconds(as_of),
        "source": quote.source,
        "quality": quality.to_dict() if quality is not None else None,
        "note": (
            "Observations. The mid is absent rather than substituted from the "
            "last trade when the market is one-sided."
        ),
    }
