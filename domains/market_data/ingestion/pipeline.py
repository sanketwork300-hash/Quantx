"""Option-chain ingestion pipeline.

    Upload -> Parse -> Validate -> Resolve -> Normalize -> Quality -> Exclusion
           -> Persist -> Retrieve

Two invariants hold at the end of every run, and both are tested:

1. **Row conservation.** ``rows_input == rows_kept + rows_excluded + rows_rejected``.
   It is also a database CHECK constraint on the snapshot row.
2. **Every set-aside row has a reason.** Excluded quotes carry a NOT NULL
   ``exclusion_reason`` plus their full flag list; rejected rows carry a
   ``RejectionReason`` and their source row number. Nothing disappears quietly.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from decimal import Decimal

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.models import MULTIPLIER_ASSUMED, Instrument, make_instrument
from domains.instruments.service import InstrumentService
from domains.market_data.ingestion.column_mapping import (
    OPTION_CHAIN_FIELDS,
    ColumnMapping,
)
from domains.market_data.ingestion.parser import ParseResult, TabularParser
from domains.market_data.ingestion.validator import (
    OptionChainRowValidator,
    RejectedRow,
    RejectionReason,
    ValidatedOptionRow,
)
from domains.market_data.models import OptionQuote, Quote
from domains.market_data.quality.config import MarketDataQualityConfig
from domains.market_data.quality.engine import MarketDataQualityEngine, QuoteContext
from domains.market_data.quality.flags import MarketDataQuality, Severity
from domains.market_data.repository import MarketDataRepository, PersistableOptionQuote
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from quant.numerical.tolerances import clamp
from quant.statistics.scoring import weighted_geometric_mean

#: Version of the ingestion + quality methodology. Bumped whenever a change can
#: move a score or an exclusion decision, so a stored snapshot always names the
#: rules that produced it.
INGESTION_MODEL_VERSION = "option-chain-ingestion@1.0.0"
QUALITY_MODEL_VERSION = "market-data-quality@1.0.0"


class IngestionWarningCode:
    NO_ROWS = "INGESTION_NO_ROWS"
    ROWS_TRUNCATED = "INGESTION_ROWS_TRUNCATED"
    PARSE_ERRORS = "INGESTION_PARSE_ERRORS"
    ROWS_REJECTED = "INGESTION_ROWS_REJECTED"
    ALL_ROWS_EXCLUDED = "INGESTION_ALL_ROWS_EXCLUDED"
    MISSING_UNDERLYING_PRICE = "INGESTION_MISSING_UNDERLYING_PRICE"
    EXPIRY_TIME_ASSUMED = "INGESTION_EXPIRY_TIME_ASSUMED"
    EXPIRY_TIME_UNKNOWN = "INGESTION_EXPIRY_TIME_UNKNOWN"
    CARRY_ASSUMPTION_USED = "INGESTION_CARRY_ASSUMPTION_USED"
    CARRY_ASSUMPTION_UNAVAILABLE = "INGESTION_CARRY_ASSUMPTION_UNAVAILABLE"
    MULTIPLIER_ASSUMED = "INGESTION_MULTIPLIER_ASSUMED"
    UNMAPPED_COLUMNS = "INGESTION_UNMAPPED_COLUMNS"


@dataclass(frozen=True, slots=True)
class UnderlyingSpec:
    symbol: str
    exchange: str
    asset_class: AssetClass = AssetClass.INDEX
    currency: str = "INR"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Contract terms that the chain file does not carry.

    ``multiplier`` is deliberately optional and defaults to ``None`` rather than
    to a plausible number. Build spec 1.1 forbids fabricating multipliers, and a
    wrong one silently scales every Greek and margin number downstream, so an
    absent multiplier is recorded as an assumption instead of being guessed.
    """

    multiplier: Decimal | None = None
    tick_size: Decimal = Decimal("0.05")
    lot_size: Decimal = Decimal(1)
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_type: SettlementType = SettlementType.CASH
    #: Settlement instant on the expiry date. ``None`` leaves the expiry instant
    #: unknown, which downstream code must treat as unknown rather than assume.
    expiry_time_utc: time | None = None


@dataclass(frozen=True, slots=True)
class IngestionOptions:
    exclusion_severity_threshold: Severity = Severity.ERROR
    create_missing_instruments: bool = True
    source_label: str = "user-upload"


@dataclass(frozen=True, slots=True)
class OptionChainIngestionRequest:
    user_id: uuid.UUID
    underlying: UnderlyingSpec
    as_of: datetime
    column_mapping: ColumnMapping
    contract: ContractSpec = field(default_factory=ContractSpec)
    options: IngestionOptions = field(default_factory=IngestionOptions)
    underlying_price: Decimal | None = None
    #: Carry assumption for the option bound checks. Supplying both enables the
    #: sub-intrinsic check; omitting them keeps the checks assumption-free.
    risk_free_rate: float | None = None
    dividend_yield: float | None = None
    upload_id: uuid.UUID | None = None
    dataset_digest: str | None = None
    provider: str = "csv"


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of: datetime
    rows_input: int
    rows_kept: int
    rows_excluded: int
    rows_rejected: int
    exclusion_counts: dict[str, int]
    rejection_counts: dict[str, int]
    flag_counts: dict[str, int]
    aggregate_quality: MarketDataQuality
    rejected_rows: tuple[RejectedRow, ...]
    expiries: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "snapshot_id": str(self.snapshot_id),
            "underlying_id": str(self.underlying_id),
            "as_of_timestamp": self.as_of.isoformat(),
            "counts": {
                "input": self.rows_input,
                "kept": self.rows_kept,
                "excluded": self.rows_excluded,
                "rejected": self.rows_rejected,
            },
            "exclusion_counts": self.exclusion_counts,
            "rejection_counts": self.rejection_counts,
            "flag_counts": self.flag_counts,
            "aggregate_quality": self.aggregate_quality.to_dict(),
            "rejected_rows": [row.to_dict() for row in self.rejected_rows],
            "expiries": list(self.expiries),
        }


class OptionChainIngestionPipeline:
    def __init__(
        self,
        instrument_service: InstrumentService,
        repository: MarketDataRepository,
        quality_config: MarketDataQualityConfig | None = None,
        max_rows: int = 500_000,
        code_commit: str = "unknown",
    ) -> None:
        self._instruments = instrument_service
        self._repository = repository
        self._quality_config = quality_config or MarketDataQualityConfig()
        self._quality = MarketDataQualityEngine(self._quality_config)
        self._parser = TabularParser(OPTION_CHAIN_FIELDS, max_rows=max_rows)
        self._code_commit = code_commit

    # -------------------------------------------------------------- preview
    def preview(
        self, data: bytes, mapping: ColumnMapping, limit: int
    ) -> tuple[ParseResult, tuple[str, ...]]:
        """Parse a sample without persisting anything.

        Mandatory before commit: the user must see how their columns were
        interpreted, because a misread column produces a plausible, wrong chain
        and no error at all.
        """
        result = self._parser.parse(data, mapping, limit=limit)
        return result, mapping.missing_required(OPTION_CHAIN_FIELDS)

    # --------------------------------------------------------------- ingest
    async def ingest(
        self, data: bytes, request: OptionChainIngestionRequest
    ) -> AnalyticalResult[IngestionSummary]:
        warnings: list[AnalyticalWarning] = []
        self._apply_carry_assumption(request)

        parse_result = self._parser.parse(data, request.column_mapping)
        rows_input = parse_result.row_count + len(parse_result.errors)

        if parse_result.truncated:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.ROWS_TRUNCATED,
                    "The file exceeded the configured row cap and was truncated.",
                    rows_parsed=parse_result.row_count,
                )
            )
        unmapped = request.column_mapping.unmapped_columns(parse_result.headers)
        if unmapped:
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.UNMAPPED_COLUMNS,
                    f"{len(unmapped)} column(s) in the file were not mapped and were ignored.",
                    columns=list(unmapped),
                )
            )

        rejected: list[RejectedRow] = [
            RejectedRow(
                row_number=error.row_number,
                reason=RejectionReason.UNPARSEABLE_ROW,
                message=error.message,
                raw=error.raw,
            )
            for error in parse_result.errors
        ]

        validator = OptionChainRowValidator(expected_symbol=request.underlying.symbol)
        validated: list[ValidatedOptionRow] = []
        for row in parse_result.rows:
            outcome = validator.validate(row)
            if isinstance(outcome, RejectedRow):
                rejected.append(outcome)
            else:
                validated.append(outcome)

        underlying = await self._resolve_underlying(request)
        underlying_price = self._resolve_underlying_price(request, validated, warnings)

        (
            persistable,
            kept_quality,
            flag_counts,
            exclusion_counts,
            extra_rejected,
        ) = await self._build_quotes(validated, request, underlying, underlying_price)
        rejected.extend(extra_rejected)

        rows_kept = sum(1 for item in persistable if not item.excluded)
        rows_excluded = sum(1 for item in persistable if item.excluded)
        rows_rejected = len(rejected)

        aggregate = self._aggregate_quality(kept_quality)
        self._collect_warnings(
            warnings, request, persistable, rows_kept, rows_input, rejected, flag_counts
        )

        provenance = self._build_provenance(request, parse_result)
        snapshot = await self._repository.create_chain_snapshot(
            user_id=request.user_id,
            underlying_id=underlying.id,
            as_of_timestamp=request.as_of,
            source=f"{request.provider}:{request.options.source_label}",
            provider=request.provider,
            dataset_digest=request.dataset_digest,
            upload_id=request.upload_id,
            underlying_price=underlying_price,
            rows_input=rows_input,
            rows_kept=rows_kept,
            rows_excluded=rows_excluded,
            rows_rejected=rows_rejected,
            quality_summary={
                "aggregate": aggregate.to_dict(),
                "flag_counts": flag_counts,
                "exclusion_counts": exclusion_counts,
                "rejection_counts": dict(Counter(str(row.reason) for row in rejected)),
                "rejected_rows": [row.to_dict() for row in rejected[:200]],
            },
            provenance=provenance.to_dict(),
        )
        await self._repository.add_option_quotes(snapshot.id, persistable)
        await self._repository.add_quality_report(
            scope_type="OPTION_CHAIN_SNAPSHOT",
            scope_id=snapshot.id,
            stale_score=aggregate.stale_score,
            spread_score=aggregate.spread_score,
            liquidity_score=aggregate.liquidity_score,
            consistency_score=aggregate.consistency_score,
            completeness_score=aggregate.completeness_score,
            overall_score=aggregate.overall_score,
            flag_counts=flag_counts,
            flags=[],
            provenance=provenance.to_dict(),
        )

        summary = IngestionSummary(
            snapshot_id=snapshot.id,
            underlying_id=underlying.id,
            as_of=request.as_of,
            rows_input=rows_input,
            rows_kept=rows_kept,
            rows_excluded=rows_excluded,
            rows_rejected=rows_rejected,
            exclusion_counts=exclusion_counts,
            rejection_counts=dict(Counter(str(row.reason) for row in rejected)),
            flag_counts=flag_counts,
            aggregate_quality=aggregate,
            rejected_rows=tuple(rejected[:200]),
            expiries=tuple(sorted({str(item.expiry) for item in persistable})),
        )

        if rows_kept == 0:
            return AnalyticalResult.partial(summary, provenance, tuple(warnings))
        return AnalyticalResult.ok(summary, provenance, tuple(warnings))

    # ------------------------------------------------------------- internals
    def _apply_carry_assumption(self, request: OptionChainIngestionRequest) -> None:
        """Fold the request's carry assumption into the quality configuration.

        Kept out of the config default so that "no assumption" stays the
        default and a caller must opt in to the carry-dependent bounds.
        """
        if request.risk_free_rate is None and request.dividend_yield is None:
            return
        self._quality_config = replace(
            self._quality_config,
            assumed_risk_free_rate=request.risk_free_rate or 0.0,
            assumed_dividend_yield=request.dividend_yield or 0.0,
        )
        self._quality = MarketDataQualityEngine(self._quality_config)

    async def _resolve_underlying(self, request: OptionChainIngestionRequest) -> Instrument:
        spec = request.underlying
        underlying = make_instrument(
            asset_class=spec.asset_class,
            exchange=spec.exchange,
            symbol=spec.symbol,
            currency=spec.currency,
            metadata={"created_by": "option_chain_ingestion"},
        )
        existing = await self._instruments.get(underlying.id)
        if existing is not None:
            return existing
        return await self._instruments.upsert(underlying)

    def _resolve_underlying_price(
        self,
        request: OptionChainIngestionRequest,
        rows: list[ValidatedOptionRow],
        warnings: list[AnalyticalWarning],
    ) -> Decimal | None:
        if request.underlying_price is not None:
            return request.underlying_price
        observed = [row.underlying_price for row in rows if row.underlying_price is not None]
        if observed:
            # Files repeat the underlying on every row; take the first observed
            # value rather than averaging, so the stored number is one the file
            # actually contained.
            return observed[0]
        warnings.append(
            AnalyticalWarning.warn(
                IngestionWarningCode.MISSING_UNDERLYING_PRICE,
                "No underlying price was supplied or found in the file; option "
                "no-arbitrage bound checks were skipped for every quote.",
            )
        )
        return None

    async def _build_quotes(
        self,
        rows: list[ValidatedOptionRow],
        request: OptionChainIngestionRequest,
        underlying: Instrument,
        underlying_price: Decimal | None,
    ) -> tuple[
        list[PersistableOptionQuote],
        list[MarketDataQuality],
        dict[str, int],
        dict[str, int],
        list[RejectedRow],
    ]:
        contract = request.contract
        threshold = request.options.exclusion_severity_threshold
        multiplier_assumed = contract.multiplier is None
        multiplier = contract.multiplier or Decimal(1)

        seen: set[tuple[object, Decimal, OptionType]] = set()
        persistable: list[PersistableOptionQuote] = []
        kept_quality: list[MarketDataQuality] = []
        flag_counter: Counter[str] = Counter()
        exclusion_counter: Counter[str] = Counter()
        rejected: list[RejectedRow] = []

        instruments_to_upsert: list[Instrument] = []
        prepared: list[tuple[ValidatedOptionRow, Instrument, OptionQuote, bool]] = []

        for row in rows:
            metadata = {"created_by": "option_chain_ingestion"}
            if multiplier_assumed:
                metadata[MULTIPLIER_ASSUMED] = "platform_default"

            instrument = make_instrument(
                asset_class=AssetClass.OPTION,
                exchange=underlying.exchange,
                symbol=underlying.symbol,
                currency=underlying.currency,
                multiplier=multiplier,
                tick_size=contract.tick_size,
                lot_size=contract.lot_size,
                expiry=row.expiry,
                strike=row.strike,
                option_type=row.option_type,
                exercise_style=contract.exercise_style,
                settlement_type=contract.settlement_type,
                underlying_id=underlying.id,
                metadata=metadata,
            )

            if not request.options.create_missing_instruments:
                if await self._instruments.get(instrument.id) is None:
                    rejected.append(
                        RejectedRow(
                            row_number=row.row_number,
                            reason=RejectionReason.INSTRUMENT_UNRESOLVED,
                            message=(
                                f"Contract {instrument.canonical_key} is not in the "
                                "instrument master and instrument creation was not "
                                "requested."
                            ),
                            raw=row.raw,
                        )
                    )
                    continue
            else:
                instruments_to_upsert.append(instrument)

            key = (row.expiry, row.strike, row.option_type)
            is_duplicate = key in seen
            seen.add(key)

            exchange_timestamp = row.exchange_timestamp or request.as_of
            expiry_timestamp = (
                datetime.combine(row.expiry, contract.expiry_time_utc, tzinfo=UTC)
                if contract.expiry_time_utc is not None
                else None
            )
            quote = Quote(
                instrument_id=instrument.id,
                exchange_timestamp=exchange_timestamp,
                receive_timestamp=request.as_of,
                source=f"{request.provider}:{request.options.source_label}",
                bid_price=row.bid_price,
                bid_size=row.bid_size,
                ask_price=row.ask_price,
                ask_size=row.ask_size,
                last_price=row.last_price,
                volume=row.volume,
                open_interest=row.open_interest,
                sequence_number=row.sequence_number,
            )
            option_quote = OptionQuote(
                quote=quote,
                underlying_id=underlying.id,
                expiry=row.expiry,
                # The instrument's normalised strike, so the persisted quote and
                # the canonical key never disagree about the same number.
                strike=instrument.strike,
                option_type=row.option_type,
                expiry_timestamp=expiry_timestamp,
                underlying_price=row.underlying_price or underlying_price,
            )
            prepared.append((row, instrument, option_quote, is_duplicate))

        if instruments_to_upsert:
            await self._instruments.upsert_many(instruments_to_upsert)

        for row, instrument, option_quote, is_duplicate in prepared:
            quality = self._quality.score_option_quote(
                option_quote,
                QuoteContext(
                    asset_class=AssetClass.OPTION,
                    as_of=request.as_of,
                    tick_size=instrument.tick_size,
                    is_duplicate=is_duplicate,
                    multiplier_assumed=multiplier_assumed,
                ),
            )
            for flag in quality.flags:
                flag_counter[str(flag.code)] += 1

            primary = quality.primary_flag(threshold)
            excluded = primary is not None
            if excluded:
                exclusion_counter[str(primary.code)] += 1
            else:
                kept_quality.append(quality)

            persistable.append(
                PersistableOptionQuote(
                    instrument_id=instrument.id,
                    underlying_id=option_quote.underlying_id,
                    source_row_number=row.row_number,
                    expiry=option_quote.expiry,
                    strike=option_quote.strike,
                    option_type=str(option_quote.option_type),
                    exchange_timestamp=option_quote.quote.exchange_timestamp,
                    receive_timestamp=option_quote.quote.receive_timestamp,
                    bid_price=option_quote.quote.bid_price,
                    bid_size=option_quote.quote.bid_size,
                    ask_price=option_quote.quote.ask_price,
                    ask_size=option_quote.quote.ask_size,
                    last_price=option_quote.quote.last_price,
                    volume=option_quote.quote.volume,
                    open_interest=option_quote.quote.open_interest,
                    sequence_number=option_quote.quote.sequence_number,
                    underlying_price=option_quote.underlying_price,
                    quality=quality,
                    excluded=excluded,
                    exclusion_reason=str(primary.code) if primary else None,
                )
            )

        return (
            persistable,
            kept_quality,
            dict(flag_counter),
            dict(exclusion_counter),
            rejected,
        )

    def _aggregate_quality(self, qualities: list[MarketDataQuality]) -> MarketDataQuality:
        if not qualities:
            return MarketDataQuality(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ())

        def mean(attribute: str) -> float:
            return sum(getattr(item, attribute) for item in qualities) / len(qualities)

        config = self._quality_config
        stale = mean("stale_score")
        spread = mean("spread_score")
        liquidity = mean("liquidity_score")
        consistency = mean("consistency_score")
        completeness = mean("completeness_score")
        overall = weighted_geometric_mean(
            [
                clamp(stale, 0.0, 1.0),
                clamp(spread, 0.0, 1.0),
                clamp(liquidity, 0.0, 1.0),
                clamp(consistency, 0.0, 1.0),
                clamp(completeness, 0.0, 1.0),
            ],
            [
                config.weight_stale,
                config.weight_spread,
                config.weight_liquidity,
                config.weight_consistency,
                config.weight_completeness,
            ],
        )
        return MarketDataQuality(
            stale_score=stale,
            spread_score=spread,
            liquidity_score=liquidity,
            consistency_score=consistency,
            completeness_score=completeness,
            overall_score=overall,
            flags=(),
        )

    def _collect_warnings(
        self,
        warnings: list[AnalyticalWarning],
        request: OptionChainIngestionRequest,
        persistable: list[PersistableOptionQuote],
        rows_kept: int,
        rows_input: int,
        rejected: list[RejectedRow],
        flag_counts: dict[str, int],
    ) -> None:
        if rows_input == 0:
            warnings.append(
                AnalyticalWarning.error(
                    IngestionWarningCode.NO_ROWS, "The file contained no data rows."
                )
            )
        if rejected:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.ROWS_REJECTED,
                    f"{len(rejected)} row(s) could not be turned into a quote; each is "
                    "reported with its source row number and reason.",
                    count=len(rejected),
                )
            )
        if persistable and rows_kept == 0:
            warnings.append(
                AnalyticalWarning.error(
                    IngestionWarningCode.ALL_ROWS_EXCLUDED,
                    "Every quote in this chain was excluded by the quality policy.",
                )
            )
        if request.contract.expiry_time_utc is None:
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.EXPIRY_TIME_UNKNOWN,
                    "No settlement time was supplied, so the expiry instant is "
                    "unknown and time to expiry is not defined for these quotes.",
                )
            )
        else:
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.EXPIRY_TIME_ASSUMED,
                    "Time to expiry uses the supplied settlement time on the expiry date.",
                    expiry_time_utc=request.contract.expiry_time_utc.isoformat(),
                )
            )
        if request.contract.multiplier is None:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.MULTIPLIER_ASSUMED,
                    "No contract multiplier was supplied; 1 was recorded and flagged as "
                    "an assumption. Greeks and margin scale with this value.",
                )
            )
        config = self._quality_config
        if config.carry_is_known:
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.CARRY_ASSUMPTION_USED,
                    "No-arbitrage bound checks used the supplied carry assumption "
                    f"r={config.assumed_risk_free_rate:.4f}, "
                    f"q={config.assumed_dividend_yield:.4f}. These are stated "
                    "assumptions, not observed market data.",
                    assumed_risk_free_rate=config.assumed_risk_free_rate,
                    assumed_dividend_yield=config.assumed_dividend_yield,
                )
            )
        else:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.CARRY_ASSUMPTION_UNAVAILABLE,
                    "No risk-free rate or dividend yield was supplied, so only the "
                    "assumption-free option bounds were checked (call <= spot, "
                    "put <= strike, price >= 0). Sub-intrinsic pricing was not "
                    "checked: without a discount curve a deep in-the-money "
                    "European put legitimately trades below its undiscounted "
                    "intrinsic value.",
                )
            )

    def _build_provenance(
        self, request: OptionChainIngestionRequest, parse_result: ParseResult
    ) -> Provenance:
        return Provenance.now(
            code_commit=self._code_commit,
            market_state_timestamp=request.as_of,
            market_data_sources=(f"{request.provider}:{request.options.source_label}",),
            dataset_versions=(
                {request.provider: request.dataset_digest} if request.dataset_digest else {}
            ),
            model_versions={
                "ingestion": INGESTION_MODEL_VERSION,
                "quality": QUALITY_MODEL_VERSION,
            },
            parameters={
                "column_mapping": request.column_mapping.to_dict(),
                "headers": parse_result.headers,
                "exclusion_severity_threshold": str(request.options.exclusion_severity_threshold),
                "create_missing_instruments": request.options.create_missing_instruments,
                "underlying": {
                    "symbol": request.underlying.symbol,
                    "exchange": request.underlying.exchange,
                    "asset_class": str(request.underlying.asset_class),
                    "currency": request.underlying.currency,
                },
                "contract": {
                    "multiplier": (
                        format(request.contract.multiplier, "f")
                        if request.contract.multiplier is not None
                        else None
                    ),
                    "tick_size": format(request.contract.tick_size, "f"),
                    "lot_size": format(request.contract.lot_size, "f"),
                    "exercise_style": str(request.contract.exercise_style),
                    "settlement_type": str(request.contract.settlement_type),
                    "expiry_time_utc": (
                        request.contract.expiry_time_utc.isoformat()
                        if request.contract.expiry_time_utc
                        else None
                    ),
                },
                "quality_config": self._quality_config.to_provenance(),
            },
        )
