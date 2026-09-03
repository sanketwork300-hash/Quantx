"""Microstructure request and response schemas.

Two shapes here are unusual on purpose.

``QueueOutlookOut`` has no ``fill_probability`` field, only a range. The two
ends of the bracket differ in an assumption public data cannot settle, and a
single field would be filled in eventually and read as a measurement.

``IntensityOut`` carries both models whatever the verdict, with
``hawkes_is_adopted`` beside them. A client that renders the Hawkes parameters
without reading that flag is rendering a rejected candidate, so the flag and the
reason are required fields rather than optional detail.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from api.schemas.common import APIModel, DecimalStr
from domains.market_data.enums import BookEventType, BookSide

MAX_PREVIEW_ROWS = 2_000


# ------------------------------------------------------------------- import
class LevelColumnsIn(APIModel):
    """A confirmed wide-CSV level mapping, as returned by the preview.

    Sent back verbatim on commit so that what is imported is what was reviewed.
    A book whose price and size columns were read the wrong way round produces
    analytics that are wrong in every number and look entirely ordinary, which
    is why this is confirmed rather than re-inferred.
    """

    timestamp: str | None = None
    receive_timestamp: str | None = None
    sequence: str | None = None
    #: ``{"bid": {"1": {"price": "BID_PX_1", "size": "BID_SZ_1"}}, "ask": {...}}``
    levels: dict[str, dict[str, dict[str, str]]] = {}
    unrecognised_columns: list[str] = []

    def to_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "receive_timestamp": self.receive_timestamp,
            "sequence": self.sequence,
            "levels": self.levels,
            "unrecognised_columns": self.unrecognised_columns,
        }


class DatasetPreviewRequest(APIModel):
    instrument_id: uuid.UUID
    snapshot_upload_id: uuid.UUID | None = None
    event_upload_id: uuid.UUID | None = None
    snapshot_columns: LevelColumnsIn | None = None
    event_mapping: dict[str, str] | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_PREVIEW_ROWS)

    @model_validator(mode="after")
    def at_least_one_upload(self) -> DatasetPreviewRequest:
        if self.snapshot_upload_id is None and self.event_upload_id is None:
            raise ValueError(
                "supply a snapshot upload, an event upload, or both; there is "
                "nothing to preview otherwise"
            )
        return self


class DatasetImportRequest(DatasetPreviewRequest):
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(default="user-upload", max_length=128)


class DatasetPreviewOut(APIModel):
    committable: bool
    snapshots: dict[str, Any]
    events: dict[str, Any]
    detected_snapshot_columns: dict[str, Any]
    detected_event_mapping: dict[str, str]
    availability: dict[str, Any]


class CapabilityOut(APIModel):
    capability: str
    available: bool
    reason: str | None = None
    message: str
    evidence: dict[str, Any] = {}


class DatasetOut(APIModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    name: str
    kind: str
    source: str
    snapshot_rows_in: int
    snapshot_rows_kept: int
    snapshot_rows_rejected: int
    event_rows_in: int
    event_rows_kept: int
    event_rows_rejected: int
    rejection_counts: dict[str, int] = {}
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    span_seconds: float
    max_depth_levels: int
    available_capabilities: list[str] = []
    created_at: datetime


class RejectionsOut(APIModel):
    """Every row that did not make it, by source row number and reason."""

    dataset_id: uuid.UUID
    snapshot_rejections: list[dict[str, Any]]
    event_rejections: list[dict[str, Any]]
    counts: dict[str, int] = {}


# ---------------------------------------------------------------- analytics
class AnalyseBookRequest(APIModel):
    #: Levels per side to measure over. More than the book carries is not an
    #: error; the measurement simply uses what is there and says how many.
    levels: int = Field(default=5, ge=1, le=50)
    #: ``w_i = exp(-decay * i)``. Zero reduces the weighted imbalance exactly to
    #: the plain one, which is why it is a parameter rather than a constant.
    weighted_decay: float = Field(default=0.5, ge=0.0, le=10.0)
    #: Sizes to walk the displayed book for. Empty by default: a cost to trade
    #: only means something at a size someone cares about.
    trade_sizes: list[float] = []
    preview_points: int = Field(default=500, ge=10, le=5_000)

    @model_validator(mode="after")
    def sizes_are_positive(self) -> AnalyseBookRequest:
        if any(size <= 0 for size in self.trade_sizes):
            raise ValueError("a non-positive trade size has no cost to trade")
        return self


class BookReportOut(APIModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    instrument_id: uuid.UUID
    levels: int
    weighted_decay: float
    snapshots_analysed: int
    window_start: datetime | None = None
    window_end: datetime | None = None
    crossed_snapshots: int
    locked_snapshots: int
    median_spread: float | None = None
    median_relative_spread: float | None = None
    median_imbalance: float | None = None
    median_microprice_tilt: float | None = None
    created_at: datetime


# ---------------------------------------------------------------- intensity
class FitIntensityRequest(APIModel):
    #: Empty means every event on the tape, which is a superposition of several
    #: processes and is labelled as such rather than called "order flow".
    event_types: list[BookEventType] = []
    side: BookSide | None = None
    price: DecimalStr | None = None
    train_fraction: float = Field(default=0.7, gt=0.0, lt=1.0)
    #: One-sided critical value for the held-out predictive test. Raising it
    #: makes the gate stricter; there is no value that disables it.
    critical_value: float = Field(default=1.645, ge=0.0, le=10.0)

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_types": [str(item) for item in self.event_types],
            "side": str(self.side) if self.side else None,
            "price": format(self.price, "f") if self.price is not None else None,
            "train_fraction": self.train_fraction,
            "critical_value": self.critical_value,
        }


class IntensityOut(APIModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    instrument_id: uuid.UUID
    scope: str
    events_selected: int
    window_start: datetime
    window_end: datetime
    split_timestamp: datetime
    poisson_rate: float
    hawkes_branching_ratio: float | None = None
    held_out_events: int
    mean_gain_per_event: float | None = None
    test_statistic: float | None = None
    critical_value: float
    #: Read this before rendering any Hawkes parameter. When it is false those
    #: parameters describe a candidate the held-out test rejected.
    hawkes_is_adopted: bool
    adopted_model: str
    adopted_rate: float
    verdict_reason: str
    created_at: datetime


# -------------------------------------------------------------------- queue
class QueueEstimateRequest(APIModel):
    side: BookSide
    horizon_seconds: float = Field(default=60.0, gt=0.0, le=86_400.0)
    price: DecimalStr | None = None
    #: What is ahead in the queue. Unset means the level's entire displayed
    #: size, which is where an order joining now would sit.
    quantity_ahead: DecimalStr | None = None
    as_of: datetime | None = None

    @model_validator(mode="after")
    def quantity_is_not_negative(self) -> QueueEstimateRequest:
        if self.quantity_ahead is not None and self.quantity_ahead < Decimal(0):
            raise ValueError("a negative queue ahead is not a queue position")
        return self


class QueueEstimateOut(APIModel):
    """Deliberately has no single fill probability. The bracket is the answer."""

    id: uuid.UUID
    dataset_id: uuid.UUID
    side: str
    price: DecimalStr
    snapshot_timestamp: datetime
    quantity_ahead: DecimalStr
    level_quantity: DecimalStr
    horizon_seconds: float
    trades_observed: int
    cancels_observed: int
    pessimistic_fill_probability: float
    optimistic_fill_probability: float
    pessimistic_wait_seconds: float | None = None
    optimistic_wait_seconds: float | None = None
    confidence: float
    assumptions: list[str]
    created_at: datetime
