"""Registering, validating, writing and querying historical datasets.

The path an acceptance criterion describes — *historical dataset → validated →
queryable → usable by the research engine* — runs through one method here,
:meth:`WarehouseService.ingest`, and the four verbs are four visible stages
rather than one opaque one.

What the service will not do is serve a dataset it has quarantined. A validation
error means the data cannot be used as it stands, and the alternative — serving
it with a warning attached and hoping the warning is read — is how a split-
adjusted return ends up in a backtest.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.service import InstrumentService
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from domains.warehouse.enums import (
    CorporateActionTreatment,
    DatasetKind,
    DatasetLayer,
    DatasetStatus,
    ValidationSeverity,
)
from domains.warehouse.orm import WarehouseDatasetORM
from domains.warehouse.quality import DatasetQuality, score_dataset
from domains.warehouse.query import QueryRequest, QueryResult, WarehouseQuery
from domains.warehouse.repository import WarehouseRepository
from domains.warehouse.storage import WarehouseStore, partition_rows
from domains.warehouse.validation import ValidationReport, validate_bars
from infrastructure.settings import Settings
from infrastructure.storage.base import ObjectStore


class WarehouseWarningCode:
    VALIDATION_FOUND_ERRORS = "WAREHOUSE_VALIDATION_FOUND_ERRORS"
    NOTHING_WRITTEN = "WAREHOUSE_NOTHING_WRITTEN"
    INSTRUMENTS_UNKNOWN = "WAREHOUSE_INSTRUMENTS_UNKNOWN"
    CORPORATE_ACTION_TREATMENT_UNKNOWN = "WAREHOUSE_CORPORATE_ACTION_TREATMENT_UNKNOWN"


class DatasetQuarantined(Exception):
    """A query asked for a dataset that validation refused to serve."""

    def __init__(self, dataset_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"dataset {dataset_id} is quarantined: {reason}")
        self.dataset_id = dataset_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    """What one ingestion produced."""

    dataset_id: uuid.UUID
    status: DatasetStatus
    rows_in: int
    rows_written: int
    rows_excluded: int
    rows_rejected: int
    rows_flagged: int
    partitions: int
    instruments: int
    bytes_written: int
    first_observation: datetime | None
    last_observation: datetime | None
    quality: DatasetQuality
    validation: ValidationReport

    @property
    def conserved(self) -> bool:
        return self.rows_in == self.rows_written + self.rows_excluded + self.rows_rejected

    def to_dict(self) -> dict:
        return {
            "dataset_id": str(self.dataset_id),
            "status": str(self.status),
            "rows_in": self.rows_in,
            "rows_written": self.rows_written,
            "rows_excluded": self.rows_excluded,
            "rows_rejected": self.rows_rejected,
            "rows_flagged": self.rows_flagged,
            "conserved": self.conserved,
            "partitions": self.partitions,
            "instruments": self.instruments,
            "bytes_written": self.bytes_written,
            "first_observation": (
                self.first_observation.isoformat() if self.first_observation else None
            ),
            "last_observation": (
                self.last_observation.isoformat() if self.last_observation else None
            ),
            "quality": self.quality.to_dict(),
            "validation": self.validation.to_dict(),
        }


def content_digest(rows: Sequence[Mapping]) -> str:
    """A digest over the supplied rows, so provenance names the exact bytes.

    Computed from the normalised repr of each row rather than from the file,
    because the same rows may arrive as CSV one day and Parquet the next and
    they are the same dataset.
    """
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(json.dumps({key: str(value) for key, value in sorted(row.items())}).encode())
    return hasher.hexdigest()[:64]


class WarehouseService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        object_store: ObjectStore,
    ) -> None:
        self._session = session
        self._settings = settings
        self._store = object_store
        self.repository = WarehouseRepository(session)
        self.instruments = InstrumentService(session)
        self.storage = WarehouseStore(object_store, settings.code_commit)
        self.query = WarehouseQuery(object_store, settings.warehouse_max_query_partitions)

    # --------------------------------------------------------------- ingest
    async def ingest(
        self,
        user_id: uuid.UUID,
        name: str,
        kind: DatasetKind,
        exchange: str,
        rows: Sequence[Mapping],
        layer: DatasetLayer = DatasetLayer.NORMALIZED,
        source: str | None = None,
        treatment: CorporateActionTreatment = CorporateActionTreatment.UNKNOWN,
        continuous: bool = False,
    ) -> AnalyticalResult[IngestionSummary]:
        """Validate a batch, write its partitions, and register what happened."""
        if kind is not DatasetKind.BARS:
            raise NotImplementedError(
                f"{kind} ingestion is not implemented; the warehouse currently validates "
                "and writes bars, and a kind it cannot validate must not be written as "
                "though it had been"
            )

        materialised = list(rows)
        digest = content_digest(materialised)
        report = validate_bars(materialised, treatment)
        warnings: list[AnalyticalWarning] = []

        known, unknown = await self._split_by_known_instrument(report.rows)
        if unknown:
            warnings.append(
                AnalyticalWarning.warn(
                    WarehouseWarningCode.INSTRUMENTS_UNKNOWN,
                    f"{len(unknown)} row(s) name an instrument the platform does not hold; "
                    "they were not written, because a partition keyed on an unknown "
                    "instrument cannot be joined to anything",
                    instruments=[str(item) for item in sorted(unknown, key=str)][:20],
                )
            )

        buckets = partition_rows(known, layer, kind, exchange)
        written: list[dict] = []
        total_bytes = 0
        for key, rows_in_partition in sorted(buckets.items(), key=lambda item: item[0].day):
            object_key, count, size = await self.storage.write_partition(
                key, rows_in_partition, source=source or "unspecified"
            )
            total_bytes += size
            timestamps = [row.exchange_timestamp for row in rows_in_partition]
            written.append(
                {
                    "instrument_id": key.instrument_id,
                    "exchange": key.exchange,
                    "day": key.day,
                    "object_key": object_key,
                    "rows": count,
                    "rows_flagged": sum(1 for row in rows_in_partition if row.flags),
                    "bytes_written": size,
                    "first_observation": min(timestamps) if timestamps else None,
                    "last_observation": max(timestamps) if timestamps else None,
                }
            )

        first = min((item["first_observation"] for item in written), default=None)
        last = max((item["last_observation"] for item in written), default=None)

        quality = score_dataset(
            report,
            source=source,
            treatment=treatment,
            dataset_digest=digest,
            last_observation=last,
            continuous=continuous,
        )

        blocking = [
            item
            for item in (*report.findings, *report.rejected)
            if item.severity is ValidationSeverity.ERROR
        ]
        status = DatasetStatus.QUARANTINED if blocking else DatasetStatus.AVAILABLE
        if not written:
            status = DatasetStatus.REGISTERED
            warnings.append(
                AnalyticalWarning.warn(
                    WarehouseWarningCode.NOTHING_WRITTEN,
                    "no partition was written, so this dataset holds nothing to query",
                )
            )
        if blocking:
            warnings.append(
                AnalyticalWarning.error(
                    WarehouseWarningCode.VALIDATION_FOUND_ERRORS,
                    f"{len(blocking)} validation error(s) mean this dataset cannot be served "
                    "as it stands. The partitions are written and the findings are stored; "
                    "nothing was repaired.",
                    codes=sorted({str(item.code) for item in blocking}),
                )
            )
        if treatment is CorporateActionTreatment.UNKNOWN:
            warnings.append(
                AnalyticalWarning.warn(
                    WarehouseWarningCode.CORPORATE_ACTION_TREATMENT_UNKNOWN,
                    "this dataset does not say whether its prices are adjusted for "
                    "corporate actions, so it cannot safely be joined to one that does",
                )
            )

        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_data_sources=(source,) if source else (),
            dataset_versions={source or "unspecified": digest},
            parameters={
                "layer": str(layer),
                "kind": str(kind),
                "exchange": exchange,
                "corporate_action_treatment": str(treatment),
                "continuous": continuous,
            },
        )

        # The complete finding list is unbounded, so the counts and a capped
        # sample go in the row and everything goes to the object store — the
        # same split the microstructure importer uses for its rejections.
        row = await self.repository.create_dataset(
            user_id=user_id,
            name=name,
            layer=str(layer),
            kind=str(kind),
            exchange=exchange,
            status=str(status),
            source=source,
            dataset_digest=digest,
            corporate_action_treatment=str(treatment),
            continuous=continuous,
            rows_in=report.rows_in,
            rows_written=report.rows_written,
            rows_excluded=len(report.excluded),
            rows_rejected=len(report.rejected),
            rows_flagged=sum(1 for item in report.rows if item.flags),
            instrument_count=len({item["instrument_id"] for item in written}),
            partition_count=len(written),
            bytes_written=total_bytes,
            first_observation=first,
            last_observation=last,
            completeness_score=quality.completeness_score,
            consistency_score=quality.consistency_score,
            outlier_score=quality.outlier_score,
            source_score=quality.source_score,
            freshness_score=quality.freshness_score,
            overall_score=quality.overall_score,
            quality_evidence=quality.evidence,
            validation_summary=report.to_dict(max_findings=25),
            provenance=provenance.to_dict(),
        )
        findings_key = await self._put_findings(user_id, row.id, report)
        row.findings_key = findings_key
        await self.repository.replace_partitions(row.id, written)

        summary = IngestionSummary(
            dataset_id=row.id,
            status=status,
            rows_in=report.rows_in,
            rows_written=report.rows_written,
            rows_excluded=len(report.excluded),
            rows_rejected=len(report.rejected),
            rows_flagged=row.rows_flagged,
            partitions=len(written),
            instruments=row.instrument_count,
            bytes_written=total_bytes,
            first_observation=first,
            last_observation=last,
            quality=quality,
            validation=report,
        )
        return AnalyticalResult.ok(summary, provenance, tuple(warnings))

    async def _split_by_known_instrument(self, rows) -> tuple[list, set[uuid.UUID]]:
        """Keep only rows whose instrument the platform can identify.

        A partition keyed on an instrument nobody has heard of cannot be joined
        to a price, a position or a surface, so writing it would create a file
        that is guaranteed to be useless and hard to notice.
        """
        known: list = []
        unknown: set[uuid.UUID] = set()
        cache: dict[uuid.UUID, bool] = {}
        for row in rows:
            if row.instrument_id not in cache:
                cache[row.instrument_id] = (
                    await self.instruments.get(row.instrument_id)
                ) is not None
            if cache[row.instrument_id]:
                known.append(row)
            else:
                unknown.add(row.instrument_id)
        return known, unknown

    async def _put_findings(
        self, user_id: uuid.UUID, dataset_id: uuid.UUID, report: ValidationReport
    ) -> str:
        key = f"warehouse/findings/{user_id}/{dataset_id}.json"
        payload = {
            "findings": [item.to_dict() for item in report.findings],
            "excluded": [item.to_dict() for item in report.excluded],
            "rejected": [item.to_dict() for item in report.rejected],
        }
        await self._store.put(
            key,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )
        return key

    # ---------------------------------------------------------------- reads
    async def get_dataset(
        self, dataset_id: uuid.UUID, user_id: uuid.UUID
    ) -> WarehouseDatasetORM | None:
        return await self.repository.get_dataset(dataset_id, user_id)

    async def list_datasets(self, user_id: uuid.UUID, **filters) -> list[WarehouseDatasetORM]:
        return await self.repository.list_datasets(user_id, **filters)

    async def list_partitions(self, dataset_id: uuid.UUID, limit: int = 2000):
        return await self.repository.list_partitions(dataset_id, limit)

    async def findings(self, dataset: WarehouseDatasetORM) -> dict:
        if not dataset.findings_key:
            return {"findings": [], "excluded": [], "rejected": []}
        return json.loads((await self._store.get(dataset.findings_key)).decode("utf-8"))

    async def run_query(
        self, user_id: uuid.UUID, request: QueryRequest, dataset_id: uuid.UUID | None = None
    ) -> QueryResult:
        """Query the warehouse, refusing a dataset validation would not serve."""
        if dataset_id is not None:
            dataset = await self.repository.get_dataset(dataset_id, user_id)
            if dataset is None:
                raise LookupError(f"dataset {dataset_id} not found")
            if dataset.status == str(DatasetStatus.QUARANTINED):
                raise DatasetQuarantined(
                    dataset_id,
                    "validation found errors in it; serving it with a warning attached "
                    "and hoping the warning is read is how a bad series reaches a backtest",
                )
        return await self.query.run(request)


def rows_from_mappings(payload: Iterable[Mapping]) -> list[dict]:
    """Shallow copies, so an ingestion cannot mutate its caller's rows."""
    return [dict(row) for row in payload]
