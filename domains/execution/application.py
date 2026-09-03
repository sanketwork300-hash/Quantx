"""Execution application service: import trades, analyse them, persist reports."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.execution.benchmarks import (
    BenchmarkKind,
    MarketWindow,
    arrival_benchmark,
    close_benchmark,
    decision_benchmark,
    interval_twap,
    interval_vwap,
    prevailing_mid_benchmark,
)
from domains.execution.impact import IMPACT_MODELS, ImpactError, build_impact_model
from domains.execution.importer import (
    IMPORT_MODEL_VERSION,
    TRADE_FIELDS,
    TradeImportDefaults,
    TradeImporter,
    TradeImportPreview,
    commit_instruments,
)
from domains.execution.models import ParentOrder, Side, group_executions
from domains.execution.orders import (
    FLAT_REFERENCE_CAVEAT,
    OrderCostEstimate,
    OrderCostRequest,
    OrderCostWarning,
    estimate_order_cost,
)
from domains.execution.repository import ExecutionRepository, to_execution
from domains.execution.simulation import (
    SIMULATION_MODEL_VERSION,
    StrategyComparison,
    compare,
)
from domains.execution.strategies import (
    STRATEGIES,
    IntervalContext,
    MarketContext,
    Schedule,
    ScheduleError,
    StrategyUnavailable,
    build_strategy,
    uniform_intervals,
)
from domains.execution.tca import ExecutionAnalysis, analyse
from domains.instruments.service import InstrumentService
from domains.market_data.ingestion.column_mapping import ColumnMapping, infer_mapping
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from infrastructure.settings import Settings

TCA_MODEL_VERSION = "execution-tca@1.0.0"


class ExecutionError(Exception):
    pass


class ImportRefused(ExecutionError):
    """A commit was attempted while rows were still ambiguous."""


class SimulationRefused(ExecutionError):
    """The requested simulation cannot be set up from what was supplied."""


class ExecutionWarningCode:
    AMBIGUOUS_ROWS = "TRADE_IMPORT_AMBIGUOUS_ROWS"
    INVALID_ROWS = "TRADE_IMPORT_INVALID_ROWS"
    INSTRUMENTS_CREATED = "TRADE_IMPORT_INSTRUMENTS_CREATED"
    INFERRED_GROUPING = "TCA_INFERRED_PARENT_GROUPING"
    NO_EXECUTIONS = "TCA_NO_EXECUTIONS_IN_RANGE"
    COVERAGE_LOW = "TCA_DATA_COVERAGE_LOW"
    NO_BENCHMARK = "TCA_NO_BENCHMARK_AVAILABLE"
    DECOMPOSITION_IS_MODELLED = "TCA_DECOMPOSITION_IS_MODELLED"
    COUNTERFACTUAL = "COUNTERFACTUAL_ESTIMATE"
    NO_STRATEGY_AVAILABLE = "SIMULATION_NO_STRATEGY_AVAILABLE"
    IMPACT_NOT_CALIBRATED = "SIMULATION_IMPACT_NOT_CALIBRATED"
    SIMULATION_INCOMPLETE = "SIMULATION_INCOMPLETE_SCHEDULE"


@dataclass(frozen=True, slots=True)
class TradeImportResult:
    preview: TradeImportPreview
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    missing_required: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimulateParams:
    """Everything a counterfactual run needs, all of it supplied or declared.

    None of the volume, spread or volatility inputs come from the platform: it
    holds no intraday profile of its own, and a strategy that needs one and does
    not get it refuses rather than assuming the day is flat.
    """

    instrument_id: uuid.UUID
    side: Side
    quantity: Decimal
    start: datetime
    end: datetime
    intervals: int = 6
    strategies: tuple[str, ...] = ("TWAP",)
    impact_model: str = "SquareRootImpactModel"
    permanent_coefficient: float = 1.0
    temporary_coefficient: float = 1.0
    volatility: float = 0.2
    average_daily_volume: float = 0.0
    lot_size: Decimal = Decimal(1)
    expected_volumes: tuple[float, ...] | None = None
    spreads: tuple[float, ...] | None = None
    volatilities: tuple[float, ...] | None = None
    participation_rate: float = 0.10
    latency_seconds: float = 0.0
    max_price_age_seconds: float | None = None
    window_padding_seconds: float = 3600.0
    staleness_tolerance_seconds: float = 300.0

    def to_provenance(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "side": str(self.side),
            "quantity": format(self.quantity, "f"),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "intervals": self.intervals,
            "strategies": list(self.strategies),
            "impact_model": self.impact_model,
            "permanent_coefficient": self.permanent_coefficient,
            "temporary_coefficient": self.temporary_coefficient,
            "volatility": self.volatility,
            "average_daily_volume": self.average_daily_volume,
            "lot_size": format(self.lot_size, "f"),
            "expected_volumes": (list(self.expected_volumes) if self.expected_volumes else None),
            "spreads": list(self.spreads) if self.spreads else None,
            "volatilities": list(self.volatilities) if self.volatilities else None,
            "participation_rate": self.participation_rate,
            "latency_seconds": self.latency_seconds,
            "max_price_age_seconds": self.max_price_age_seconds,
            "staleness_tolerance_seconds": self.staleness_tolerance_seconds,
        }

    def build_context(self, reference_price: Decimal) -> MarketContext:
        base = uniform_intervals(self.start, self.end, self.intervals, self.expected_volumes)
        if self.spreads or self.volatilities:
            base = tuple(
                IntervalContext(
                    start=item.start,
                    end=item.end,
                    expected_volume=item.expected_volume,
                    spread=self.spreads[index] if self.spreads else None,
                    volatility=(self.volatilities[index] if self.volatilities else None),
                )
                for index, item in enumerate(base)
            )
        return MarketContext(
            intervals=base,
            reference_price=reference_price,
            volatility=self.volatility,
            average_daily_volume=self.average_daily_volume,
            lot_size=self.lot_size,
        )


@dataclass(frozen=True, slots=True)
class AnalyseParams:
    start: datetime | None = None
    end: datetime | None = None
    instrument_id: uuid.UUID | None = None
    parent_order_key: str | None = None
    primary_benchmark: BenchmarkKind = BenchmarkKind.ARRIVAL
    parent_gap_seconds: float = 300.0
    staleness_tolerance_seconds: float = 300.0
    window_padding_seconds: float = 3600.0

    def to_provenance(self) -> dict:
        return {
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "parent_order_key": self.parent_order_key,
            "primary_benchmark": str(self.primary_benchmark),
            "parent_gap_seconds": self.parent_gap_seconds,
            "staleness_tolerance_seconds": self.staleness_tolerance_seconds,
            "window_padding_seconds": self.window_padding_seconds,
        }


class ExecutionApplicationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self.repository = ExecutionRepository(session)
        self.instruments = InstrumentService(session)

    # ------------------------------------------------------------------ import
    async def preview_import(
        self,
        user_id: uuid.UUID,
        data: bytes,
        mapping: ColumnMapping | None,
        defaults: TradeImportDefaults,
        limit: int | None = None,
    ) -> TradeImportResult:
        from domains.market_data.ingestion.parser import TabularParser

        headers = TabularParser.read_headers(data)
        inferred = infer_mapping(headers, TRADE_FIELDS)
        applied = mapping or inferred

        preview = await TradeImporter(self.instruments).preview(
            data, applied, defaults, user_id, limit=limit
        )
        return TradeImportResult(
            preview=preview,
            headers=headers,
            inferred_mapping=inferred.to_dict(),
            applied_mapping=applied.to_dict(),
            missing_required=applied.missing_required(TRADE_FIELDS),
        )

    async def commit_import(
        self,
        user_id: uuid.UUID,
        data: bytes,
        mapping: ColumnMapping,
        defaults: TradeImportDefaults,
        upload_id: uuid.UUID | None = None,
    ) -> AnalyticalResult[dict]:
        """Commit a trade log, refusing while any row is ambiguous.

        A fill attributed to the wrong contract lands in the wrong parent order
        and takes a benchmark window with it, so every cost computed afterwards
        is wrong with no error anywhere. Refusing is the only safe answer.
        """
        result = await self.preview_import(user_id, data, mapping, defaults)
        preview = result.preview
        warnings: list[AnalyticalWarning] = []

        if preview.ambiguous:
            raise ImportRefused(
                f"{len(preview.ambiguous)} row(s) match more than one contract. "
                "Choose the intended instrument for each; the platform will not "
                "pick one for you, because a fill on the wrong contract corrupts "
                "every benchmark computed from it."
            )
        if preview.invalid:
            warnings.append(
                AnalyticalWarning.warn(
                    ExecutionWarningCode.INVALID_ROWS,
                    f"{len(preview.invalid)} row(s) could not become a fill; each "
                    "is reported with its source row number and reason.",
                    count=len(preview.invalid),
                )
            )

        created = commit_instruments(preview)
        if created:
            await self.instruments.upsert_many(created)
            warnings.append(
                AnalyticalWarning.info(
                    ExecutionWarningCode.INSTRUMENTS_CREATED,
                    f"{len(created)} contract(s) were added to the instrument master.",
                    count=len(created),
                )
            )

        rows = [
            {
                "id": row.execution.id,
                "user_id": user_id,
                "instrument_id": row.execution.instrument_id,
                "upload_id": upload_id,
                "side": str(row.execution.side),
                "quantity": row.execution.quantity,
                "execution_price": row.execution.execution_price,
                "exchange_timestamp": row.execution.exchange_timestamp,
                "receive_timestamp": row.execution.receive_timestamp,
                "order_id": row.execution.order_id,
                "parent_order_key": row.execution.parent_order_key,
                "order_type": str(row.execution.order_type),
                "limit_price": row.execution.limit_price,
                "order_quantity": row.execution.order_quantity,
                "submit_timestamp": row.execution.submit_timestamp,
                "decision_timestamp": row.execution.decision_timestamp,
                "broker": row.execution.broker,
                "venue": row.execution.venue,
                "fees": row.execution.fees,
                "source": str(row.execution.source),
                "execution_metadata": dict(row.execution.metadata),
            }
            for row in preview.resolved
        ]
        await self.repository.add_executions(rows)

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            model_versions={"import": IMPORT_MODEL_VERSION},
            parameters={
                "column_mapping": mapping.to_dict(),
                "defaults": defaults.to_provenance(),
                "upload_id": str(upload_id) if upload_id else None,
            },
        )
        payload = preview.to_dict()
        payload["committed"] = len(rows)
        payload["instruments_created"] = len(created)
        return AnalyticalResult.ok(payload, provenance, tuple(warnings))

    # ----------------------------------------------------------------- analysis
    async def analyse(
        self,
        user_id: uuid.UUID,
        params: AnalyseParams,
        window_composer,
    ) -> tuple[AnalyticalResult[dict], list[uuid.UUID]]:
        """Group stored fills into parents, benchmark each, and persist a report."""
        rows = await self.repository.list_executions(
            user_id,
            instrument_id=params.instrument_id,
            start=params.start,
            end=params.end,
            parent_order_key=params.parent_order_key,
        )
        executions = [to_execution(row) for row in rows]

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            model_versions={"tca": TCA_MODEL_VERSION},
            parameters={"analysis": params.to_provenance()},
        )
        if not executions:
            return (
                AnalyticalResult.failed(
                    provenance,
                    (
                        AnalyticalWarning.error(
                            ExecutionWarningCode.NO_EXECUTIONS,
                            "No stored fills match this range, so there is nothing "
                            "to analyse. Import a trade log first.",
                        ),
                    ),
                ),
                [],
            )

        instruments: dict[uuid.UUID, object] = {}
        for execution in executions:
            if execution.instrument_id not in instruments:
                instrument = await self.instruments.get(execution.instrument_id)
                if instrument is not None:
                    instruments[execution.instrument_id] = instrument

        parents = group_executions(
            executions, instruments, max_gap_seconds=params.parent_gap_seconds
        )

        analyses: list[ExecutionAnalysis] = []
        report_ids: list[uuid.UUID] = []
        for parent in parents:
            instrument = instruments.get(parent.instrument_id)
            window = await window_composer.build(
                user_id,
                parent.instrument_id,
                parent.start,
                parent.end,
                underlying_id=getattr(instrument, "underlying_id", None),
                padding_seconds=params.window_padding_seconds,
                staleness_tolerance_seconds=params.staleness_tolerance_seconds,
            )
            analysis = self._analyse_parent(parent, window, params.primary_benchmark)
            analyses.append(analysis)
            report_ids.append((await self._persist(user_id, analysis, provenance)).id)

        payload = {
            "parent_orders": len(analyses),
            "fills": len(executions),
            "reports": [analysis.to_dict() for analysis in analyses],
        }
        warnings = _aggregate_warnings(analyses)
        status = AnalyticalResult.ok(payload, provenance, tuple(warnings))
        if any(not analysis.shortfalls for analysis in analyses):
            status = AnalyticalResult.partial(payload, provenance, tuple(warnings))
        return status, report_ids

    @staticmethod
    def _analyse_parent(
        parent: ParentOrder, window: MarketWindow, primary: BenchmarkKind
    ) -> ExecutionAnalysis:
        submit = parent.start if parent.has_submit_timestamp else None
        benchmarks = [
            arrival_benchmark(window, submit, parent.first_fill.execution_price),
            decision_benchmark(window, parent.decision_timestamp),
            prevailing_mid_benchmark(
                window,
                [(item.exchange_timestamp, item.quantity) for item in parent.ordered],
            ),
            interval_twap(window),
            interval_vwap(window),
            close_benchmark(window),
        ]
        return analyse(parent, window, benchmarks, primary=primary)

    async def _persist(
        self, user_id: uuid.UUID, analysis: ExecutionAnalysis, provenance: Provenance
    ):
        parent = analysis.parent
        primary = analysis.primary_shortfall
        coverage = analysis.window.coverage
        return await self.repository.create_report(
            user_id=user_id,
            instrument_id=parent.instrument_id,
            parent_order_key=parent.key[:200],
            grouping_method=str(parent.grouping_method),
            grouping_is_inferred=parent.is_inferred,
            side=str(parent.side),
            canonical_key=parent.canonical_key,
            currency=parent.currency,
            multiplier=parent.multiplier,
            fills=len(parent.executions),
            filled_quantity=parent.filled_quantity,
            order_quantity=parent.order_quantity,
            average_price=parent.average_price,
            fees=parent.fees,
            window_start=analysis.window.start,
            window_end=analysis.window.end,
            primary_benchmark=str(analysis.primary),
            primary_benchmark_price=primary.benchmark_price if primary else None,
            shortfall_currency=float(primary.currency_amount) if primary else None,
            shortfall_bps=primary.basis_points if primary else None,
            shortfall_percent=primary.percent if primary else None,
            observations=coverage.observations,
            coverage_span_ratio=coverage.span_ratio,
            coverage_is_sufficient=coverage.is_sufficient,
            benchmarks=[item.to_dict() for item in analysis.benchmarks],
            shortfalls=[item.to_dict() for item in analysis.shortfalls],
            unavailable_shortfalls=[item.to_dict() for item in analysis.unavailable],
            decomposition=(analysis.decomposition.to_dict() if analysis.decomposition else None),
            market_window=analysis.window.to_dict(),
            warnings=list(analysis.warnings),
            provenance=provenance.to_dict(),
        )

    # ------------------------------------------------------------ simulation
    async def simulate(
        self,
        user_id: uuid.UUID,
        params: SimulateParams,
        window_composer,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        """Run one or more schedules against a past path, and store the result."""
        instrument = await self.instruments.get(params.instrument_id)
        if instrument is None:
            raise SimulationRefused(f"instrument {params.instrument_id} is not in the master")

        window = await window_composer.build(
            user_id,
            params.instrument_id,
            params.start,
            params.end,
            underlying_id=getattr(instrument, "underlying_id", None),
            padding_seconds=params.window_padding_seconds,
            staleness_tolerance_seconds=params.staleness_tolerance_seconds,
        )

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            model_versions={
                "simulation": SIMULATION_MODEL_VERSION,
                "tca": TCA_MODEL_VERSION,
            },
            parameters={"simulation": params.to_provenance()},
        )

        reference, _age = window.at(params.start)
        if reference is None:
            return (
                AnalyticalResult.failed(
                    provenance,
                    (
                        AnalyticalWarning.error(
                            ExecutionWarningCode.NO_EXECUTIONS,
                            "The market window holds no observation at or before "
                            "the start of the simulation window, so there is no "
                            "price to simulate against. A counterfactual needs a "
                            "path, and this one has none.",
                        ),
                    ),
                ),
                None,
            )

        context = params.build_context(reference.price)
        try:
            impact = build_impact_model(
                params.impact_model,
                params.permanent_coefficient,
                params.temporary_coefficient,
            )
        except ImpactError as exc:
            raise SimulationRefused(str(exc)) from exc

        schedules: dict[str, Schedule] = {}
        unavailable: dict[str, str] = {}
        for name in params.strategies:
            try:
                strategy = build_strategy(name, participation_rate=params.participation_rate)
                schedules[name] = strategy.generate_schedule(params.quantity, params.side, context)
            except StrategyUnavailable as exc:
                unavailable[name] = exc.reason
            except ScheduleError as exc:
                raise SimulationRefused(str(exc)) from exc

        if not schedules:
            return (
                AnalyticalResult.failed(
                    provenance,
                    tuple(
                        AnalyticalWarning.error(
                            ExecutionWarningCode.NO_STRATEGY_AVAILABLE,
                            f"{name}: {reason}",
                            strategy=name,
                        )
                        for name, reason in sorted(unavailable.items())
                    ),
                ),
                None,
            )

        comparison = compare(
            schedules,
            unavailable,
            window,
            context,
            impact,
            instrument_id=params.instrument_id,
            user_id=user_id,
            latency_seconds=params.latency_seconds,
            multiplier=getattr(instrument, "multiplier", Decimal(1)),
            currency=getattr(instrument, "currency", "INR"),
            max_price_age_seconds=params.max_price_age_seconds,
        )

        comparison_id = uuid.uuid4()
        await self._persist_simulations(user_id, comparison_id, params, comparison, provenance)

        payload = comparison.to_dict(include_fills=True)
        payload["comparison_id"] = str(comparison_id)
        return (
            AnalyticalResult.ok(payload, provenance, tuple(_simulation_warnings(comparison))),
            comparison_id,
        )

    async def _persist_simulations(
        self,
        user_id: uuid.UUID,
        comparison_id: uuid.UUID,
        params: SimulateParams,
        comparison: StrategyComparison,
        provenance: Provenance,
    ) -> None:
        rows = []
        for result in comparison.results:
            shortfall = result.analysis.primary_shortfall if result.analysis is not None else None
            rows.append(
                {
                    "user_id": user_id,
                    "instrument_id": params.instrument_id,
                    "comparison_id": comparison_id,
                    "counterfactual": True,
                    "strategy": result.strategy,
                    "impact_model": result.impact_model,
                    "impact_is_calibrated": (
                        "SIMULATION_IMPACT_NOT_CALIBRATED" not in result.warnings
                    ),
                    "side": str(result.side),
                    "ordered_quantity": result.ordered_quantity,
                    "filled_quantity": result.filled_quantity,
                    "completion_rate": result.completion_rate,
                    "average_price": result.average_price,
                    "window_start": result.schedule.start,
                    "window_end": result.schedule.end,
                    "latency_seconds": result.latency_seconds,
                    "max_price_age_seconds": result.max_price_age_seconds,
                    "modelled_impact_cost": result.modelled_impact_cost,
                    "modelled_spread_cost": result.modelled_spread_cost,
                    "primary_benchmark": (
                        str(shortfall.benchmark) if shortfall is not None else None
                    ),
                    "shortfall_currency": (
                        float(shortfall.currency_amount) if shortfall is not None else None
                    ),
                    "shortfall_bps": shortfall.basis_points if shortfall is not None else None,
                    "schedule": result.schedule.to_dict(),
                    "context": result.context.to_dict(include_intervals=True),
                    "fills": [item.to_dict() for item in result.fills],
                    "unfilled": [item.to_dict() for item in result.unfilled],
                    "benchmarks": (
                        [item.to_dict() for item in result.analysis.benchmarks]
                        if result.analysis
                        else []
                    ),
                    "warnings": list(result.warnings),
                    "provenance": provenance.to_dict(),
                }
            )
        await self.repository.add_simulations(rows)

    def estimate_order_cost(
        self,
        request: OrderCostRequest,
        permanent_coefficient: float = 1.0,
        temporary_coefficient: float = 1.0,
    ) -> tuple[OrderCostEstimate, tuple[AnalyticalWarning, ...]]:
        """Estimated slippage for an order that has not been placed.

        Nothing is persisted: there is no execution, no schedule anyone
        committed to and no fill to measure. The estimate belongs to whatever
        analysis asked for it, and Phase 11 stores it inside that analysis with
        the snapshot it was computed against.
        """
        try:
            impact = build_impact_model(
                request.impact_model, permanent_coefficient, temporary_coefficient
            )
        except ImpactError as exc:
            raise SimulationRefused(str(exc)) from exc

        estimate = estimate_order_cost(request, impact)
        return estimate, tuple(_order_cost_warnings(estimate))

    async def get_simulation(self, simulation_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_simulation(simulation_id, user_id)

    async def list_simulations(self, user_id: uuid.UUID, **kwargs):
        return await self.repository.list_simulations(user_id, **kwargs)

    @staticmethod
    def available_strategies() -> tuple[str, ...]:
        return tuple(sorted(STRATEGIES))

    @staticmethod
    def available_impact_models() -> tuple[str, ...]:
        return tuple(sorted(IMPACT_MODELS))

    async def get_report(self, report_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_report(report_id, user_id)

    async def list_reports(self, user_id: uuid.UUID, limit: int = 100, offset: int = 0):
        return await self.repository.list_reports(user_id, limit=limit, offset=offset)

    async def list_executions(self, user_id: uuid.UUID, **kwargs):
        return await self.repository.list_executions(user_id, **kwargs)


def _aggregate_warnings(analyses: list[ExecutionAnalysis]) -> list[AnalyticalWarning]:
    """Roll per-order flags up into sentences, counted."""
    messages = {
        "ARRIVAL_PROXY_USED": (
            "had no submit timestamp, so the arrival benchmark is the first fill's "
            "own price. A shortfall measured that way is systematically smaller "
            "than one measured against the price before trading started."
        ),
        "TCA_INFERRED_PARENT_GROUPING": (
            "had no parent order in the file, so fills were grouped by instrument, "
            "side and a contiguous time window. A different gap would produce "
            "different parents, different windows and different benchmarks."
        ),
        "TCA_DATA_COVERAGE_LOW": (
            "had too little market data across the execution window for an "
            "interval benchmark. Those benchmarks report themselves unavailable "
            "rather than being computed from a handful of ticks."
        ),
        "TCA_NO_BENCHMARK_AVAILABLE": (
            "could not be benchmarked at all, so no shortfall is reported for "
            "them. That is an absence, not a cost of zero."
        ),
        "TCA_STALE_REFERENCE_QUOTE": (
            "were benchmarked against a quote older than the staleness tolerance."
        ),
        "TCA_NO_SPREAD_DATA": (
            "had no two-sided quote at any fill, so no spread charge could be "
            "attributed and it sits inside the residual."
        ),
        "TCA_NO_ORDER_QUANTITY": (
            "did not state the order's intended quantity, so nothing is said about "
            "what went unfilled. It is not assumed to be zero."
        ),
    }
    counts: dict[str, int] = {}
    for analysis in analyses:
        for code in analysis.warnings:
            counts[code] = counts.get(code, 0) + 1

    warnings = [
        AnalyticalWarning.warn(code, f"{count} parent order(s) {messages[code]}", count=count)
        for code, count in sorted(counts.items())
        if code in messages
    ]
    if any(analysis.decomposition is not None for analysis in analyses):
        warnings.append(
            AnalyticalWarning.info(
                ExecutionWarningCode.DECOMPOSITION_IS_MODELLED,
                "The cost decomposition is model-based. Spread, impact and timing "
                "are not separately observable: the total is measured, the spread "
                "charge is modelled, fees are observed, and timing is the residual "
                "carrying everything else — including market impact, which this "
                "phase does not model.",
            )
        )
    return warnings


def _simulation_warnings(comparison: StrategyComparison) -> list[AnalyticalWarning]:
    """The counterfactual label first, because everything else depends on it."""
    warnings = [
        AnalyticalWarning.warn(
            ExecutionWarningCode.COUNTERFACTUAL,
            "Every number in this result is a counterfactual estimate. These "
            "schedules were never executed: they are priced against a path the "
            "market printed while something else was happening, and executing "
            "one would itself have moved that path in ways no simulation here "
            "can capture.",
        )
    ]
    if any("SIMULATION_IMPACT_NOT_CALIBRATED" in result.warnings for result in comparison.results):
        warnings.append(
            AnalyticalWarning.warn(
                ExecutionWarningCode.IMPACT_NOT_CALIBRATED,
                "The impact coefficients were left at the identity, so the impact "
                "figures are the shape of the model in units of sigma*sqrt(Q/ADV) "
                "rather than a magnitude anyone measured. Differences between "
                "strategies are smaller than that uncertainty.",
            )
        )
    incomplete = [result for result in comparison.results if result.completion_rate < 1.0]
    if incomplete:
        warnings.append(
            AnalyticalWarning.warn(
                ExecutionWarningCode.SIMULATION_INCOMPLETE,
                f"{len(incomplete)} schedule(s) could not be filled completely "
                "against the observed path. The unfilled slices are listed with "
                "their reasons, and the completion rate is not 100%.",
                count=len(incomplete),
            )
        )
    for name, reason in comparison.unavailable:
        warnings.append(
            AnalyticalWarning.info(
                ExecutionWarningCode.NO_STRATEGY_AVAILABLE, f"{name}: {reason}", strategy=name
            )
        )
    return warnings


#: One sentence per code, so a reader is told what an absent number means
#: rather than being left to infer it from the code.
_ORDER_COST_MESSAGES = {
    OrderCostWarning.COUNTERFACTUAL: (
        "An estimate of what this order would cost, not a measurement of what "
        "anything did cost. No order was placed."
    ),
    OrderCostWarning.FLAT_REFERENCE: FLAT_REFERENCE_CAVEAT,
    OrderCostWarning.NO_SPREAD: (
        "No two-sided quote was observed for this contract, so the spread half "
        "of the cost is absent rather than zero, and no total is stated."
    ),
    OrderCostWarning.NO_ADV: (
        "No average daily volume was supplied. The platform holds none, so "
        "market impact has no size to be relative to; it is reported as absent "
        "rather than as zero, and no total slippage is stated."
    ),
    OrderCostWarning.NO_VOLATILITY: (
        "No volatility was supplied, so the impact model has no scale and every "
        "impact figure below is zero by arithmetic rather than by measurement."
    ),
    OrderCostWarning.IMPACT_NOT_CALIBRATED: (
        "The impact coefficients are at their uncalibrated default of one. The "
        "impact figures are therefore the shape of the model in units of "
        "sigma * sqrt(Q/ADV), not a magnitude this platform is claiming. Supply "
        "coefficients measured on your own executions to change that."
    ),
    OrderCostWarning.STRATEGY_UNAVAILABLE: (
        "One or more requested schedules could not be built from what was "
        "supplied. Each is listed with its reason rather than omitted."
    ),
    OrderCostWarning.PASSIVE_FILL_NOT_MODELLED: (
        "The limit price rests behind the touch, so this order would not cross. "
        "Every figure is conditional on it filling in full, and whether a "
        "resting order fills is not modelled here."
    ),
    OrderCostWarning.LARGER_THAN_A_DAY: (
        "The order is larger than the whole day's volume that was supplied. The "
        "impact model is far outside any range it was ever fitted in."
    ),
}


def _order_cost_warnings(estimate: OrderCostEstimate) -> list[AnalyticalWarning]:
    severities = {
        OrderCostWarning.COUNTERFACTUAL: AnalyticalWarning.info,
        OrderCostWarning.FLAT_REFERENCE: AnalyticalWarning.info,
        OrderCostWarning.PASSIVE_FILL_NOT_MODELLED: AnalyticalWarning.warn,
    }
    return [
        severities.get(code, AnalyticalWarning.warn)(code, _ORDER_COST_MESSAGES[code])
        for code in estimate.warnings
        if code in _ORDER_COST_MESSAGES
    ]
