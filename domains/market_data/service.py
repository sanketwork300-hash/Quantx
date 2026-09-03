"""Market-data application service: uploads, ingestion, retrieval."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePosixPath

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.service import InstrumentService
from domains.market_data.enums import UploadKind, UploadStatus
from domains.market_data.ingestion.column_mapping import (
    OPTION_CHAIN_FIELDS,
    ColumnMapping,
    infer_mapping,
)
from domains.market_data.ingestion.parser import TabularParser
from domains.market_data.ingestion.pipeline import (
    IngestionSummary,
    OptionChainIngestionPipeline,
    OptionChainIngestionRequest,
)
from domains.market_data.orm import OptionChainSnapshotORM, OptionQuoteORM, UploadORM
from domains.market_data.quality.config import MarketDataQualityConfig
from domains.market_data.quality.flags import MarketDataQuality, QualityFlag
from domains.market_data.repository import MarketDataRepository
from domains.reports.envelope import AnalyticalResult
from infrastructure.settings import Settings
from infrastructure.storage.base import ObjectStore

#: Parquet's magic number, which both begins and ends a valid file. Parquet is
#: the one binary format the platform accepts, because it is what an L2 capture
#: pipeline writes and what the platform itself stores; it is a columnar data
#: file with no macro or formula facility, which is what the binary rejection
#: below exists to keep out.
PARQUET_MAGIC = b"PAR1"

#: Byte sequences that mark a file as something we will not parse as a table.
#: Checked against the actual content, not the filename, because the filename is
#: attacker-controlled and the content is what gets parsed.
BINARY_SIGNATURES: tuple[bytes, ...] = (
    b"PK\x03\x04",  # zip / xlsx / ods
    b"\xd0\xcf\x11\xe0",  # legacy OLE (xls, doc)
    b"%PDF",
    b"\x7fELF",
    b"MZ",
    b"\x89PNG",
    b"\xff\xd8\xff",  # jpeg
    b"\x1f\x8b",  # gzip
)


class UploadRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class UploadPreview:
    upload_id: uuid.UUID
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    missing_required: tuple[str, ...]
    unmapped_columns: tuple[str, ...]
    sample_rows: list[dict]
    parse_errors: list[dict]

    def to_dict(self) -> dict:
        return {
            "upload_id": str(self.upload_id),
            "headers": self.headers,
            "inferred_mapping": self.inferred_mapping,
            "applied_mapping": self.applied_mapping,
            "missing_required": list(self.missing_required),
            "unmapped_columns": list(self.unmapped_columns),
            "sample_rows": self.sample_rows,
            "parse_errors": self.parse_errors,
        }


def sanitize_filename(filename: str) -> str:
    """Keep a display name only. It never influences a storage path."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ").strip()
    return (cleaned or "upload")[:255]


class MarketDataService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        object_store: ObjectStore,
        quality_config: MarketDataQualityConfig | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._store = object_store
        self.repository = MarketDataRepository(session)
        self.instruments = InstrumentService(session)
        self._quality_config = quality_config or MarketDataQualityConfig()

    def _pipeline(self) -> OptionChainIngestionPipeline:
        return OptionChainIngestionPipeline(
            instrument_service=self.instruments,
            repository=self.repository,
            quality_config=self._quality_config,
            max_rows=self._settings.max_upload_rows,
            code_commit=self._settings.code_commit,
        )

    # -------------------------------------------------------------- uploads
    def validate_upload(self, filename: str, content_type: str, data: bytes) -> None:
        settings = self._settings
        if len(data) == 0:
            raise UploadRejected("UPLOAD_EMPTY", "The uploaded file is empty.")
        if len(data) > settings.max_upload_bytes:
            raise UploadRejected(
                "UPLOAD_TOO_LARGE",
                f"File is {len(data)} bytes; the limit is {settings.max_upload_bytes}.",
            )

        suffix = PurePosixPath(sanitize_filename(filename)).suffix.lower()
        if suffix not in settings.allowed_upload_extensions:
            raise UploadRejected(
                "UPLOAD_EXTENSION_NOT_ALLOWED",
                f"Extension {suffix!r} is not accepted. Allowed: "
                f"{', '.join(settings.allowed_upload_extensions)}.",
            )

        if data[:4] == PARQUET_MAGIC:
            # A parquet file is binary and is not UTF-8 text, so it has to be
            # admitted before the two checks below rather than excused after
            # them. Both ends are checked: a file that merely starts with the
            # magic number is not a parquet file, and admitting it would let
            # arbitrary bytes past the binary rejection behind four characters
            # anyone can type.
            if len(data) < 8 or data[-4:] != PARQUET_MAGIC:
                raise UploadRejected(
                    "UPLOAD_PARQUET_TRUNCATED",
                    "The file begins with the parquet magic number but does not "
                    "end with it, so it is truncated or is not parquet. It is "
                    "not parsed.",
                )
            if suffix != ".parquet":
                raise UploadRejected(
                    "UPLOAD_EXTENSION_DISAGREES_WITH_CONTENT",
                    f"The content is parquet but the file is named {suffix!r}. "
                    "The extension and the content have to agree, so that what "
                    "is stored is what the name says.",
                )
            return

        head = data[:8]
        for signature in BINARY_SIGNATURES:
            if head.startswith(signature):
                raise UploadRejected(
                    "UPLOAD_BINARY_CONTENT",
                    "The file content is binary. Export the sheet to CSV and upload "
                    "that; spreadsheet files are not parsed, so no formula can be "
                    "evaluated.",
                )
        try:
            data[:65536].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadRejected("UPLOAD_NOT_UTF8", "The file is not valid UTF-8 text.") from exc

    async def create_upload(
        self,
        user_id: uuid.UUID,
        kind: UploadKind,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> UploadORM:
        self.validate_upload(filename, content_type, data)

        digest = hashlib.sha256(data).hexdigest()
        upload_id = uuid.uuid4()
        # Server-generated key. The client filename is display metadata and
        # never touches the storage path.
        stored_key = (
            f"uploads/{user_id}/{datetime.now(UTC):%Y/%m/%d}/{upload_id}"
            f"{PurePosixPath(sanitize_filename(filename)).suffix.lower()}"
        )
        await self._store.put(stored_key, data, content_type=content_type)

        return await self.repository.create_upload(
            id=upload_id,
            user_id=user_id,
            kind=str(kind),
            original_filename=sanitize_filename(filename),
            stored_key=stored_key,
            content_type=content_type,
            byte_size=len(data),
            sha256=digest,
            status=str(UploadStatus.RECEIVED),
        )

    async def get_upload(self, upload_id: uuid.UUID, user_id: uuid.UUID) -> UploadORM | None:
        return await self.repository.get_upload(upload_id, user_id=user_id)

    async def read_upload(self, upload: UploadORM) -> bytes:
        return await self._store.get(upload.stored_key)

    async def preview_upload(
        self,
        upload: UploadORM,
        mapping: ColumnMapping | None = None,
        limit: int | None = None,
    ) -> UploadPreview:
        data = await self.read_upload(upload)
        headers = TabularParser.read_headers(data)

        inferred = infer_mapping(headers, OPTION_CHAIN_FIELDS)
        applied = mapping or inferred

        pipeline = self._pipeline()
        parse_result, missing = pipeline.preview(
            data, applied, limit or self._settings.upload_preview_rows
        )

        return UploadPreview(
            upload_id=upload.id,
            headers=parse_result.headers or headers,
            inferred_mapping=inferred.to_dict(),
            applied_mapping=applied.to_dict(),
            missing_required=missing,
            unmapped_columns=applied.unmapped_columns(parse_result.headers),
            sample_rows=[
                {key: _jsonable(value) for key, value in row.values.items()}
                for row in parse_result.rows
            ],
            parse_errors=[error.to_dict() for error in parse_result.errors],
        )

    # -------------------------------------------------------------- ingestion
    async def ingest_option_chain(
        self, upload: UploadORM, request: OptionChainIngestionRequest
    ) -> AnalyticalResult[IngestionSummary]:
        data = await self.read_upload(upload)
        result = await self._pipeline().ingest(data, request)
        upload.status = str(UploadStatus.INGESTED)
        await self._session.flush()
        return result

    # ---------------------------------------------------------- chain reads
    # Exposed as service methods because other domains may not touch this
    # domain's repository (docs/architecture.md section 3, enforced by
    # scripts/check_layering.py).
    async def get_chain_snapshot(
        self, snapshot_id: uuid.UUID, user_id: uuid.UUID
    ) -> OptionChainSnapshotORM | None:
        return await self.repository.get_chain_snapshot(snapshot_id, user_id=user_id)

    async def latest_chain_snapshot(
        self, user_id: uuid.UUID, underlying_id: uuid.UUID
    ) -> OptionChainSnapshotORM | None:
        return await self.repository.latest_chain_snapshot(user_id, underlying_id)

    async def get_chain_quotes(
        self, snapshot_id: uuid.UUID, include_excluded: bool = False
    ) -> list[OptionQuoteORM]:
        """Quotes for a snapshot. Excluded ones are omitted by default: a quote
        the quality engine set aside should not silently enter a calibration."""
        return await self.repository.get_option_quotes(
            snapshot_id, include_excluded=include_excluded
        )

    async def build_market_state(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        as_of: datetime | None = None,
        risk_free_rate: float | None = None,
    ):
        """Assemble the market-data half of a snapshot for one underlying.

        Surfaces are added by the caller: they belong to the derivatives domain,
        and having market data reach for one would invert the dependency between
        the two. ``domains.reports`` composes the two halves.
        """
        from domains.market_data.curves import YieldCurve
        from domains.market_data.market_state import MarketStateBuilder
        from domains.market_data.models import Quote

        snapshot = await self.repository.latest_chain_snapshot(user_id, underlying_id)
        if snapshot is None:
            return None

        moment = as_of or snapshot.as_of_timestamp
        builder = MarketStateBuilder(moment)
        builder.add_source(snapshot.source, snapshot.dataset_digest)

        if snapshot.underlying_price is not None:
            builder.add_spot(underlying_id, snapshot.underlying_price)

        for row in await self.repository.get_option_quotes(snapshot.id, include_excluded=False):
            builder.add_quote(
                Quote(
                    instrument_id=row.instrument_id,
                    exchange_timestamp=row.exchange_timestamp,
                    receive_timestamp=row.receive_timestamp,
                    source=snapshot.source,
                    bid_price=row.bid_price,
                    ask_price=row.ask_price,
                    bid_size=row.bid_size,
                    ask_size=row.ask_size,
                    last_price=row.last_price,
                    volume=row.volume,
                    open_interest=row.open_interest,
                    sequence_number=row.sequence_number,
                ),
                # The quality measured at ingestion travels with the snapshot,
                # so a consumer that needs to know how good a quote is reads the
                # measurement that was made rather than making a second one.
                quality=_quality_of(row),
            )

        if risk_free_rate is not None:
            builder.add_curve(YieldCurve.flat(risk_free_rate, moment, "INR", source="assumption"))
        return builder

    async def underlying_price_history(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        limit: int = 500,
    ) -> list[tuple[datetime, Decimal]]:
        """The underlying's observed level, one point per ingested chain.

        This is the platform's price history: it is exactly as long as the user's
        own ingestion record, which is a real constraint and is reported as an
        observation count wherever it is used rather than padded out.
        """
        rows = await self.repository.list_chain_snapshots(
            user_id, underlying_id=underlying_id, limit=limit
        )
        return [
            (row.as_of_timestamp, row.underlying_price)
            for row in reversed(rows)
            if row.underlying_price is not None
        ]

    async def instrument_quote_history(
        self,
        user_id: uuid.UUID,
        instrument_id: uuid.UUID,
        start: datetime,
        end: datetime,
        limit: int = 5_000,
    ) -> list[tuple[datetime, Decimal, Decimal | None]]:
        """Observed mids for one contract over a window, with their spreads.

        Built from the option quotes stored with each ingested chain, which is
        the platform's only intraday record of a contract. It carries a
        two-sided market, so a spread travels with each observation and a spread
        charge is attributable; it does *not* carry interval volume, and none is
        invented here.
        """
        rows = await self.repository.option_quote_history(
            user_id, instrument_id, start, end, limit=limit
        )
        history: list[tuple[datetime, Decimal, Decimal | None]] = []
        for row in rows:
            if row.bid_price is None or row.ask_price is None:
                continue
            if row.bid_price <= 0 or row.ask_price <= 0 or row.ask_price < row.bid_price:
                continue
            history.append(
                (
                    row.exchange_timestamp,
                    (row.bid_price + row.ask_price) / 2,
                    row.ask_price - row.bid_price,
                )
            )
        return history

    async def underlying_level_history(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        start: datetime,
        end: datetime,
        limit: int = 5_000,
    ) -> list[tuple[datetime, Decimal, Decimal | None]]:
        """The underlying's recorded level over a window.

        One observation per ingested chain, and no spread: a snapshot's
        underlying price is a single level, so nothing two-sided can be read off
        it and no spread charge is attributable from it.
        """
        rows = await self.repository.list_chain_snapshots(
            user_id, underlying_id=underlying_id, limit=limit
        )
        return [
            (row.as_of_timestamp, row.underlying_price, None)
            for row in reversed(rows)
            if row.underlying_price is not None and start <= row.as_of_timestamp <= end
        ]

    async def ingest_option_chain_bytes(
        self, data: bytes, request: OptionChainIngestionRequest
    ) -> AnalyticalResult[IngestionSummary]:
        """Ingest without an upload row (research mode, provider imports, tests)."""
        return await self._pipeline().ingest(data, request)


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _quality_of(row) -> MarketDataQuality:
    """Rehydrate the quality measured when the quote was ingested."""
    return MarketDataQuality(
        stale_score=row.stale_score,
        spread_score=row.spread_score,
        liquidity_score=row.liquidity_score,
        consistency_score=row.consistency_score,
        completeness_score=row.completeness_score,
        overall_score=row.overall_score,
        flags=tuple(QualityFlag.from_dict(item) for item in (row.quality_flags or ())),
    )
