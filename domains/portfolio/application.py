"""Portfolio application service: import, valuation, persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.service import InstrumentService
from domains.market_data.ingestion.column_mapping import ColumnMapping, infer_mapping
from domains.portfolio.enums import PositionSource
from domains.portfolio.importer import (
    IMPORT_MODEL_VERSION,
    POSITION_FIELDS,
    ImportDefaults,
    ImportPreview,
    PositionImporter,
    commit_instruments,
)
from domains.portfolio.models import PortfolioValuation
from domains.portfolio.repository import PortfolioRepository
from domains.portfolio.service import PortfolioNotFound, PortfolioService
from domains.portfolio.valuation import (
    VALUATION_MODEL_VERSION,
    PortfolioValuationService,
    ValuationContext,
)
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from infrastructure.settings import Settings


class PortfolioApplicationError(Exception):
    pass


class ImportRefused(PortfolioApplicationError):
    """A commit was attempted while rows were still ambiguous."""


class PortfolioWarningCode:
    AMBIGUOUS_ROWS = "PORTFOLIO_IMPORT_AMBIGUOUS_ROWS"
    INVALID_ROWS = "PORTFOLIO_IMPORT_INVALID_ROWS"
    INSTRUMENTS_CREATED = "PORTFOLIO_IMPORT_INSTRUMENTS_CREATED"
    NO_MARKET_DATA = "PORTFOLIO_NO_MARKET_DATA"
    UNVALUED_POSITIONS = "PORTFOLIO_UNVALUED_POSITIONS"
    MODEL_PRICED = "PORTFOLIO_MODEL_PRICED_POSITIONS"
    STALE_QUOTES = "PORTFOLIO_STALE_QUOTES"
    NO_SETTLEMENT_TIME = "PORTFOLIO_NO_SETTLEMENT_TIME"


@dataclass(frozen=True, slots=True)
class ImportPreviewResult:
    preview: ImportPreview
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    missing_required: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComputedValuation:
    """A valuation and everything it was computed from.

    Returned so a caller that needs to do more with the same book — a risk run
    repricing it under a shock — works from the *same* objects rather than
    recomputing and hoping the two agree.
    """

    portfolio: object
    positions: list
    instruments: dict
    context: ValuationContext
    valuation: PortfolioValuation
    warnings: tuple[AnalyticalWarning, ...]


@dataclass(frozen=True, slots=True)
class ValuePortfolioParams:
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    settlement_time_utc: time | None = None
    as_of: datetime | None = None


class PortfolioApplicationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self.portfolios = PortfolioService(session)
        self.repository = PortfolioRepository(session)
        self.instruments = InstrumentService(session)

    # ------------------------------------------------------------------ import
    async def preview_import(
        self,
        data: bytes,
        mapping: ColumnMapping | None,
        defaults: ImportDefaults,
        limit: int | None = None,
    ) -> ImportPreviewResult:
        """Parse and resolve without writing anything.

        Preview is mandatory before a commit because instrument resolution is
        where a portfolio silently acquires the wrong contract, and the only
        safe place to catch that is in front of the user.
        """
        from domains.market_data.ingestion.parser import TabularParser

        headers = TabularParser.read_headers(data)
        inferred = infer_mapping(headers, POSITION_FIELDS)
        applied = mapping or inferred

        preview = await PositionImporter(self.instruments).preview(
            data, applied, defaults, limit=limit
        )
        return ImportPreviewResult(
            preview=preview,
            headers=headers,
            inferred_mapping=inferred.to_dict(),
            applied_mapping=applied.to_dict(),
            missing_required=applied.missing_required(POSITION_FIELDS),
        )

    async def commit_import(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        data: bytes,
        mapping: ColumnMapping,
        defaults: ImportDefaults,
        replace_existing: bool = False,
    ) -> AnalyticalResult[dict]:
        """Commit an import, refusing while any row is ambiguous.

        Refusing is the whole point: an ambiguous row committed under a guess is
        a portfolio holding the wrong contract with no error anywhere.
        """
        if await self.portfolios.get(portfolio_id, user_id) is None:
            raise PortfolioNotFound(str(portfolio_id))

        preview = (await self.preview_import(data, mapping, defaults)).preview
        warnings: list[AnalyticalWarning] = []

        if preview.ambiguous:
            raise ImportRefused(
                f"{len(preview.ambiguous)} row(s) match more than one contract. "
                "Choose the intended instrument for each; the platform will not "
                "pick one for you."
            )
        if preview.invalid:
            warnings.append(
                AnalyticalWarning.warn(
                    PortfolioWarningCode.INVALID_ROWS,
                    f"{len(preview.invalid)} row(s) could not become a position; "
                    "each is reported with its source row number and reason.",
                    count=len(preview.invalid),
                )
            )

        created = commit_instruments(preview)
        if created:
            await self.instruments.upsert_many(created)
            warnings.append(
                AnalyticalWarning.info(
                    PortfolioWarningCode.INSTRUMENTS_CREATED,
                    f"{len(created)} contract(s) were added to the instrument master.",
                    count=len(created),
                )
            )

        if replace_existing:
            for existing in await self.repository.list_positions(portfolio_id):
                await self.repository.delete_position(existing.id)

        rows = []
        for row in preview.resolved:
            rows.append(
                {
                    "portfolio_id": portfolio_id,
                    "instrument_id": row.instrument.id,
                    "quantity": row.quantity,
                    "side": str(row.side),
                    "average_price": row.average_price,
                    "source": str(PositionSource.CSV_IMPORT),
                    "strategy_tag": row.strategy_tag,
                    "position_metadata": {"source_row_number": row.row_number},
                }
            )
        await self.repository.add_positions(rows)

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            model_versions={"import": IMPORT_MODEL_VERSION},
            parameters={
                "portfolio_id": str(portfolio_id),
                "column_mapping": mapping.to_dict(),
                "defaults": defaults.to_provenance(),
                "replace_existing": replace_existing,
            },
        )
        payload = preview.to_dict()
        payload["committed"] = len(rows)
        payload["instruments_created"] = len(created)
        return AnalyticalResult.ok(payload, provenance, tuple(warnings))

    # --------------------------------------------------------------- valuation
    async def compute_valuation(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        params: ValuePortfolioParams,
        composer,
    ) -> ComputedValuation | None:
        """Value a portfolio without persisting anything.

        ``None`` means no market data was available for any underlying, which is
        a stateable outcome and not an exception.
        """
        portfolio = await self.portfolios.get(portfolio_id, user_id)
        if portfolio is None:
            raise PortfolioNotFound(str(portfolio_id))

        positions = await self.portfolios.positions(portfolio_id)
        instruments = {}
        underlyings: set[uuid.UUID] = set()
        for position in positions:
            instrument = await self.instruments.get(position.instrument_id)
            if instrument is None:
                continue
            instruments[instrument.id] = instrument
            underlyings.add(instrument.underlying_id or instrument.id)

        warnings: list[AnalyticalWarning] = []
        if params.settlement_time_utc is None:
            warnings.append(
                AnalyticalWarning.warn(
                    PortfolioWarningCode.NO_SETTLEMENT_TIME,
                    "No settlement time was supplied, so time to expiry is "
                    "undefined and option Greeks were not produced.",
                )
            )

        context: ValuationContext | None = await composer.build(
            user_id,
            underlyings,
            portfolio.base_currency,
            as_of=params.as_of,
            risk_free_rate=params.risk_free_rate,
            dividend_yield=params.dividend_yield,
            settlement_time_utc=params.settlement_time_utc,
        )
        if context is None:
            return None

        valuation = PortfolioValuationService().value(portfolio_id, positions, instruments, context)
        warnings.extend(self._valuation_warnings(valuation))
        return ComputedValuation(
            portfolio=portfolio,
            positions=positions,
            instruments=instruments,
            context=context,
            valuation=valuation,
            warnings=tuple(warnings),
        )

    async def persist_valuation(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        computed: ComputedValuation,
        provenance: Provenance | None = None,
    ):
        """Store a computed valuation and its positions.

        Separate from computing it so a risk run can persist the very valuation
        it repriced, rather than a second one taken a moment later that would
        not be the book the risk numbers describe.
        """
        valuation = computed.valuation
        context = computed.context
        if provenance is None:
            provenance = Provenance.now(
                code_commit=self._settings.code_commit,
                market_state_id=context.market_state.state_id,
                market_state_timestamp=context.as_of,
                market_data_sources=context.market_state.sources,
                dataset_versions=dict(context.market_state.data_versions),
                model_versions={
                    "valuation": VALUATION_MODEL_VERSION,
                    "pricing": "black-scholes-merton@1.0.0",
                },
                parameters={
                    "portfolio_id": str(portfolio_id),
                    "context": context.to_provenance(),
                },
            )

        row = await self.repository.create_valuation(
            portfolio_id=portfolio_id,
            user_id=user_id,
            as_of_timestamp=context.as_of,
            base_currency=valuation.base_currency,
            market_state_id=context.market_state.state_id,
            positions=len(valuation.valuations),
            valued=valuation.valued,
            base_market_value=valuation.base_market_value,
            unrealized_pnl=valuation.unrealized_pnl,
            gross_exposure=valuation.gross_exposure,
            net_exposure=valuation.net_exposure,
            delta=valuation.greeks.delta,
            gamma=valuation.greeks.gamma,
            vega_per_vol_point=valuation.greeks.vega_per_vol_point,
            theta_per_day=valuation.greeks.theta_per_day,
            rho_per_bp=valuation.greeks.rho_per_bp,
            valuation_methods=valuation.method_counts,
            aggregates=[bucket.to_dict() for bucket in valuation.aggregates],
            provenance=provenance.to_dict(),
        )
        await self.repository.add_position_valuations(row.id, valuation.valuations)
        return row

    async def value_portfolio(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID,
        params: ValuePortfolioParams,
        composer,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        computed = await self.compute_valuation(user_id, portfolio_id, params, composer)
        if computed is None:
            return (
                AnalyticalResult.failed(
                    Provenance.now(
                        code_commit=self._settings.code_commit,
                        model_versions={"valuation": VALUATION_MODEL_VERSION},
                        parameters={"portfolio_id": str(portfolio_id)},
                    ),
                    (
                        AnalyticalWarning.error(
                            PortfolioWarningCode.NO_MARKET_DATA,
                            "No market data is available for any underlying in this "
                            "portfolio, so nothing could be valued.",
                        ),
                    ),
                ),
                None,
            )

        context = computed.context
        valuation = computed.valuation
        warnings = list(computed.warnings)

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_id=context.market_state.state_id,
            market_state_timestamp=context.as_of,
            market_data_sources=context.market_state.sources,
            dataset_versions=dict(context.market_state.data_versions),
            model_versions={
                "valuation": VALUATION_MODEL_VERSION,
                "pricing": "black-scholes-merton@1.0.0",
            },
            parameters={
                "portfolio_id": str(portfolio_id),
                "context": context.to_provenance(),
            },
        )

        row = await self.persist_valuation(user_id, portfolio_id, computed, provenance)

        payload = valuation.to_dict(include_positions=False)
        payload["valuation_id"] = str(row.id)
        status = AnalyticalResult.ok(payload, provenance, tuple(warnings))
        if valuation.valued < len(valuation.valuations):
            status = AnalyticalResult.partial(payload, provenance, tuple(warnings))
        return status, row.id

    @staticmethod
    def _valuation_warnings(valuation: PortfolioValuation) -> list[AnalyticalWarning]:
        warnings: list[AnalyticalWarning] = []
        counts = valuation.method_counts

        unvalued = counts.get("UNAVAILABLE", 0)
        if unvalued:
            warnings.append(
                AnalyticalWarning.warn(
                    PortfolioWarningCode.UNVALUED_POSITIONS,
                    f"{unvalued} position(s) could not be valued. They contribute "
                    "nothing to the totals and are listed with their reason rather "
                    "than counted as zero.",
                    count=unvalued,
                )
            )
        modelled = counts.get("MODEL_REFERENCE", 0)
        if modelled:
            warnings.append(
                AnalyticalWarning.info(
                    PortfolioWarningCode.MODEL_PRICED,
                    f"{modelled} position(s) had no usable market price and were "
                    "valued from the fitted surface. Those values are model "
                    "outputs, not observations.",
                    count=modelled,
                )
            )
        stale = counts.get("STALE_MARKET", 0)
        if stale:
            warnings.append(
                AnalyticalWarning.warn(
                    PortfolioWarningCode.STALE_QUOTES,
                    f"{stale} position(s) were valued on a stale quote.",
                    count=stale,
                )
            )
        return warnings

    # ----------------------------------------------------------------- reads
    async def get_valuation(self, valuation_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.get_valuation(valuation_id, user_id)

    async def latest_valuation(self, portfolio_id: uuid.UUID, user_id: uuid.UUID):
        return await self.repository.latest_valuation(portfolio_id, user_id)
