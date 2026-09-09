"""Historical warehouse: dataset registry, quality, and queries."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from api.schemas.common import APIModel
from domains.warehouse.enums import (
    CorporateActionTreatment,
    DatasetKind,
    DatasetLayer,
    DatasetStatus,
)


class IngestDatasetRequest(APIModel):
    """Register a historical file as a warehouse dataset."""

    upload_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    exchange: str = Field(min_length=1, max_length=32)
    kind: DatasetKind = DatasetKind.BARS
    layer: DatasetLayer = DatasetLayer.NORMALIZED
    #: One instrument for the whole file. Omit it and the file must carry a
    #: symbol column, resolved row by row against the instrument master.
    instrument_id: uuid.UUID | None = None
    interval: str = Field(default="1d", max_length=8)
    source: str | None = Field(default=None, max_length=128)
    #: Declared, never inferred. The platform holds no corporate-action feed and
    #: will not adjust a series; leaving this UNKNOWN is recorded as a warning
    #: because an unflagged 1:5 split reads as an -80% return.
    corporate_action_treatment: CorporateActionTreatment = CorporateActionTreatment.UNKNOWN
    #: Whether the dataset is meant to stay current. Freshness is scored only
    #: for these — a historical archive is not stale, it is history.
    continuous: bool = False
    column_mapping: dict[str, str] = Field(default_factory=dict)


class DatasetQualityOut(APIModel):
    completeness_score: float | None
    consistency_score: float | None
    outlier_score: float | None
    #: How completely the dataset describes its own provenance — not a ranking
    #: of vendors, which the platform has no basis for.
    source_score: float | None
    #: Null for a dataset that is not continuous: not measurable, which is a
    #: different statement from zero.
    freshness_score: float | None
    overall_score: float | None


class DatasetSummaryOut(APIModel):
    id: uuid.UUID
    name: str
    layer: DatasetLayer
    kind: DatasetKind
    exchange: str
    status: DatasetStatus
    source: str | None
    corporate_action_treatment: CorporateActionTreatment
    continuous: bool
    rows_in: int
    rows_written: int
    rows_excluded: int
    rows_rejected: int
    rows_flagged: int
    instrument_count: int
    partition_count: int
    bytes_written: int
    first_observation: datetime | None
    last_observation: datetime | None
    quality: DatasetQualityOut
    created_at: datetime


class DatasetDetailOut(DatasetSummaryOut):
    dataset_digest: str | None
    quality_evidence: dict
    #: Counts by code plus a capped sample. The complete list is in the object
    #: store and is served by the findings endpoint.
    validation_summary: dict
    provenance: dict


class PartitionOut(APIModel):
    instrument_id: uuid.UUID
    exchange: str
    day: date
    object_key: str
    rows: int
    rows_flagged: int
    bytes_written: int
    first_observation: datetime | None
    last_observation: datetime | None


class PartitionListOut(APIModel):
    items: list[PartitionOut]
    #: Sums across the listed partitions, so a caller need not add them up to
    #: check them against the dataset row.
    total_rows: int
    total_bytes: int


class FindingOut(APIModel):
    code: str
    severity: str
    message: str
    row_number: int | None
    instrument_id: uuid.UUID | None
    #: The numbers the finding was made on, so it can be argued with.
    evidence: dict


class FindingsOut(APIModel):
    """Everything the validator said. Nothing here was repaired."""

    findings: list[FindingOut]
    excluded: list[FindingOut]
    rejected: list[FindingOut]


class QueryRowsOut(APIModel):
    """Rows from the warehouse, with how they were read."""

    columns: list[str]
    rows: list[dict]
    row_count: int
    #: DIRECT means the reader pruned partitions and pushed predicates down;
    #: MATERIALISED means partitions were fetched and filtered in memory.
    read_path: str
    #: Partitions that actually contributed rows, not the number a glob matched.
    partitions_read: int
    #: True when a limit cut the answer short, so a truncated result is never
    #: mistaken for a complete one.
    truncated: bool
    warnings: list[str] = Field(default_factory=list)
