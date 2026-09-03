"""Microstructure application service.

The shape of every method here is the same, and it is the phase's whole design:
**read the dataset's stored availability report, require the capability, then
compute.** Nothing computes first and warns afterwards. A capability the gate
refused produces a refusal carrying the reason and the evidence it was decided
on, and there is no parameter that overrides it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pyarrow as pa
from sqlalchemy.ext.asyncio import AsyncSession

from domains.market_data.enums import BookEventType
from domains.market_data.ingestion.column_mapping import ColumnMapping
from domains.market_data.models import BookEvent, OrderBookSnapshot
from domains.microstructure.analytics import (
    ANALYTICS_MODEL_VERSION,
    BookAnalyticsParams,
    BookAnalyticsResult,
    analyse_snapshots,
)
from domains.microstructure.availability import (
    GATE_VERSION,
    AvailabilityReport,
    CapabilityRefused,
    MicrostructureCapability,
    assess,
)
from domains.microstructure.importer import (
    EventImporter,
    LevelColumns,
    SnapshotImporter,
)
from domains.microstructure.intensity import (
    INTENSITY_MODEL_VERSION,
    IntensityParams,
    fit_intensity,
)
from domains.microstructure.models import (
    IMPORT_MODEL_VERSION,
    DatasetKind,
    EventBatch,
    SnapshotBatch,
    profile_dataset,
)
from domains.microstructure.orm import MicrostructureDatasetORM
from domains.microstructure.queue import (
    QUEUE_MODEL_VERSION,
    QueueParams,
    QueueResult,
)
from domains.microstructure.queue import estimate as estimate_queue
from domains.microstructure.repository import MicrostructureRepository
from domains.microstructure.storage import STORAGE_VERSION, MicrostructureStore
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from infrastructure.settings import Settings
from infrastructure.storage.base import ObjectStore
from quant.microstructure.intensity import IntensityUnavailable
from quant.microstructure.queue import QueueUnavailable


class MicrostructureError(Exception):
    pass


class WarningCode:
    ROWS_REJECTED = "MICROSTRUCTURE_ROWS_REJECTED"
    CAPABILITY_REFUSED = "MICROSTRUCTURE_CAPABILITY_REFUSED"
    SNAPSHOTS_ONLY = "MICROSTRUCTURE_SNAPSHOTS_ONLY"
    EVENTS_ONLY = "MICROSTRUCTURE_EVENTS_ONLY"
    CROSSED_OR_LOCKED = "MICROSTRUCTURE_CROSSED_OR_LOCKED_BOOKS"
    HAWKES_NOT_ADOPTED = "INTENSITY_HAWKES_NOT_ADOPTED"
    QUEUE_IS_A_BRACKET = "QUEUE_ESTIMATE_IS_A_BRACKET"
    MEASURE_UNAVAILABLE = "BOOK_MEASURE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ImportParams:
    """What to import, and what to call it. Nothing here is inferred silently."""

    instrument_id: uuid.UUID
    name: str
    source: str = "user-upload"
    snapshot_columns: LevelColumns | None = None
    event_mapping: ColumnMapping | None = None
    snapshot_upload_id: uuid.UUID | None = None
    event_upload_id: uuid.UUID | None = None

    def to_provenance(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "name": self.name,
            "source": self.source,
            "snapshot_columns": (
                self.snapshot_columns.to_dict() if self.snapshot_columns else None
            ),
            "event_mapping": (self.event_mapping.to_dict() if self.event_mapping else None),
            "snapshot_upload_id": (
                str(self.snapshot_upload_id) if self.snapshot_upload_id else None
            ),
            "event_upload_id": str(self.event_upload_id) if self.event_upload_id else None,
        }


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """What a commit would produce, computed without writing anything."""

    snapshots: SnapshotBatch
    events: EventBatch
    availability: AvailabilityReport
    detected_snapshot_columns: dict
    detected_event_mapping: dict

    @property
    def committable(self) -> bool:
        """Something usable has to come out, or there is no dataset to make."""
        return bool(self.snapshots.snapshots) or bool(self.events.events)

    def to_dict(self, sample_rows: int = 5) -> dict:
        return {
            "committable": self.committable,
            "snapshots": {
                **self.snapshots.counts(),
                "detected_levels": self.snapshots.detected_levels,
                "rejected_sample": [row.to_dict() for row in self.snapshots.rejected[:sample_rows]],
            },
            "events": {
                **self.events.counts(),
                "rejected_sample": [row.to_dict() for row in self.events.rejected[:sample_rows]],
            },
            "detected_snapshot_columns": self.detected_snapshot_columns,
            "detected_event_mapping": self.detected_event_mapping,
            "availability": self.availability.to_dict(),
        }


def _rejection_counts(*batches) -> dict[str, int]:
    counts: dict[str, int] = {}
    for batch in batches:
        for row in batch:
            key = str(row.reason)
            counts[key] = counts.get(key, 0) + 1
    return counts


class MicrostructureApplicationService:
    def __init__(
        self, session: AsyncSession, settings: Settings, object_store: ObjectStore
    ) -> None:
        self._session = session
        self._settings = settings
        self.repository = MicrostructureRepository(session)
        self._store = MicrostructureStore(object_store, settings.code_commit)

    # ---------------------------------------------------------------- import
    def parse(
        self,
        params: ImportParams,
        snapshot_data: bytes | None,
        event_data: bytes | None,
        limit: int | None = None,
    ) -> ImportPreview:
        """Parse both halves and assess what they would support. No writes."""
        snapshot_importer = SnapshotImporter(
            params.instrument_id, params.source, max_rows=self._settings.max_upload_rows
        )
        event_importer = EventImporter(
            params.instrument_id, params.source, max_rows=self._settings.max_upload_rows
        )

        if snapshot_data:
            # A supplied mapping wins; otherwise the importer detects the level
            # columns itself and reports what it detected in the preview.
            snapshots = snapshot_importer.parse(snapshot_data, params.snapshot_columns, limit)
        else:
            snapshots = SnapshotBatch(snapshots=(), rejected=(), rows_in=0)

        if event_data:
            events = event_importer.parse(event_data, params.event_mapping, limit)
        else:
            events = EventBatch(events=(), rejected=(), rows_in=0)

        profile = profile_dataset(params.instrument_id, snapshots.snapshots, events.events)
        return ImportPreview(
            snapshots=snapshots,
            events=events,
            availability=assess(profile),
            detected_snapshot_columns=snapshots.detected_columns,
            detected_event_mapping=events.detected_columns,
        )

    async def commit_import(
        self,
        user_id: uuid.UUID,
        params: ImportParams,
        snapshot_data: bytes | None,
        event_data: bytes | None,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        """Write the parquet, record the dataset, store every rejection."""
        preview = self.parse(params, snapshot_data, event_data)
        warnings: list[AnalyticalWarning] = []
        # Digest the bytes that were actually parsed rather than the upload
        # row's, so a dataset is tied to its input whatever route the input
        # arrived by. Two datasets reporting the same digest saw the same bytes.
        digests = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in (("snapshots", snapshot_data), ("events", event_data))
            if data
        }
        dataset_digest = hashlib.sha256(
            "".join(f"{name}:{digest}" for name, digest in sorted(digests.items())).encode()
        ).hexdigest()
        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_data_sources=(params.source,),
            dataset_versions={**digests, "dataset": dataset_digest},
            model_versions={
                "import": IMPORT_MODEL_VERSION,
                "storage": STORAGE_VERSION,
                "availability": GATE_VERSION,
            },
            parameters=params.to_provenance(),
        )

        if not preview.committable:
            return (
                AnalyticalResult.failed(
                    provenance,
                    (
                        AnalyticalWarning.error(
                            WarningCode.ROWS_REJECTED,
                            "No snapshot and no event survived parsing, so there "
                            "is no dataset to create. Every rejected row and its "
                            "reason is in the preview.",
                            snapshot_rows=preview.snapshots.counts(),
                            event_rows=preview.events.counts(),
                        ),
                    ),
                ),
                None,
            )

        dataset_id = uuid.uuid4()
        snapshot_key = event_key = None
        if preview.snapshots.snapshots:
            snapshot_key = await self._store.put_snapshots(
                user_id,
                dataset_id,
                params.instrument_id,
                preview.snapshots.snapshots,
                params.source,
            )
        if preview.events.events:
            event_key = await self._store.put_events(
                user_id,
                dataset_id,
                params.instrument_id,
                preview.events.events,
                params.source,
            )

        rejection_key = None
        rejected_total = len(preview.snapshots.rejected) + len(preview.events.rejected)
        if rejected_total:
            rejection_key = await self._store.put_rejections(
                user_id,
                dataset_id,
                {
                    "dataset_id": str(dataset_id),
                    "snapshot_rejections": [row.to_dict() for row in preview.snapshots.rejected],
                    "event_rejections": [row.to_dict() for row in preview.events.rejected],
                },
            )
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.ROWS_REJECTED,
                    f"{rejected_total} rows could not be read and are not in the "
                    "dataset. Each is listed with its source row number and "
                    "reason; nothing was dropped silently.",
                    counts=_rejection_counts(preview.snapshots.rejected, preview.events.rejected),
                )
            )

        profile = preview.availability.profile
        if profile.kind is DatasetKind.SNAPSHOTS_ONLY:
            warnings.append(
                AnalyticalWarning.info(
                    WarningCode.SNAPSHOTS_ONLY,
                    "This dataset holds depth snapshots but no event tape, so "
                    "book analytics are available and arrival-intensity and "
                    "queue models are not. The changes between snapshots are "
                    "not the messages that caused them.",
                )
            )
        elif profile.kind is DatasetKind.EVENTS_ONLY:
            warnings.append(
                AnalyticalWarning.info(
                    WarningCode.EVENTS_ONLY,
                    "This dataset holds an event tape but no depth snapshots, so "
                    "intensity models are available and anything needing a book "
                    "at an instant is not.",
                )
            )
        if profile.crossed_snapshots or profile.locked_snapshots:
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.CROSSED_OR_LOCKED,
                    f"{profile.crossed_snapshots} snapshots are crossed and "
                    f"{profile.locked_snapshots} are locked. They are kept and "
                    "measured, and flagged here, because a crossed book is "
                    "usually a stale or interleaved feed rather than a market.",
                    crossed=profile.crossed_snapshots,
                    locked=profile.locked_snapshots,
                )
            )

        row = await self.repository.add_dataset(
            id=dataset_id,
            user_id=user_id,
            instrument_id=params.instrument_id,
            name=params.name,
            kind=str(profile.kind),
            snapshot_upload_id=params.snapshot_upload_id,
            event_upload_id=params.event_upload_id,
            snapshot_key=snapshot_key,
            event_key=event_key,
            rejection_key=rejection_key,
            source=params.source,
            dataset_digest=dataset_digest,
            snapshot_rows_in=preview.snapshots.rows_in,
            snapshot_rows_kept=len(preview.snapshots.snapshots),
            snapshot_rows_rejected=len(preview.snapshots.rejected),
            event_rows_in=preview.events.rows_in,
            event_rows_kept=len(preview.events.events),
            event_rows_rejected=len(preview.events.rejected),
            rejection_counts=_rejection_counts(preview.snapshots.rejected, preview.events.rejected),
            first_timestamp=profile.first_timestamp,
            last_timestamp=profile.last_timestamp,
            span_seconds=profile.span_seconds,
            max_depth_levels=profile.max_levels,
            availability=preview.availability.to_dict(),
            available_capabilities=[str(item) for item in preview.availability.available],
            provenance=provenance.to_dict(),
        )
        payload = preview.to_dict()
        payload["dataset_id"] = str(row.id)
        return AnalyticalResult.ok(payload, provenance, tuple(warnings)), row.id

    # ------------------------------------------------------------ dataset io
    async def get_dataset(
        self, dataset_id: uuid.UUID, user_id: uuid.UUID
    ) -> MicrostructureDatasetORM | None:
        return await self.repository.get_dataset(dataset_id, user_id)

    async def list_datasets(self, user_id: uuid.UUID, **kwargs):
        return await self.repository.list_datasets(user_id, **kwargs)

    async def rejections(self, dataset: MicrostructureDatasetORM) -> dict:
        """Every rejected row, by source row number and reason."""
        if not dataset.rejection_key:
            return {"snapshot_rejections": [], "event_rejections": []}
        return await self._store.get_rejections(dataset.rejection_key)

    async def load_snapshots(
        self, dataset: MicrostructureDatasetORM
    ) -> tuple[OrderBookSnapshot, ...]:
        if not dataset.snapshot_key:
            return ()
        return await self._store.get_snapshots(
            dataset.snapshot_key, dataset.instrument_id, dataset.source
        )

    async def load_events(self, dataset: MicrostructureDatasetORM) -> tuple[BookEvent, ...]:
        if not dataset.event_key:
            return ()
        return await self._store.get_events(
            dataset.event_key, dataset.instrument_id, dataset.source
        )

    @staticmethod
    def availability(dataset: MicrostructureDatasetORM) -> dict:
        return dict(dataset.availability or {})

    @staticmethod
    def _capability(dataset: MicrostructureDatasetORM, capability) -> dict | None:
        for entry in (dataset.availability or {}).get("capabilities", []):
            if entry.get("capability") == str(capability):
                return entry
        return None

    def require_capability(self, dataset: MicrostructureDatasetORM, capability) -> None:
        """The one way through the gate, read from what was stored at import."""
        entry = self._capability(dataset, capability)
        if entry is None or not entry.get("available"):
            from domains.microstructure.availability import (
                AvailabilityRefusal,
                CapabilityAssessment,
            )

            reason = (entry or {}).get("reason")
            raise CapabilityRefused(
                CapabilityAssessment(
                    capability=capability,
                    is_available=False,
                    reason=AvailabilityRefusal(reason) if reason else None,
                    message=(entry or {}).get(
                        "message",
                        f"This dataset does not support {capability}.",
                    ),
                    evidence=(entry or {}).get("evidence", {}),
                )
            )

    # -------------------------------------------------------------- analytics
    async def analyse(
        self,
        user_id: uuid.UUID,
        dataset: MicrostructureDatasetORM,
        params: BookAnalyticsParams,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=dataset.last_timestamp,
            market_data_sources=(dataset.source,),
            dataset_versions=_dataset_versions(dataset),
            model_versions={
                "book_analytics": ANALYTICS_MODEL_VERSION,
                "availability": GATE_VERSION,
            },
            parameters={"dataset_id": str(dataset.id), **params.to_dict()},
        )
        self.require_capability(dataset, MicrostructureCapability.TOP_OF_BOOK)

        warnings: list[AnalyticalWarning] = []
        depth = self._capability(dataset, MicrostructureCapability.DEPTH_ANALYTICS)
        if depth is not None and not depth.get("available"):
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.CAPABILITY_REFUSED,
                    "Depth analytics are refused on this dataset, so the book "
                    "slope, the depth concentration and the multi-level weighted "
                    f"imbalance are absent rather than approximated: {depth.get('message')}",
                    capability=str(MicrostructureCapability.DEPTH_ANALYTICS),
                    reason=depth.get("reason"),
                )
            )

        snapshots = await self.load_snapshots(dataset)
        result = analyse_snapshots(snapshots, params)

        for summary in result.summaries:
            if summary.observations == 0 and summary.missing:
                warnings.append(
                    AnalyticalWarning.info(
                        WarningCode.MEASURE_UNAVAILABLE,
                        f"{summary.name} could not be measured on any of the "
                        f"{summary.missing} snapshots.",
                        measure=summary.name,
                        reasons=summary.missing_reasons,
                    )
                )

        report_id = uuid.uuid4()
        series_key = None
        if result.series:
            series_key = await self._store.put_series(user_id, report_id, _series_table(result))

        row = await self.repository.add_report(
            id=report_id,
            user_id=user_id,
            dataset_id=dataset.id,
            instrument_id=dataset.instrument_id,
            levels=params.levels,
            weighted_decay=params.weighted_decay,
            snapshots_analysed=result.snapshots_analysed,
            window_start=result.first_timestamp,
            window_end=result.last_timestamp,
            crossed_snapshots=result.crossed_snapshots,
            locked_snapshots=result.locked_snapshots,
            median_spread=_median(result, "spread"),
            median_relative_spread=_median(result, "relative_spread"),
            median_imbalance=_median(result, "imbalance"),
            median_microprice_tilt=_median(result, "microprice_tilt"),
            measures=[item.to_dict() for item in result.summaries],
            trade_costs=[item.to_dict() for item in result.trade_costs],
            preview_series=[_jsonable(row) for row in result.preview_series()],
            series_key=series_key,
            warnings=[warning.to_dict() for warning in warnings],
            provenance=provenance.to_dict(),
        )
        payload = result.to_dict()
        payload["report_id"] = str(row.id)
        payload["dataset_id"] = str(dataset.id)
        return AnalyticalResult.ok(payload, provenance, tuple(warnings)), row.id

    # -------------------------------------------------------------- intensity
    async def fit_intensity(
        self,
        user_id: uuid.UUID,
        dataset: MicrostructureDatasetORM,
        params: IntensityParams,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=dataset.last_timestamp,
            market_data_sources=(dataset.source,),
            dataset_versions=_dataset_versions(dataset),
            model_versions={"intensity": INTENSITY_MODEL_VERSION, "availability": GATE_VERSION},
            parameters={"dataset_id": str(dataset.id), **params.to_dict()},
        )
        self.require_capability(dataset, MicrostructureCapability.EVENT_INTENSITY)
        if BookEventType.CANCEL in params.event_types:
            self.require_capability(dataset, MicrostructureCapability.CANCELLATION_INTENSITY)

        warnings: list[AnalyticalWarning] = []
        excitation = self._capability(dataset, MicrostructureCapability.SELF_EXCITATION)
        events = await self.load_events(dataset)

        try:
            result = fit_intensity(events, params, dataset.first_timestamp, dataset.last_timestamp)
        except IntensityUnavailable as exc:
            return (
                AnalyticalResult.failed(
                    provenance,
                    (
                        AnalyticalWarning.error(
                            WarningCode.CAPABILITY_REFUSED,
                            str(exc),
                            reason=str(exc.reason),
                            scope=params.scope,
                        ),
                    ),
                ),
                None,
            )

        if excitation is not None and not excitation.get("available"):
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.CAPABILITY_REFUSED,
                    "The self-exciting model was fitted for comparison only: this "
                    f"dataset does not support reporting one. {excitation.get('message')}",
                    capability=str(MicrostructureCapability.SELF_EXCITATION),
                    reason=excitation.get("reason"),
                )
            )
        adopted = result.comparison.hawkes_is_adopted and (
            excitation is None or bool(excitation.get("available"))
        )
        if not adopted:
            warnings.append(
                AnalyticalWarning.info(
                    WarningCode.HAWKES_NOT_ADOPTED,
                    result.comparison.reason
                    if result.comparison.hawkes_is_adopted is False
                    else "The self-exciting fit is withheld because the dataset "
                    "does not support reporting one, so the constant-rate "
                    "baseline is what is reported.",
                    statistic=result.comparison.predictive.statistic,
                    critical_value=result.comparison.predictive.critical_value,
                )
            )

        payload = result.to_dict()
        payload["adopted_model"] = "HAWKES_EXPONENTIAL" if adopted else "POISSON"
        payload["adopted_rate_per_second"] = (
            result.adopted_rate if adopted else result.comparison.poisson_train.parameters.rate
        )
        payload["dataset_id"] = str(dataset.id)

        hawkes = result.comparison.hawkes_train
        predictive = result.comparison.predictive
        row = await self.repository.add_intensity(
            user_id=user_id,
            dataset_id=dataset.id,
            instrument_id=dataset.instrument_id,
            scope=params.scope,
            event_types=[str(item) for item in params.event_types] or ["ALL"],
            side=str(params.side) if params.side else None,
            price=params.price,
            events_selected=result.events_selected,
            window_start=result.window_start,
            window_end=result.window_end,
            split_timestamp=result.split_timestamp,
            train_fraction=params.train_fraction,
            poisson_rate=result.comparison.poisson_train.parameters.rate,
            poisson_train_log_likelihood=result.comparison.poisson_train.log_likelihood,
            poisson_held_out_log_likelihood=result.comparison.poisson_held_out_log_likelihood,
            hawkes_mu=hawkes.parameters.mu,
            hawkes_alpha=hawkes.parameters.alpha,
            hawkes_beta=hawkes.parameters.beta,
            hawkes_branching_ratio=hawkes.parameters.branching_ratio,
            hawkes_train_log_likelihood=_finite(hawkes.log_likelihood),
            hawkes_held_out_log_likelihood=_finite(
                result.comparison.hawkes_held_out_log_likelihood
            ),
            hawkes_converged=hawkes.converged,
            held_out_events=result.comparison.held_out_events,
            mean_gain_per_event=_finite(predictive.mean_gain),
            test_statistic=_finite(predictive.statistic),
            critical_value=predictive.critical_value,
            hawkes_is_adopted=adopted,
            adopted_model="HAWKES_EXPONENTIAL" if adopted else "POISSON",
            adopted_rate=payload["adopted_rate_per_second"],
            verdict_reason=result.comparison.reason[:1000],
            comparison=payload,
            warnings=[warning.to_dict() for warning in warnings],
            provenance=provenance.to_dict(),
        )
        payload["intensity_model_id"] = str(row.id)
        return AnalyticalResult.ok(payload, provenance, tuple(warnings)), row.id

    # ------------------------------------------------------------------ queue
    async def estimate_queue(
        self,
        user_id: uuid.UUID,
        dataset: MicrostructureDatasetORM,
        params: QueueParams,
    ) -> tuple[AnalyticalResult[dict], uuid.UUID | None]:
        provenance = Provenance.now(
            code_commit=self._settings.code_commit,
            market_state_timestamp=dataset.last_timestamp,
            market_data_sources=(dataset.source,),
            dataset_versions=_dataset_versions(dataset),
            model_versions={"queue": QUEUE_MODEL_VERSION, "availability": GATE_VERSION},
            parameters={"dataset_id": str(dataset.id), **params.to_dict()},
        )
        self.require_capability(dataset, MicrostructureCapability.QUEUE_POSITION)

        snapshots = await self.load_snapshots(dataset)
        events = await self.load_events(dataset)
        try:
            result: QueueResult = estimate_queue(snapshots, events, params)
        except QueueUnavailable as exc:
            return (
                AnalyticalResult.failed(
                    provenance,
                    (
                        AnalyticalWarning.error(
                            WarningCode.CAPABILITY_REFUSED,
                            str(exc),
                            reason=str(exc.reason),
                        ),
                    ),
                ),
                None,
            )

        low, high = result.outlook.fill_probability_range
        warnings = (
            AnalyticalWarning.warn(
                WarningCode.QUEUE_IS_A_BRACKET,
                "This is a bracket, not a number. The two ends differ only in "
                "where cancellations at the level are assumed to come from, and "
                "public data does not say. The fill probability lies somewhere "
                f"between {low:.2f} and {high:.2f} if every stated assumption "
                "holds.",
                low=low,
                high=high,
                confidence=result.outlook.confidence,
            ),
        )
        payload = result.to_dict()
        payload["dataset_id"] = str(dataset.id)

        fast, slow = result.outlook.wait_seconds_range
        row = await self.repository.add_queue_estimate(
            user_id=user_id,
            dataset_id=dataset.id,
            instrument_id=dataset.instrument_id,
            side=str(params.side),
            price=result.price,
            snapshot_timestamp=result.snapshot_timestamp,
            quantity_ahead=Decimal(str(result.outlook.quantity_ahead)),
            level_quantity=Decimal(str(result.outlook.level_quantity)),
            horizon_seconds=params.horizon_seconds,
            observation_window_seconds=result.outlook.observation_window_seconds,
            trades_observed=result.outlook.trades_observed,
            cancels_observed=result.outlook.cancels_observed,
            pessimistic_fill_probability=low,
            pessimistic_wait_seconds=_finite(slow),
            optimistic_fill_probability=high,
            optimistic_wait_seconds=_finite(fast),
            confidence=result.outlook.confidence,
            assumptions=list(result.outlook.assumptions),
            detail=payload,
            warnings=[warning.to_dict() for warning in warnings],
            provenance=provenance.to_dict(),
        )
        payload["queue_estimate_id"] = str(row.id)
        return AnalyticalResult.ok(payload, provenance, warnings), row.id


# --------------------------------------------------------------------- helpers
def _dataset_versions(dataset: MicrostructureDatasetORM) -> dict[str, str]:
    """The digest of the bytes this dataset was built from.

    A historical analysis has to be reproducible later, and "which file was
    this?" is the first question that needs answering.
    """
    return {"dataset": dataset.dataset_digest} if dataset.dataset_digest else {}


def _median(result: BookAnalyticsResult, measure: str) -> float | None:
    summary = result.summary(measure)
    if summary is None:
        return None
    return summary.percentiles.get("p50")


def _finite(value: float | None) -> float | None:
    """``inf`` and ``nan`` are not values a Float column should hold."""
    if value is None:
        return None
    import math

    return value if math.isfinite(value) else None


def _jsonable(row: dict) -> dict:
    out: dict = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = format(value, "f")
        else:
            out[key] = value
    return out


def _series_table(result: BookAnalyticsResult) -> pa.Table:
    """The full per-snapshot series, for the object store."""
    columns: dict[str, list] = {}
    for row in result.series:
        for key, value in row.items():
            columns.setdefault(key, []).append(value)
    arrays = {}
    for key, values in columns.items():
        if key == "timestamp":
            arrays[key] = pa.array(values, pa.timestamp("us", tz="UTC"))
        elif key in {"bid_levels", "ask_levels"}:
            arrays[key] = pa.array(values, pa.int32())
        else:
            arrays[key] = pa.array(values, pa.float64())
    return pa.table(arrays)


__all__ = [
    "BookAnalyticsParams",
    "ImportParams",
    "ImportPreview",
    "IntensityParams",
    "MicrostructureApplicationService",
    "MicrostructureError",
    "QueueParams",
    "WarningCode",
]
