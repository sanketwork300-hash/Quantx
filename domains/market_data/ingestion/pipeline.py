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
from datetime import UTC, date, datetime, time
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
    infer_mapping,
)
from domains.market_data.ingestion.layout import (
    ChainLayout,
    LayoutDetection,
    TwoSidedLayout,
)
from domains.market_data.ingestion.layout import (
    detect as detect_layout,
)
from domains.market_data.ingestion.layout import (
    split as split_two_sided,
)
from domains.market_data.ingestion.parser import (
    DateOrder,
    ParseResult,
    TabularParser,
    detect_delimiter,
)
from domains.market_data.ingestion.reading import (
    ReadingRefused,
    assess_rejections,
    describe,
)
from domains.market_data.ingestion.reading import (
    obstacle as reading_obstacle,
)
from domains.market_data.ingestion.validator import (
    NON_OPTION_TOKENS,
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
    TWO_SIDED_LAYOUT = "INGESTION_TWO_SIDED_LAYOUT"
    LAYOUT_AUTO_DETECTED = "INGESTION_LAYOUT_AUTO_DETECTED"
    MAPPING_INFERRED = "INGESTION_MAPPING_INFERRED"
    EXPIRY_FROM_FILENAME = "INGESTION_EXPIRY_FROM_FILENAME"
    CONTRACTS_ALREADY_EXPIRED = "INGESTION_CONTRACTS_ALREADY_EXPIRED"
    OPTIONAL_VALUES_UNREADABLE = "INGESTION_OPTIONAL_VALUES_UNREADABLE"
    DELIMITER_DETECTED = "INGESTION_DELIMITER_DETECTED"
    HEADER_ROW_DETECTED = "INGESTION_HEADER_ROW_DETECTED"
    DATE_ORDER = "INGESTION_DATE_ORDER"
    TIMESTAMP_TIMEZONE_ASSUMED = "INGESTION_TIMESTAMP_TIMEZONE_ASSUMED"


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
    #: Set when the file is a two-sided chain export (calls left of the strike,
    #: puts right of it). The side is implied by column position, so the layout
    #: must be resolved before the name-based ``column_mapping`` can apply. The
    #: layout also carries the expiry, which such a file names in no column.
    layout: TwoSidedLayout | None = None
    #: Set when ``layout`` was detected from the file rather than named by the
    #: caller. It carries the evidence for that reading, which is reported in
    #: the result's warnings: a misread layout produces a plausible chain and
    #: no error, so the reasoning has to travel with the result.
    layout_detection: LayoutDetection | None = None
    #: Set when ``column_mapping`` was matched to the file's headers rather than
    #: named by the caller. Reported for the same reason a detected layout is:
    #: a column taken for the wrong field produces a plausible chain.
    mapping_inferred: bool = False
    #: Original name of the uploaded file. A two-sided export names its expiry
    #: in no column, and the filename is the only place it appears; it is read
    #: as a *hint*, reported as one, never as a fact from the data.
    filename: str | None = None
    #: Whether a numeric date's first number is the day. Needed only when a
    #: date column does not show it; never assumed when it is not supplied.
    date_order: DateOrder | None = None
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


class ChainExpiredRefused(ValueError):
    """Every contract in the file had expired at the as-of, so nothing was written.

    The file was read correctly; it is the as-of that cannot be right, or the
    chain is one nothing can be computed from. Stored, it would be a snapshot
    with no usable quote in it that then becomes the underlying's latest chain:
    no implied volatility, no surface slice, nothing to scan, and no error
    anywhere near the timestamp that caused it. The as-of is supplied by the
    caller rather than read from the file, so it is the thing to correct.
    """

    code = "ALL_CONTRACTS_EXPIRED"

    def __init__(
        self, expiries: list[str], as_of: datetime, settlement: time | None, quotes: int
    ) -> None:
        judged = (
            f"a settlement time of {settlement.isoformat()} UTC on the expiry date"
            if settlement is not None
            else "the expiry date alone, because no settlement time was supplied"
        )
        super().__init__(
            f"All {quotes} quote(s) in this file are for expiry {', '.join(expiries)}, which "
            f"had already passed at the as-of timestamp {as_of.isoformat()} (judged by "
            f"{judged}). Nothing was ingested: no implied volatility, surface or deviation "
            "scan can be solved from an expired contract. Check the as-of timestamp, which "
            "is supplied by the caller and not read from the file, and set it to when the "
            "chain was captured."
        )
        self.details = {
            "code": self.code,
            "expiries": expiries,
            "as_of_timestamp": as_of.isoformat(),
            "expiry_time_utc": settlement.isoformat() if settlement is not None else None,
            "quotes": quotes,
        }


def _is_expired(expiry: date, as_of: datetime, settlement: time | None) -> bool:
    if settlement is not None:
        return datetime.combine(expiry, settlement, tzinfo=UTC) <= as_of
    # With no settlement time the expiry *instant* is unknown, so only a date
    # strictly in the past is certainly expired. Claiming more would be an
    # assumption about when the venue settles.
    return expiry < as_of.date()


@dataclass(frozen=True, slots=True)
class ReadingPlan:
    """How a file will be read, and whether that was said or worked out.

    The layout is resolved before the mapping, because a two-sided chain repeats
    every header name once per side and so cannot be described by names at all.
    ``detection`` is present whenever the reading was worked out rather than
    stated, and carries the evidence for it.
    """

    layout: TwoSidedLayout | None
    mapping: ColumnMapping
    detection: LayoutDetection | None = None
    mapping_inferred: bool = False

    @property
    def auto_detected(self) -> bool:
        return self.detection is not None and self.layout is not None


def _applied_mapping(
    request: OptionChainIngestionRequest, plan: ReadingPlan | None
) -> ColumnMapping:
    """The mapping the file was actually read with."""
    if plan is None:
        return request.column_mapping
    if plan.layout is not None:
        return plan.layout.identity_mapping()
    return plan.mapping


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
        # A row is a quote if it has a strike, an expiry, a side and a price.
        # An optional column that does not hold what its header suggests -- a
        # ``time`` of ``15:30:00``, a volume of ``1.2K`` -- costs that cell, and
        # is reported, rather than costing the row and with it the file.
        #
        # The order of a numeric date is settled per column rather than per
        # cell, and a future listed beside the options is a row that is not an
        # option rather than one that could not be read.
        self._parser = TabularParser(
            OPTION_CHAIN_FIELDS,
            max_rows=max_rows,
            lenient_optional=True,
            resolve_date_order=True,
            non_option_tokens=NON_OPTION_TOKENS,
        )
        self._code_commit = code_commit

    # ----------------------------------------------------------------- read
    def _resolve_reading(
        self,
        data: bytes,
        mapping: ColumnMapping,
        layout: TwoSidedLayout | None,
        filename: str | None,
        detection: LayoutDetection | None = None,
        mapping_inferred: bool = False,
    ) -> ReadingPlan:
        """Work out how to read the file when the caller did not say.

        This runs only when the caller named no layout and supplied no mapping
        at all. An instruction is never overridden by a guess, and a *partial*
        mapping is still an instruction: the caller knows something about their
        file that a header scan does not, and the useful answer to an incomplete
        one is which field is missing, not a different reading.

        A user who downloads a chain from an exchange and uploads it has nothing
        to say about its columns, and the alternative to reading the file is
        rejecting every row of a perfectly readable one. So the same two steps
        the preview shows are applied here -- the layout, then the mapping -- and
        both are reported in the result's warnings with the evidence for them,
        because the failure mode of a misread file is a plausible chain rather
        than an error.

        When a two-sided chain is detected, the expiry hint carried by the
        filename is applied, because such a file names its expiry in no column.
        It is a hint and is reported as one:
        :attr:`IngestionWarningCode.EXPIRY_FROM_FILENAME` names the date and
        where it came from, so a wrong one is visible rather than silently
        shifting every contract along the term structure.
        """
        if layout is not None:
            return ReadingPlan(layout=layout, mapping=mapping, detection=detection)
        if mapping.to_dict():
            return ReadingPlan(layout=None, mapping=mapping, mapping_inferred=mapping_inferred)

        found = detect_layout(data, filename=filename)
        if found.layout is ChainLayout.TWO_SIDED and found.two_sided is not None:
            resolved = replace(found.two_sided, expiry=found.suggested_expiry)
            return ReadingPlan(layout=resolved, mapping=mapping, detection=found)

        # A long-form file whose columns were never named. Inference is the same
        # step the preview performs and shows; it is applied here so that a file
        # uploaded with nothing said about it is still read, and reported so the
        # user can see which column was taken for which field.
        inferred = infer_mapping(list(found.headers), OPTION_CHAIN_FIELDS)
        if inferred.missing_required(OPTION_CHAIN_FIELDS):
            return ReadingPlan(layout=None, mapping=mapping, detection=found)
        return ReadingPlan(layout=None, mapping=inferred, detection=found, mapping_inferred=True)

    def _read(
        self,
        data: bytes,
        mapping: ColumnMapping,
        layout: TwoSidedLayout | None,
        limit: int | None = None,
        date_order: DateOrder | None = None,
    ) -> tuple[ParseResult, tuple[str, ...]]:
        """Turn a file into parsed rows, using an already-resolved layout.

        A two-sided chain is split into one record per quote *by column index*
        before any name-based mapping runs, because the same header name
        appears on the call side and the put side and a name-keyed reader keeps
        only one of them -- silently giving every call the put's prices.

        Returns the parsed rows and the columns that were read but not mapped
        to any field, which differ per layout and so cannot be derived from the
        mapping alone.
        """
        if layout is None:
            result = self._parser.parse(data, mapping, limit=limit, date_order=date_order)
            return result, mapping.unmapped_columns(result.headers)

        records, headers = split_two_sided(data, layout)
        # A preview limit counts source rows, so that the sample shows whole
        # strikes rather than a call whose put was cut off.
        capped = records if limit is None else records[: limit * 2]
        result = self._parser.parse_records(
            capped, headers, layout.identity_mapping(), limit=None if limit is None else limit * 2
        )
        result = replace(
            result,
            delimiter=detect_delimiter(data.decode("utf-8-sig", errors="replace")),
            header_row=layout.header_row,
        )
        claimed = {
            layout.strike_column,
            *layout.call_columns.values(),
            *layout.put_columns.values(),
            *layout.shared_columns.values(),
        }
        unmapped = tuple(
            headers[index]
            for index in range(len(headers))
            if index not in claimed and headers[index]
        )
        return result, unmapped

    # -------------------------------------------------------------- preview
    def preview(
        self,
        data: bytes,
        mapping: ColumnMapping,
        limit: int,
        layout: TwoSidedLayout | None = None,
        filename: str | None = None,
        date_order: DateOrder | None = None,
    ) -> tuple[ParseResult, tuple[str, ...]]:
        """Parse a sample without persisting anything.

        The same reading the commit path performs, over the file's first rows,
        so what the user is shown and what is then ingested cannot diverge.
        """
        plan = self._resolve_reading(data, mapping, layout, filename)
        result, _ = self._read(data, plan.mapping, plan.layout, limit=limit, date_order=date_order)
        applied = plan.layout.identity_mapping() if plan.layout is not None else plan.mapping
        return result, applied.missing_required(OPTION_CHAIN_FIELDS)

    # --------------------------------------------------------------- ingest
    async def ingest(
        self, data: bytes, request: OptionChainIngestionRequest
    ) -> AnalyticalResult[IngestionSummary]:
        warnings: list[AnalyticalWarning] = []
        self._apply_carry_assumption(request)

        plan = self._resolve_reading(
            data,
            request.column_mapping,
            request.layout,
            request.filename,
            request.layout_detection,
            request.mapping_inferred,
        )
        layout, detection = plan.layout, plan.detection
        parse_result, unmapped = self._read(
            data, plan.mapping, layout, date_order=request.date_order
        )
        rows_input = parse_result.row_count + len(parse_result.errors)
        self._report_how_the_file_was_split(warnings, parse_result, layout)

        if layout is not None:
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.TWO_SIDED_LAYOUT,
                    f"The file was read as a two-sided chain: each source row became one "
                    f"call quote and one put quote, so {rows_input} quote row(s) came from "
                    f"{rows_input // 2} source row(s). Row numbers below are source rows.",
                    layout=str(ChainLayout.TWO_SIDED),
                    header_row=layout.header_row,
                    strike_column=layout.strike_column,
                    expiry=(layout.expiry.isoformat() if layout.expiry else None),
                )
            )
        if plan.mapping_inferred:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.MAPPING_INFERRED,
                    "No column mapping was supplied, so each field was matched to a "
                    "column by its header name. Check the mapping recorded in this "
                    "result's provenance: a column taken for the wrong field produces "
                    "a plausible chain and no error.",
                    column_mapping=plan.mapping.to_dict(),
                )
            )
        if plan.auto_detected:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.LAYOUT_AUTO_DETECTED,
                    "No layout was supplied, and the column mapping could not read the "
                    "file, so its layout was detected from the header. Check the evidence "
                    "below against the file: a misread layout produces a plausible chain "
                    "and no error. " + " ".join(detection.evidence),
                    evidence=list(detection.evidence),
                    header_row=layout.header_row,
                    strike_column=layout.strike_column,
                    call_columns=dict(layout.call_columns),
                    put_columns=dict(layout.put_columns),
                )
            )
            if layout.expiry is not None and detection.suggestion_source is not None:
                warnings.append(
                    AnalyticalWarning.warn(
                        IngestionWarningCode.EXPIRY_FROM_FILENAME,
                        f"The file names no expiry column, so every contract was dated "
                        f"{layout.expiry.isoformat()} from the {detection.suggestion_source}. "
                        f"This was not read from the data. If it is wrong, every contract "
                        f"sits at the wrong point of the term structure.",
                        expiry=layout.expiry.isoformat(),
                        source=detection.suggestion_source,
                    )
                )

        if parse_result.truncated:
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.ROWS_TRUNCATED,
                    "The file exceeded the configured row cap and was truncated.",
                    rows_parsed=parse_result.row_count,
                )
            )
        if parse_result.cell_issues:
            by_column: dict[str, dict] = {}
            for issue in parse_result.cell_issues:
                entry = by_column.setdefault(
                    issue.column,
                    {
                        "field": issue.field,
                        "count": 0,
                        "example": issue.value,
                        "first_row": issue.row_number,
                    },
                )
                entry["count"] += 1
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.OPTIONAL_VALUES_UNREADABLE,
                    f"{len(parse_result.cell_issues)} optional value(s) could not be read "
                    "and were taken as absent; their rows were kept. "
                    + " ".join(
                        f"Column {column!r} ({entry['field']}): {entry['count']} value(s), "
                        f"e.g. {entry['example']!r} at row {entry['first_row']}."
                        for column, entry in by_column.items()
                    ),
                    columns=by_column,
                )
            )
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

        # Whether the file was read at all is settled here, before anything is
        # created or written. A reading that did not work produces an empty or
        # near-empty snapshot, and an empty snapshot is indistinguishable
        # downstream from a market with nothing in it -- every later analysis
        # takes it at face value. So it is refused, with the diagnosis, rather
        # than recorded.
        verdict = assess_rejections(
            len(validated), rejected, obstacle=reading_obstacle(parse_result)
        )
        if not verdict.readable:
            raise ReadingRefused(
                verdict,
                describe(
                    OPTION_CHAIN_FIELDS,
                    _applied_mapping(request, plan),
                    list(parse_result.headers),
                    supplied=request.column_mapping,
                    layout=layout,
                    detected_layout=(detection.two_sided if detection else None),
                    expiry_source=(
                        detection.suggestion_source
                        if detection is not None
                        and layout is not None
                        and layout.expiry == detection.suggested_expiry
                        else None
                    ),
                ),
            )

        # Also settled before anything is written, and for the same reason: a
        # chain that had wholly expired at the as-of stores as a snapshot with
        # no usable quote in it.
        settlement = request.contract.expiry_time_utc
        expiries = {row.expiry for row in validated}
        if all(_is_expired(expiry, request.as_of, settlement) for expiry in expiries):
            raise ChainExpiredRefused(
                sorted(str(expiry) for expiry in expiries),
                request.as_of,
                settlement,
                len(validated),
            )

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

        provenance = self._build_provenance(request, parse_result, plan)
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
    def _report_how_the_file_was_split(
        self,
        warnings: list[AnalyticalWarning],
        parse_result: ParseResult,
        layout: TwoSidedLayout | None,
    ) -> None:
        """Say what was worked out about the file's shape and its conventions.

        Each of these is a reading the file did not state outright -- which
        character separates its cells, which line is its header, which number of
        a date is the day, which offset its timestamps are in. They are reported
        for the same reason a detected layout is: had any been read wrongly the
        chain would still look entirely plausible.
        """
        if parse_result.delimiter != ",":
            shown = "a tab" if parse_result.delimiter == "\t" else repr(parse_result.delimiter)
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.DELIMITER_DETECTED,
                    f"The file separates its cells with {shown} rather than a comma, and was "
                    "split on that.",
                    delimiter=parse_result.delimiter,
                )
            )
        if layout is None and parse_result.header_row:
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.HEADER_ROW_DETECTED,
                    f"Row {parse_result.header_row + 1} was read as the header. The "
                    f"{parse_result.header_row} line(s) above it name no fields and were not "
                    "read as data.",
                    header_row=parse_result.header_row,
                )
            )
        for reading in parse_result.date_readings:
            if reading.order is None:
                continue
            written = "day first" if reading.order is DateOrder.DAY_FIRST else "month first"
            basis = (
                "as stated in the request"
                if reading.stated
                else f"because {reading.example!r} reads no other way"
            )
            warnings.append(
                AnalyticalWarning.info(
                    IngestionWarningCode.DATE_ORDER,
                    f"Column {reading.column!r} writes its dates as numbers. Every one was "
                    f"read {written}, {basis}.",
                    **reading.to_dict(),
                )
            )
        if parse_result.naive_timestamps:
            columns = ", ".join(
                f"{column!r} ({count} value(s))"
                for column, count in parse_result.naive_timestamps.items()
            )
            warnings.append(
                AnalyticalWarning.warn(
                    IngestionWarningCode.TIMESTAMP_TIMEZONE_ASSUMED,
                    f"Timestamps in column {columns} state no UTC offset and were read as "
                    "UTC. That is an assumption, not a fact from the file: an exchange "
                    "that stamps local time is off by its offset, which moves quote "
                    "staleness by the same amount.",
                    columns=dict(parse_result.naive_timestamps),
                )
            )

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
        self._warn_about_expired_contracts(warnings, request, persistable)
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

    def _warn_about_expired_contracts(
        self,
        warnings: list[AnalyticalWarning],
        request: OptionChainIngestionRequest,
        persistable: list[PersistableOptionQuote],
    ) -> None:
        """Say at ingest when the as-of timestamp is past the contracts' expiry.

        An expired contract stores perfectly well and then supports nothing: the
        implied-volatility solver refuses it (`OPTION_EXPIRED`), so the surface
        has no slice to fit and the scanner has no surface. Left unsaid, the
        user meets that three screens later as an empty chart.

        The usual cause is not the file. It is an as-of timestamp that was typed
        or defaulted rather than observed, so the warning names both the dates
        and the timestamp they were compared against.

        Only ever a *part* of the chain by the time this runs: a chain that had
        wholly expired was refused before anything was written
        (:class:`ChainExpiredRefused`).
        """
        settlement = request.contract.expiry_time_utc
        expiries = {item.expiry for item in persistable}
        gone = sorted(
            str(expiry) for expiry in expiries if _is_expired(expiry, request.as_of, settlement)
        )
        if not gone:
            return

        quotes = sum(1 for item in persistable if str(item.expiry) in set(gone))
        message = (
            f"{quotes} quote(s) at expiry {', '.join(gone)} had already expired at the "
            f"as-of timestamp {request.as_of.isoformat()}. They are stored as observed, "
            "but no implied volatility, surface or deviation scan can be solved from "
            "them, because time to expiry is not positive. Check the as-of timestamp: "
            "it is supplied by the caller, not read from the file."
        )
        warnings.append(
            AnalyticalWarning.warn(
                IngestionWarningCode.CONTRACTS_ALREADY_EXPIRED, message, expiries=gone
            )
        )

    def _build_provenance(
        self,
        request: OptionChainIngestionRequest,
        parse_result: ParseResult,
        plan: ReadingPlan | None = None,
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
                # The reading that actually happened. A detected layout or an
                # inferred mapping supersedes what the request asked for, so
                # recording the request here would describe a file that was
                # never read that way.
                "column_mapping": _applied_mapping(request, plan).to_dict(),
                "layout": (
                    plan.layout.to_dict()
                    if plan is not None and plan.layout is not None
                    else str(ChainLayout.LONG)
                ),
                "headers": parse_result.headers,
                "delimiter": parse_result.delimiter,
                "header_row": parse_result.header_row,
                "date_readings": [item.to_dict() for item in parse_result.date_readings],
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
