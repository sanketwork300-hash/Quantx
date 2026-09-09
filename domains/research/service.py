"""Running a backtest, and recording it well enough to argue with.

The path: warehouse bars → features → strategy → simulated fills → equity curve
→ metrics and attribution → an experiment row that holds everything needed to
run it again.

The service's own contribution is mostly refusal. It will not run against a
quarantined dataset, it will not silently include bars the warehouse flagged, and
it will not let a run be recorded without the cost schedule and slippage
assumption that produced its numbers. Each of those is a way a backtest ends up
reporting a return that nobody could reproduce.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from domains.research import attribution as attribution_module
from domains.research import metrics as metrics_module
from domains.research import strategies as strategy_module
from domains.research.costs import (
    NO_COST_MODEL,
    NO_SLIPPAGE_MODEL,
    CostSchedule,
    SlippageModel,
)
from domains.research.engine import BacktestConfig, BacktestResult, run
from domains.research.features import BarSeries, FeatureError
from domains.research.models import ExecutionTiming
from domains.research.orm import ResearchExperimentORM
from domains.warehouse.enums import DatasetKind, DatasetLayer
from domains.warehouse.query import QueryRequest
from domains.warehouse.service import WarehouseService
from infrastructure.settings import Settings
from infrastructure.storage.base import ObjectStore


class ResearchWarningCode:
    NO_BARS = "RESEARCH_NO_BARS"
    FLAGGED_BARS_EXCLUDED = "RESEARCH_FLAGGED_BARS_EXCLUDED"
    COSTS_NOT_MODELLED = "RESEARCH_COSTS_NOT_MODELLED"
    SLIPPAGE_NOT_MODELLED = "RESEARCH_SLIPPAGE_NOT_MODELLED"
    SHORT_SAMPLE = "RESEARCH_SHORT_SAMPLE"


class ResearchError(ValueError):
    """A run could not be set up as specified."""


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """Everything a run needs that is not already in the warehouse."""

    name: str
    instrument_id: uuid.UUID
    strategy_name: str
    strategy_parameters: dict = field(default_factory=dict)
    start: datetime | None = None
    end: datetime | None = None
    exchange: str | None = None
    dataset_id: uuid.UUID | None = None
    initial_cash: Decimal = Decimal(1_000_000)
    timing: ExecutionTiming = ExecutionTiming.NEXT_OPEN
    costs: CostSchedule = NO_COST_MODEL
    slippage: SlippageModel = NO_SLIPPAGE_MODEL
    max_position_weight: Decimal = Decimal(1)
    max_gross_exposure: Decimal = Decimal(1)
    include_flagged_bars: bool = False
    risk_free_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
    """A completed run, as the API returns it."""

    experiment_id: uuid.UUID
    result: BacktestResult
    metrics: metrics_module.PerformanceMetrics
    attribution: attribution_module.AttributionReport

    def to_dict(self, include_curve: bool = False, include_fills: bool = False) -> dict:
        return {
            "experiment_id": str(self.experiment_id),
            "backtest": self.result.to_dict(
                include_curve=include_curve, include_fills=include_fills
            ),
            "metrics": self.metrics.to_dict(),
            "attribution": self.attribution.to_dict(),
        }


def bars_from_table(instrument_id: uuid.UUID, rows: Sequence[dict]) -> tuple[BarSeries, list[bool]]:
    """Turn warehouse rows into a bar series, keeping the validator's flags.

    The flags come along rather than being dropped at the boundary. A backtest
    whose best day was a bad tick should be able to say so, and it cannot if the
    knowledge stopped at the query.
    """
    ordered = sorted(rows, key=lambda row: row["exchange_timestamp"])
    if not ordered:
        raise ResearchError("the query returned no bars to run against")

    def column(name: str) -> tuple[Decimal, ...]:
        return tuple(Decimal(str(row[name])) for row in ordered)

    series = BarSeries(
        instrument_id=instrument_id,
        timestamps=tuple(row["exchange_timestamp"] for row in ordered),
        open=column("open"),
        high=column("high"),
        low=column("low"),
        close=column("close"),
        volume=column("volume"),
    )
    return series, [bool(row.get("flags")) for row in ordered]


class ResearchService:
    def __init__(
        self, session: AsyncSession, settings: Settings, object_store: ObjectStore
    ) -> None:
        self._session = session
        self._settings = settings
        self._store = object_store
        self.warehouse = WarehouseService(session, settings, object_store)

    async def run_backtest(
        self, user_id: uuid.UUID, request: BacktestRequest
    ) -> AnalyticalResult[ExperimentSummary]:
        warnings: list[AnalyticalWarning] = []

        strategy = strategy_module.build(request.strategy_name, request.strategy_parameters)

        query = QueryRequest(
            layer=DatasetLayer.NORMALIZED,
            kind=DatasetKind.BARS,
            exchange=request.exchange,
            instrument_ids=(request.instrument_id,),
            start=request.start,
            end=request.end,
        )
        # Refuses a quarantined dataset. A backtest is exactly the place a
        # series with an unadjusted split does its damage.
        result = await self.warehouse.run_query(user_id, query, request.dataset_id)
        rows = result.table.to_pylist() if result.rows else []
        if not rows:
            raise ResearchError(
                "the warehouse holds no bars for this instrument and window; load a "
                "dataset first, or widen the range"
            )

        try:
            series, flagged = bars_from_table(request.instrument_id, rows)
        except FeatureError as exc:
            raise ResearchError(str(exc)) from exc

        if any(flagged) and not request.include_flagged_bars:
            warnings.append(
                AnalyticalWarning.info(
                    ResearchWarningCode.FLAGGED_BARS_EXCLUDED,
                    f"{sum(flagged)} bar(s) the warehouse validator flagged were not "
                    "traded on. They remain in the equity curve so it stays continuous.",
                    flagged_bars=sum(flagged),
                )
            )

        config = BacktestConfig(
            initial_cash=request.initial_cash,
            timing=request.timing,
            costs=request.costs,
            slippage=request.slippage,
            max_position_weight=request.max_position_weight,
            max_gross_exposure=request.max_gross_exposure,
            include_flagged_bars=request.include_flagged_bars,
        )
        backtest = run(series, strategy, config, flagged)

        if backtest.gross_of_costs:
            warnings.append(
                AnalyticalWarning.warn(
                    ResearchWarningCode.COSTS_NOT_MODELLED,
                    "no cost schedule was supplied, so every return here is gross of "
                    "brokerage, exchange charges and statutory levies. This platform "
                    "does not hold those rates and will not invent them.",
                )
            )
        if not request.slippage.models_slippage:
            warnings.append(
                AnalyticalWarning.warn(
                    ResearchWarningCode.SLIPPAGE_NOT_MODELLED,
                    "no slippage was modelled: fills are at the reference price, which "
                    "no real order achieves, so the result is optimistic by an "
                    "unmeasured amount.",
                )
            )

        performance = metrics_module.evaluate(
            equity=[point.equity for point in backtest.equity_curve],
            timestamps=[point.timestamp for point in backtest.equity_curve],
            realised_trades=[float(value) for value in backtest.realised_trades],
            traded_notional=backtest.traded_notional,
            risk_free_rate=request.risk_free_rate,
            gross_of_costs=backtest.gross_of_costs,
        )
        if not performance.is_reliable:
            warnings.append(
                AnalyticalWarning.warn(
                    ResearchWarningCode.SHORT_SAMPLE,
                    f"{performance.observations} return observations is too few for the "
                    "ratio metrics to mean much; they are reported with the count "
                    "attached rather than withheld.",
                    observations=performance.observations,
                )
            )

        report = attribution_module.attribute(
            initial_equity=backtest.initial_equity,
            final_equity=backtest.final_equity,
            fills=backtest.fills,
            realised_by_instrument=backtest.realised_by_instrument,
            unrealised_by_instrument=backtest.unrealised_by_instrument,
        )

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=backtest.end,
            parameters={
                "strategy": strategy.identifier,
                "strategy_parameters": strategy.parameters(),
                "features": [spec.name for spec in strategy.features()],
                "engine": config.to_dict(),
                "warehouse_read": result.to_provenance(),
            },
        )

        row = await self._persist(
            user_id, request, strategy, backtest, performance, report, provenance, warnings
        )
        return AnalyticalResult.ok(
            ExperimentSummary(
                experiment_id=row.id,
                result=backtest,
                metrics=performance,
                attribution=report,
            ),
            provenance,
            tuple(warnings),
        )

    async def _persist(
        self,
        user_id: uuid.UUID,
        request: BacktestRequest,
        strategy,
        backtest: BacktestResult,
        performance,
        report,
        provenance: Provenance,
        warnings: Sequence[AnalyticalWarning],
    ) -> ResearchExperimentORM:
        experiment_id = uuid.uuid4()
        curve_key = await self._put(
            f"research/{user_id}/{experiment_id}/equity_curve.json",
            [point.to_dict() for point in backtest.equity_curve],
        )
        fills_key = await self._put(
            f"research/{user_id}/{experiment_id}/fills.json",
            [fill.to_dict() for fill in backtest.fills],
        )

        row = ResearchExperimentORM(
            id=experiment_id,
            user_id=user_id,
            name=request.name,
            status="COMPLETED",
            dataset_id=request.dataset_id,
            instrument_id=request.instrument_id,
            start_timestamp=backtest.start,
            end_timestamp=backtest.end,
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            strategy_parameters=strategy.parameters(),
            features=[spec.name for spec in strategy.features()],
            engine_config=backtest.config.to_dict(),
            cost_schedule=request.costs.to_dict(),
            slippage_model=request.slippage.to_dict(),
            code_commit=self._settings.code_commit,
            data_digest=None,
            initial_equity=backtest.initial_equity,
            final_equity=backtest.final_equity,
            total_costs=backtest.total_costs,
            total_slippage=backtest.total_slippage,
            gross_of_costs=backtest.gross_of_costs,
            total_return=performance.total_return,
            cagr=performance.cagr,
            sharpe=performance.sharpe,
            sortino=performance.sortino,
            max_drawdown=performance.max_drawdown.depth,
            bars_in=backtest.bars_in,
            bars_used=backtest.bars_used,
            fill_count=len(backtest.fills),
            traded_notional=backtest.traded_notional,
            metrics=performance.to_dict(),
            attribution=report.to_dict(),
            warnings=[
                *(warning.to_dict() for warning in warnings),
                *(
                    {"code": code, "severity": "INFO", "message": code}
                    for code in backtest.warnings
                ),
            ],
            provenance=provenance.to_dict(),
            equity_curve_key=curve_key,
            fills_key=fills_key,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def _put(self, key: str, payload: object) -> str:
        stored = await self._store.put(
            key,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )
        return stored.key

    # ---------------------------------------------------------------- reads
    async def get_experiment(
        self, experiment_id: uuid.UUID, user_id: uuid.UUID
    ) -> ResearchExperimentORM | None:
        stmt = select(ResearchExperimentORM).where(
            ResearchExperimentORM.id == experiment_id,
            ResearchExperimentORM.user_id == user_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_experiments(
        self,
        user_id: uuid.UUID,
        strategy_name: str | None = None,
        instrument_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ResearchExperimentORM]:
        stmt = select(ResearchExperimentORM).where(ResearchExperimentORM.user_id == user_id)
        if strategy_name is not None:
            stmt = stmt.where(ResearchExperimentORM.strategy_name == strategy_name)
        if instrument_id is not None:
            stmt = stmt.where(ResearchExperimentORM.instrument_id == instrument_id)
        stmt = stmt.order_by(ResearchExperimentORM.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def equity_curve(self, row: ResearchExperimentORM) -> list[dict]:
        if not row.equity_curve_key:
            return []
        return json.loads((await self._store.get(row.equity_curve_key)).decode("utf-8"))

    async def fills(self, row: ResearchExperimentORM) -> list[dict]:
        if not row.fills_key:
            return []
        return json.loads((await self._store.get(row.fills_key)).decode("utf-8"))
