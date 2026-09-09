from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


#: Legal transitions. Enforced in the service so a race between a worker and a
#: cancellation cannot resurrect a terminal job.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class JobType(StrEnum):
    INGEST_OPTION_CHAIN = "INGEST_OPTION_CHAIN"
    ANALYZE_OPTION_CHAIN = "ANALYZE_OPTION_CHAIN"
    CALIBRATE_SURFACE = "CALIBRATE_SURFACE"
    SCAN_ANOMALIES = "SCAN_ANOMALIES"
    VALUE_PORTFOLIO = "VALUE_PORTFOLIO"
    IMPORT_POSITIONS = "IMPORT_POSITIONS"
    RUN_VAR = "RUN_VAR"
    RUN_STRESS = "RUN_STRESS"
    RUN_MARGIN = "RUN_MARGIN"
    IMPORT_TRADES = "IMPORT_TRADES"
    ANALYZE_EXECUTIONS = "ANALYZE_EXECUTIONS"
    SIMULATE_EXECUTION = "SIMULATE_EXECUTION"
    CALIBRATE_GLOBAL_SURFACE = "CALIBRATE_GLOBAL_SURFACE"
    PRICE_CONSENSUS = "PRICE_CONSENSUS"
    IMPORT_BOOK_DATA = "IMPORT_BOOK_DATA"
    ANALYZE_MICROSTRUCTURE = "ANALYZE_MICROSTRUCTURE"
    FIT_INTENSITY = "FIT_INTENSITY"
    #: Load a provider's instrument file into canonical instruments and the
    #: alias mapping. A job rather than a request because the published files
    #: are large and the work has nothing to do with any one HTTP call.
    LOAD_INSTRUMENT_MASTER = "LOAD_INSTRUMENT_MASTER"
    #: Capture a live option chain and take it through to a fitted surface.
    #: One job rather than four so that every stage sees the same captured
    #: moment; an SVI calibration is seconds of numerical work and does not
    #: belong in a request.
    ANALYSE_LIVE_CHAIN = "ANALYSE_LIVE_CHAIN"
    #: Read a historical file, validate it, and write its partitions into the
    #: warehouse. A job because a year of minute bars is millions of rows and
    #: the work has nothing to do with any one HTTP call.
    INGEST_HISTORICAL_DATASET = "INGEST_HISTORICAL_DATASET"
    #: Offer the current market to every resting order on a paper account. A
    #: paper account has no exchange behind it, so a resting limit order only
    #: moves when something asks it to.
    WORK_RESTING_ORDERS = "WORK_RESTING_ORDERS"
    #: Run a strategy over warehouse bars and record the experiment. A job
    #: because a long window with per-bar features is real work, and because the
    #: result is a record rather than a response.
    RUN_BACKTEST = "RUN_BACKTEST"
    # Later phases register their types here; the enum is the contract between
    # the API, the worker and the frontend.


@dataclass(frozen=True, slots=True)
class Job:
    id: uuid.UUID
    user_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    progress: float
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    input_reference: dict = field(default_factory=dict)
    result_reference: dict | None = None
    error: dict | None = None
