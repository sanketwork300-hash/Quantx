"""Risk application service: snapshot, VaR, stress, persistence.

Every risk run starts by valuing the portfolio through the *same* Phase 4 code
path the valuation endpoint uses, and stores that valuation. The risk snapshot
then points at it. That is what makes a VaR number auditable: the chain from it
back to the individual quotes is a sequence of foreign keys, not an assertion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from domains.portfolio.application import (
    ComputedValuation,
    PortfolioApplicationService,
    ProposedPositionValuation,
    ValuePortfolioParams,
)
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from domains.risk.exposure import ExposureSet, build_exposures
from domains.risk.factors import AlignmentPolicy, FactorPanel, FactorSeries, build_panel
from domains.risk.incremental import (
    INCREMENTAL_MODEL_VERSION,
    CombinedBook,
    IncrementalGreeks,
    IncrementalWarning,
    combine,
    greeks_of,
    incremental_margin,
    incremental_stress,
    incremental_var,
)
from domains.risk.margin import MarginParameters, ShockGrid, build_model
from domains.risk.repository import RiskRepository
from domains.risk.stress import ContributionDimension, apply_scenario
from domains.risk.var import (
    DEFAULT_CONFIDENCES,
    VaRMethod,
    historical_var,
    monte_carlo_var,
    parametric_var,
)
from domains.risk.vulnerability import DEFAULT_LADDER, scan_vulnerability
from domains.scenarios.models import Scenario
from infrastructure.settings import Settings
from quant.simulation.paths import Distribution

RISK_MODEL_VERSION = "portfolio-risk@1.0.0"
STRESS_MODEL_VERSION = "scenario-stress@1.0.0"
DEFAULT_MARGIN_MODEL = "SimpleRiskMarginModel"


class RiskError(Exception):
    pass


class RiskWarningCode:
    NO_MARKET_DATA = "RISK_NO_MARKET_DATA"
    NO_EXPOSURES = "RISK_NO_REPRICEABLE_POSITIONS"
    INSUFFICIENT_HISTORY = "RISK_INSUFFICIENT_HISTORY"
    POSITIONS_EXCLUDED = "RISK_POSITIONS_EXCLUDED"
    REPRICING_GAP = "RISK_MODEL_MARK_GAP"
    PARAMETRIC_ON_OPTIONS = "RISK_PARAMETRIC_ON_NONLINEAR_BOOK"
    NOT_BROKER_MARGIN = "MARGIN_IS_A_MODEL_ESTIMATE"


#: A model value this far from the marked value, relative to the book, is worth
#: saying out loud: every P&L below is measured from the model side.
REPRICING_GAP_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class RiskRunParams:
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    settlement_time_utc: time | None = None
    as_of: datetime | None = None
    lookback: int | None = None
    horizon_days: int = 1
    confidences: tuple[float, ...] = DEFAULT_CONFIDENCES
    paths: int = 10_000
    seed: int = 20_260_924
    distribution: Distribution = Distribution.NORMAL
    degrees_of_freedom: float = 5.0
    include_volatility_factor: bool = True

    def to_valuation_params(self) -> ValuePortfolioParams:
        return ValuePortfolioParams(
            risk_free_rate=self.risk_free_rate,
            dividend_yield=self.dividend_yield,
            settlement_time_utc=self.settlement_time_utc,
            as_of=self.as_of,
        )

    def to_provenance(self) -> dict:
        return {
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "settlement_time_utc": (
                self.settlement_time_utc.isoformat() if self.settlement_time_utc else None
            ),
            "lookback": self.lookback,
            "horizon_days": self.horizon_days,
            "confidences": list(self.confidences),
            "paths": self.paths,
            "seed": self.seed,
            "distribution": str(self.distribution),
            "include_volatility_factor": self.include_volatility_factor,
        }


@dataclass(frozen=True, slots=True)
class MarginRunParams:
    """What the margin estimate and the vulnerability ladder were run under."""

    model: str = DEFAULT_MARGIN_MODEL
    spot_returns: tuple[float, ...] | None = None
    vol_points: tuple[float, ...] | None = None
    short_option_minimum_rate: float = 0.0
    concentration_add_on_rate: float = 0.0
    concentration_threshold: float = 0.5
    eligible_capital: float | None = None
    ladder: tuple[float, ...] = DEFAULT_LADDER
    vol_co_shock: float = 0.0

    def to_margin_parameters(self) -> MarginParameters:
        grid = ShockGrid()
        if self.spot_returns or self.vol_points:
            grid = ShockGrid(
                spot_returns=tuple(self.spot_returns or ShockGrid().spot_returns),
                vol_points=tuple(self.vol_points or ShockGrid().vol_points),
            )
        return MarginParameters(
            grid=grid,
            short_option_minimum_rate=self.short_option_minimum_rate,
            concentration_add_on_rate=self.concentration_add_on_rate,
            concentration_threshold=self.concentration_threshold,
        )

    def to_provenance(self) -> dict:
        return {
            "model": self.model,
            "eligible_capital": self.eligible_capital,
            "vol_co_shock": self.vol_co_shock,
            "ladder": list(self.ladder),
            **self.to_margin_parameters().to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    """One valued, repriceable book plus the factor history behind it."""

    snapshot_id: uuid.UUID
    valuation_id: uuid.UUID
    computed: ComputedValuation
    exposures: ExposureSet
    panel: FactorPanel | None
    warnings: tuple[AnalyticalWarning, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "snapshot_id": str(self.snapshot_id),
            "valuation_id": str(self.valuation_id),
            "as_of_timestamp": self.computed.context.as_of.isoformat(),
            "market_state_id": self.computed.context.market_state.state_id,
            "base_currency": self.computed.valuation.base_currency,
            **self.exposures.to_dict(),
            "greeks": self.computed.valuation.greeks.to_dict(),
            "factor_panel": self.panel.to_dict() if self.panel else None,
        }


class RiskApplicationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self.repository = RiskRepository(session)
        self.portfolios = PortfolioApplicationService(session, settings)

    # ------------------------------------------------------------- snapshot
    async def build_snapshot(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        params: RiskRunParams,
        valuation_composer,
        history_composer=None,
    ) -> RiskSnapshot | None:
        computed = await self.portfolios.compute_valuation(
            user_id, portfolio_id, params.to_valuation_params(), valuation_composer
        )
        if computed is None:
            return None

        valuation_row = await self.portfolios.persist_valuation(user_id, portfolio_id, computed)
        strategy_tags = {
            position.id: position.strategy_tag
            for position in computed.positions
            if position.strategy_tag
        }
        exposures = build_exposures(computed.valuation, computed.context, strategy_tags)

        panel = None
        if history_composer is not None:
            series = await history_composer.build(
                user_id,
                [uuid.UUID(key) for key in exposures.underlying_keys()],
                limit=params.lookback or 500,
                include_volatility=params.include_volatility_factor,
            )
            panel = _panel_from(series, params)

        warnings = list(computed.warnings)
        warnings.extend(_snapshot_warnings(exposures))

        provenance = self._provenance(portfolio_id, computed, params)
        row = await self.repository.create_snapshot(
            user_id=user_id,
            portfolio_id=portfolio_id,
            valuation_id=valuation_row.id,
            as_of_timestamp=computed.context.as_of,
            base_currency=computed.valuation.base_currency,
            market_state_id=computed.context.market_state.state_id,
            positions=len(exposures.exposures),
            excluded_positions=len(exposures.excluded),
            base_value=exposures.base_value,
            reported_value=exposures.reported_value,
            delta=computed.valuation.greeks.delta,
            gamma=computed.valuation.greeks.gamma,
            vega_per_vol_point=computed.valuation.greeks.vega_per_vol_point,
            theta_per_day=computed.valuation.greeks.theta_per_day,
            rho_per_bp=computed.valuation.greeks.rho_per_bp,
            excluded=[item.to_dict() for item in exposures.excluded],
            provenance=provenance.to_dict(),
        )
        return RiskSnapshot(
            snapshot_id=row.id,
            valuation_id=valuation_row.id,
            computed=computed,
            exposures=exposures,
            panel=panel,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------ VaR
    async def run_var(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        method: VaRMethod,
        params: RiskRunParams,
        valuation_composer,
        history_composer,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        snapshot = await self.build_snapshot(
            user_id, portfolio_id, params, valuation_composer, history_composer
        )
        if snapshot is None:
            return self._failed(
                portfolio_id,
                RiskWarningCode.NO_MARKET_DATA,
                "No market data is available for any underlying in this "
                "portfolio, so it could not be valued, let alone stressed.",
            ), None

        if not snapshot.exposures.exposures:
            return self._failed(
                portfolio_id,
                RiskWarningCode.NO_EXPOSURES,
                "No position in this portfolio could be repriced, so there "
                "is nothing to measure risk on.",
            ), None

        panel = snapshot.panel
        if panel is None or not panel.is_sufficient:
            return self._failed(
                portfolio_id,
                RiskWarningCode.INSUFFICIENT_HISTORY,
                (
                    "The factor history this platform holds is too short to "
                    f"estimate risk from: {panel.observations if panel else 0} "
                    "aligned observations. History accumulates one point per "
                    "ingested option chain, so ingest more of them rather than "
                    "reading a number produced from too few."
                ),
                extra={"observations": panel.observations if panel else 0},
            ), None

        result = _dispatch(method, snapshot.exposures, panel, params)

        provenance = self._provenance(portfolio_id, snapshot.computed, params, method=method)
        row = await self.repository.create_var(
            user_id=user_id,
            portfolio_id=portfolio_id,
            snapshot_id=snapshot.snapshot_id,
            method=str(method),
            horizon_days=params.horizon_days,
            scenarios=result.scenarios,
            base_value=result.base_value,
            seed=params.seed if method is VaRMethod.MONTE_CARLO else None,
            tail_risk=[item.to_dict() for item in result.tail_risks],
            estimate_intervals={
                key: {"low": low, "high": high}
                for key, (low, high) in result.estimate_intervals.items()
            },
            assumptions=result.assumptions,
            factor_panel=result.panel,
            warnings=list(result.warnings),
            provenance=provenance.to_dict(),
        )

        payload = result.to_dict()
        payload["var_id"] = str(row.id)
        payload["snapshot"] = snapshot.to_dict()
        warnings = [*snapshot.warnings, *_result_warnings(result.warnings)]
        status = AnalyticalResult.ok(payload, provenance, tuple(warnings))
        if snapshot.exposures.excluded:
            status = AnalyticalResult.partial(payload, provenance, tuple(warnings))
        return status, row.id

    # --------------------------------------------------------------- stress
    async def run_stress(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        scenario: Scenario,
        params: RiskRunParams,
        valuation_composer,
        time_decay_days: float = 0.0,
        scenario_row_id: uuid.UUID | None = None,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        snapshot = await self.build_snapshot(user_id, portfolio_id, params, valuation_composer)
        if snapshot is None:
            return self._failed(
                portfolio_id,
                RiskWarningCode.NO_MARKET_DATA,
                "No market data is available for any underlying in this "
                "portfolio, so it could not be valued, let alone stressed.",
            ), None
        if not snapshot.exposures.exposures:
            return self._failed(
                portfolio_id,
                RiskWarningCode.NO_EXPOSURES,
                "No position in this portfolio could be repriced, so the "
                "scenario has nothing to act on.",
            ), None

        result = apply_scenario(
            snapshot.exposures,
            scenario,
            time_decay_days=time_decay_days,
            dimensions=(
                ContributionDimension.UNDERLYING,
                ContributionDimension.EXPIRY,
                ContributionDimension.ASSET_CLASS,
                ContributionDimension.STRATEGY_TAG,
            ),
        )

        provenance = self._provenance(portfolio_id, snapshot.computed, params, scenario=scenario)
        row = await self.repository.create_stress(
            user_id=user_id,
            portfolio_id=portfolio_id,
            snapshot_id=snapshot.snapshot_id,
            scenario_id=scenario_row_id,
            scenario_name=scenario.name,
            scenario_source=str(scenario.source),
            shocks={key: shock.to_dict() for key, shock in result.revaluation.shocks.items()},
            base_value=result.revaluation.base_value,
            shocked_value=result.revaluation.shocked_value,
            pnl=result.revaluation.pnl,
            greek_estimate=result.revaluation.greek_estimate,
            time_decay_days=time_decay_days,
            floored_volatilities=result.revaluation.floored_volatilities,
            contributions=[item.to_dict() for item in result.breakdowns],
            positions=[item.to_dict() for item in result.revaluation.positions],
            warnings=[],
            provenance=provenance.to_dict(),
        )

        payload = result.to_dict()
        payload["stress_id"] = str(row.id)
        payload["snapshot"] = snapshot.to_dict()
        warnings = list(snapshot.warnings)
        status = AnalyticalResult.ok(payload, provenance, tuple(warnings))
        if snapshot.exposures.excluded:
            status = AnalyticalResult.partial(payload, provenance, tuple(warnings))
        return status, row.id

    # --------------------------------------------------------------- margin
    async def run_margin(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        params: RiskRunParams,
        margin_params: MarginRunParams,
        valuation_composer,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        """Estimate margin and scan the buffer ladder around today's market."""
        snapshot = await self.build_snapshot(user_id, portfolio_id, params, valuation_composer)
        if snapshot is None:
            return (
                self._failed(
                    portfolio_id,
                    RiskWarningCode.NO_MARKET_DATA,
                    "No market data is available for any underlying in this "
                    "portfolio, so it could not be valued and no margin can be "
                    "estimated from it.",
                ),
                None,
            )

        try:
            model = build_model(margin_params.model, margin_params.to_margin_parameters())
        except ValueError as exc:
            raise RiskError(str(exc)) from exc

        result = scan_vulnerability(
            snapshot.exposures,
            model,
            eligible_capital=margin_params.eligible_capital,
            ladder=tuple(margin_params.ladder),
            vol_co_shock=margin_params.vol_co_shock,
        )

        provenance = self._provenance(
            portfolio_id, snapshot.computed, params, margin=margin_params, method_name=result.method
        )
        row = await self.repository.create_margin(
            user_id=user_id,
            portfolio_id=portfolio_id,
            snapshot_id=snapshot.snapshot_id,
            method=result.method,
            model_version=result.base.model_version,
            currency=result.currency,
            estimated_margin=result.base.estimated_margin,
            confidence=result.base.confidence,
            eligible_capital=result.eligible_capital,
            buffer=result.base_buffer,
            utilisation=result.base_utilisation,
            in_shortfall_at_rest=result.in_shortfall_at_rest,
            vol_co_shock=result.vol_co_shock,
            worst_spot_return=result.base.worst_spot_return,
            worst_vol_points=result.base.worst_vol_points,
            worst_loss=result.base.worst_loss,
            worst_at_grid_edge=result.base.worst_at_grid_edge,
            positions=result.base.positions,
            excluded_positions=result.base.excluded_positions,
            summary=result.summary[:1000],
            components=[item.to_dict() for item in result.base.components],
            assumptions=[*result.base.assumptions, *result.assumptions],
            parameters=result.base.parameters,
            shortfall_region={
                "downside": result.downside.to_dict() if result.downside else None,
                "upside": result.upside.to_dict() if result.upside else None,
            },
            ladder=[point.to_dict() for point in result.ladder],
            warnings=[*result.base.warnings, *result.warnings],
            provenance=provenance.to_dict(),
        )

        payload = result.to_dict()
        payload["margin_id"] = str(row.id)
        payload["snapshot"] = snapshot.to_dict()

        warnings = [
            AnalyticalWarning.info(
                RiskWarningCode.NOT_BROKER_MARGIN,
                result.base.disclaimer,
                method=result.method,
            ),
            *snapshot.warnings,
            *_margin_warnings(result),
        ]
        status = AnalyticalResult.ok(payload, provenance, tuple(warnings))
        if snapshot.exposures.excluded:
            status = AnalyticalResult.partial(payload, provenance, tuple(warnings))
        return status, row.id

    # ---------------------------------------------------------- incremental
    def incremental_book(
        self, computed: ComputedValuation, proposed: ProposedPositionValuation
    ) -> CombinedBook:
        """The book, the proposed order, and the two together.

        Both sides are built by ``build_exposures`` from valuations taken in the
        *same* ``ValuationContext``, so an anchor that differs between them is
        not possible by construction rather than by convention.
        """
        strategy_tags = {
            position.id: position.strategy_tag
            for position in computed.positions
            if position.strategy_tag
        }
        current = build_exposures(computed.valuation, computed.context, strategy_tags)
        order = build_exposures(proposed.valuation, computed.context)
        return combine(current, order)

    async def run_incremental_risk(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        computed: ComputedValuation,
        book: CombinedBook,
        params: RiskRunParams,
        method: VaRMethod,
        history_composer,
        scenario: Scenario | None = None,
        time_decay_days: float = 0.0,
    ) -> AnalyticalResult[dict]:
        """Greeks, VaR/ES and one scenario, before and after the order.

        Nothing is persisted: this is an analysis of a position that does not
        exist, and writing a risk snapshot for it would put a book nobody holds
        into the portfolio's own risk history.
        """
        provenance = self._provenance(portfolio_id, computed, params, method=method)
        provenance.model_versions["incremental"] = INCREMENTAL_MODEL_VERSION

        if not book.order_is_repriceable:
            return AnalyticalResult.failed(
                provenance,
                (
                    AnalyticalWarning.error(
                        IncrementalWarning.ORDER_NOT_REPRICEABLE,
                        "The proposed contract could not be repriced "
                        f"({book.order_exclusion_reason}), so it contributes nothing "
                        "to either side of the comparison. Every difference would be "
                        "exactly zero, which would read as an order that adds no "
                        "risk. It is refused instead.",
                        reason=book.order_exclusion_reason,
                    ),
                ),
            )

        greeks = IncrementalGreeks(
            current=greeks_of(book.current), proposed=greeks_of(book.proposed)
        )
        payload: dict = {
            "book": book.to_dict(),
            "greeks": greeks.to_dict(),
            "value_at_risk": None,
            "stress": None,
        }
        warnings: list[AnalyticalWarning] = [*computed.warnings, *_snapshot_warnings(book.proposed)]

        series = await history_composer.build(
            user_id,
            [uuid.UUID(key) for key in book.proposed.underlying_keys()],
            limit=params.lookback or 500,
            include_volatility=params.include_volatility_factor,
        )
        panel = _panel_from(series, params)

        if panel is None or not panel.is_sufficient:
            warnings.append(
                AnalyticalWarning.error(
                    RiskWarningCode.INSUFFICIENT_HISTORY,
                    "The factor history this platform holds is too short to "
                    f"estimate risk from: {panel.observations if panel else 0} "
                    "aligned observations. The Greeks above stand; the value-at-risk "
                    "comparison does not, and is absent rather than estimated from "
                    "too few points.",
                    observations=panel.observations if panel else 0,
                )
            )
        else:
            measured = incremental_var(
                book,
                panel,
                method,
                lambda exposures, factors: _dispatch(method, exposures, factors, params),
            )
            payload["value_at_risk"] = measured.to_dict()
            warnings.extend(_result_warnings(measured.warnings))
            warnings.extend(_incremental_warnings(measured.warnings))

        if scenario is not None:
            stressed = incremental_stress(book, scenario, time_decay_days=time_decay_days)
            payload["stress"] = stressed.to_dict()
        else:
            warnings.append(
                AnalyticalWarning.info(
                    "INCREMENTAL_NO_SCENARIO",
                    "No scenario was requested, so no stress comparison was run.",
                )
            )

        return AnalyticalResult.ok(payload, provenance, tuple(warnings))

    def run_incremental_margin(
        self,
        portfolio_id: uuid.UUID,
        computed: ComputedValuation,
        book: CombinedBook,
        params: RiskRunParams,
        margin_params: MarginRunParams,
    ) -> AnalyticalResult[dict]:
        """Estimated margin, buffer and utilisation, before and after the order."""
        try:
            model = build_model(margin_params.model, margin_params.to_margin_parameters())
        except ValueError as exc:
            raise RiskError(str(exc)) from exc

        provenance = self._provenance(
            portfolio_id, computed, params, margin=margin_params, method_name=model.identifier
        )
        provenance.model_versions["incremental"] = INCREMENTAL_MODEL_VERSION

        if not book.order_is_repriceable:
            return AnalyticalResult.failed(
                provenance,
                (
                    AnalyticalWarning.error(
                        IncrementalWarning.ORDER_NOT_REPRICEABLE,
                        "The proposed contract could not be repriced "
                        f"({book.order_exclusion_reason}), so the margin estimate "
                        "would be identical before and after. It is refused rather "
                        "than reported as an order that consumes no margin.",
                        reason=book.order_exclusion_reason,
                    ),
                ),
            )

        measured = incremental_margin(
            book,
            model,
            eligible_capital=margin_params.eligible_capital,
            ladder=tuple(margin_params.ladder),
            vol_co_shock=margin_params.vol_co_shock,
        )
        warnings = [
            AnalyticalWarning.info(
                RiskWarningCode.NOT_BROKER_MARGIN,
                measured.proposed.base.disclaimer,
                method=model.identifier,
            ),
            *_margin_warnings(measured.proposed),
        ]
        return AnalyticalResult.ok(measured.to_dict(), provenance, tuple(warnings))

    async def get_margin(self, margin_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_margin(margin_id, user_id)

    async def list_margin(self, portfolio_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50):
        return await self.repository.list_margin(portfolio_id, user_id, limit=limit)

    # -------------------------------------------------------------- helpers
    def _provenance(
        self,
        portfolio_id: uuid.UUID,
        computed: ComputedValuation,
        params: RiskRunParams,
        method: VaRMethod | None = None,
        scenario: Scenario | None = None,
        margin: MarginRunParams | None = None,
        method_name: str | None = None,
    ) -> Provenance:
        model_versions = {
            "risk": RISK_MODEL_VERSION,
            "valuation": "portfolio-valuation@1.0.0",
            "pricing": "black-scholes-merton@1.0.0",
        }
        if scenario is not None:
            model_versions["stress"] = STRESS_MODEL_VERSION
        if method_name is not None:
            model_versions["margin"] = method_name
        parameters: dict = {
            "portfolio_id": str(portfolio_id),
            "context": computed.context.to_provenance(),
            "run": params.to_provenance(),
        }
        if method is not None:
            parameters["method"] = str(method)
        if scenario is not None:
            parameters["scenario"] = scenario.to_dict()
        if margin is not None:
            parameters["margin"] = margin.to_provenance()
        return Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_id=computed.context.market_state.state_id,
            market_state_timestamp=computed.context.as_of,
            market_data_sources=computed.context.market_state.sources,
            dataset_versions=dict(computed.context.market_state.data_versions),
            model_versions=model_versions,
            parameters=parameters,
        )

    def _failed(
        self, portfolio_id: uuid.UUID, code: str, message: str, extra: dict | None = None
    ) -> AnalyticalResult[dict]:
        return AnalyticalResult.failed(
            Provenance.now(
                code_commit=self._settings.code_commit,
                model_versions={"risk": RISK_MODEL_VERSION},
                parameters={"portfolio_id": str(portfolio_id)},
            ),
            (AnalyticalWarning.error(code, message, **(extra or {})),),
        )

    async def get_var(self, var_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_var(var_id, user_id)

    async def get_stress(self, stress_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_stress(stress_id, user_id)

    async def list_var(self, portfolio_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50):
        return await self.repository.list_var(portfolio_id, user_id, limit=limit)

    async def list_stress(self, portfolio_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50):
        return await self.repository.list_stress(portfolio_id, user_id, limit=limit)


def _panel_from(series: list[FactorSeries], params: RiskRunParams) -> FactorPanel | None:
    if not series:
        return None
    return build_panel(
        series,
        window_days=params.horizon_days,
        policy=AlignmentPolicy.INTERSECT_DATES,
        lookback=params.lookback,
    )


def _dispatch(method: VaRMethod, exposures: ExposureSet, panel: FactorPanel, params: RiskRunParams):
    match method:
        case VaRMethod.HISTORICAL:
            return historical_var(
                exposures,
                panel,
                confidences=params.confidences,
                horizon_days=params.horizon_days,
                seed=params.seed,
            )
        case VaRMethod.PARAMETRIC:
            return parametric_var(
                exposures,
                panel,
                confidences=params.confidences,
                horizon_days=params.horizon_days,
            )
        case VaRMethod.MONTE_CARLO:
            return monte_carlo_var(
                exposures,
                panel,
                paths=params.paths,
                seed=params.seed,
                confidences=params.confidences,
                horizon_days=params.horizon_days,
                distribution=params.distribution,
                degrees_of_freedom=params.degrees_of_freedom,
            )
    raise RiskError(f"unknown VaR method {method}")


def _snapshot_warnings(exposures: ExposureSet) -> list[AnalyticalWarning]:
    warnings: list[AnalyticalWarning] = []
    if exposures.excluded:
        warnings.append(
            AnalyticalWarning.warn(
                RiskWarningCode.POSITIONS_EXCLUDED,
                f"{len(exposures.excluded)} position(s) could not be repriced and "
                "are outside every number below. They are listed with their "
                "reason rather than treated as riskless.",
                count=len(exposures.excluded),
                reported_value=exposures.excluded_reported_value,
            )
        )
    scale = max(abs(exposures.reported_value), 1.0)
    if abs(exposures.repricing_gap) / scale > REPRICING_GAP_TOLERANCE:
        warnings.append(
            AnalyticalWarning.info(
                RiskWarningCode.REPRICING_GAP,
                "The model value at today's anchors differs from the marked value "
                f"by {exposures.repricing_gap:,.2f}. Every P&L below is measured "
                "from the model side, so this gap is not part of any of them.",
                gap=exposures.repricing_gap,
                model_value=exposures.base_value,
                marked_value=exposures.reported_value,
            )
        )
    return warnings


def _result_warnings(codes: tuple[str, ...]) -> list[AnalyticalWarning]:
    messages = {
        RiskWarningCode.PARAMETRIC_ON_OPTIONS: (
            "This book contains options, so the parametric estimate ignores their "
            "convexity. Compare it with the historical or Monte Carlo answer "
            "rather than using it alone."
        ),
        "RISK_VOLATILITY_HELD_CONSTANT": (
            "No volatility history was available, so implied volatility was held "
            "fixed in every scenario. For an option book that understates risk."
        ),
        "RISK_SINGLE_UNDERLYING_NO_DIVERSIFICATION": (
            "Every position shares one underlying, so there is no diversification "
            "in these numbers to speak of."
        ),
        "RISK_OVERLAPPING_WINDOWS": (
            "Multi-day returns overlap, so the observations are not independent "
            "and the effective sample is smaller than the count shown."
        ),
        "RISK_OBSERVATIONS_DROPPED_BY_ALIGNMENT": (
            "Some observations were dropped because a factor had no value on that "
            "date. Nothing was forward-filled."
        ),
    }
    return [
        AnalyticalWarning.warn(code, messages.get(code, code)) for code in codes if code in messages
    ]


def _margin_warnings(result) -> list[AnalyticalWarning]:
    """Turn the model's own codes into sentences a reader can act on."""
    messages = {
        "MARGIN_NO_SHORT_OPTION_MINIMUM": (
            "No short-option minimum was applied, so a book of far out-of-the-money "
            "short options is understated by this estimate. The rate is left at "
            "zero because choosing one would be inventing a rule no venue published."
        ),
        "MARGIN_WORST_LOSS_AT_GRID_EDGE": (
            "The worst loss sat at the edge of the shock grid, so the true worst "
            "case over a wider range of moves is larger than this estimate. Widen "
            "the grid to find it."
        ),
        "MARGIN_VOLATILITY_HELD_FLAT": (
            "Volatility was held flat across the grid, so a book whose risk is "
            "mostly vega is not measured by this estimate."
        ),
        "MARGIN_POSITIONS_EXCLUDED": (
            "Some positions could not be repriced and contribute nothing to the "
            "estimate. They are listed on the snapshot with their reasons."
        ),
        "MARGIN_EMPTY_BOOK": (
            "No position could be repriced, so the estimate is zero. That is an "
            "absence, not a finding that the book is riskless."
        ),
        "VULNERABILITY_NO_ELIGIBLE_CAPITAL": (
            "No eligible capital was supplied, so utilisation and buffer are "
            "undefined. They are not defaulted to the portfolio's value, which is "
            "a different and usually wrong quantity."
        ),
        "VULNERABILITY_ALREADY_IN_SHORTFALL_REGION": (
            "The estimated buffer is already at or below zero before any move."
        ),
        "VULNERABILITY_NO_SHORTFALL_WITHIN_LADDER": (
            "The estimated buffer stays positive everywhere on the scanned ladder. "
            "It may not beyond that range."
        ),
        "VULNERABILITY_SHORTFALL_BEYOND_LADDER": (
            "A shortfall appears on the ladder but no crossing was located; widen "
            "or refine the ladder."
        ),
    }
    codes = [*result.base.warnings, *result.warnings]
    return [
        AnalyticalWarning.warn(code, messages.get(code, code)) for code in codes if code in messages
    ]


def _incremental_warnings(codes: tuple[str, ...]) -> list[AnalyticalWarning]:
    """The caveats that belong to the comparison rather than to either side.

    Kept apart from ``_result_warnings`` because these are properties of running
    one estimator twice, not of the estimator, and a reader needs to be told
    that the sample both sides were measured on is a single shared one.
    """
    messages = {
        IncrementalWarning.SHARED_PANEL: (
            "Both sides were measured over one factor panel built from the "
            "combined book, so the difference is the order's contribution and "
            "not the difference between two samples."
        ),
        IncrementalWarning.ORDER_ON_A_NEW_UNDERLYING: (
            "This order is on an underlying the portfolio does not hold. Its "
            "factor history joins the panel, which can shorten the aligned "
            "sample that the *current* book is measured on as well. The "
            "alternative — a panel per side — would put that change inside the "
            "difference attributed to the order."
        ),
    }
    return [AnalyticalWarning.info(code, messages[code]) for code in codes if code in messages]
