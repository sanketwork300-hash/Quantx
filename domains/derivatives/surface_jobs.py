"""Surface calibration job handler.

Calibration is a job because it scales with the chain: SLSQP with ten starts per
expiry over a full option market is minutes of work, not milliseconds.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.anomaly import AnomalyPolicy
from domains.derivatives.application import CalibrateSurfaceParams, DerivativesService
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from infrastructure.settings import get_settings


async def calibrate_surface(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    derivatives = DerivativesService(session, get_settings())

    params = CalibrateSurfaceParams(
        seed=int(payload.get("seed") or 20_260_924),
        use_weights=bool(payload.get("use_weights", True)),
    )
    result, surface_row_id = await derivatives.calibrate_surface(
        job.user_id, uuid.UUID(payload["analysis_id"]), params
    )
    out = result.to_dict()
    # Inside `results`, matching GET /derivatives/surfaces/{id}: the row id is
    # part of the result payload, not envelope metadata.
    if out.get("results") is not None:
        out["results"]["surface_row_id"] = str(surface_row_id)
    return out


async def scan_anomalies(session: AsyncSession, job: Job) -> dict:
    """Scan a surface for deviations.

    A job because a full option-market scan is hundreds of thousands of
    comparisons, each of which reads history for its contract.
    """
    payload = job.input_reference
    derivatives = DerivativesService(session, get_settings())

    policy = AnomalyPolicy(
        min_z_score=float(payload.get("min_z_score", 2.0)),
        require_outside_envelope=bool(payload.get("require_outside_envelope", True)),
        min_confidence=float(payload.get("min_confidence", 0.3)),
        min_liquidity=float(payload.get("min_liquidity", 0.05)),
    )
    result, scan_row_id = await derivatives.scan_anomalies(
        job.user_id, uuid.UUID(payload["surface_row_id"]), policy
    )
    out = result.to_dict()
    if out.get("results") is not None:
        out["results"]["scan_row_id"] = str(scan_row_id)
    return out


def register_handlers() -> None:
    register(JobType.CALIBRATE_SURFACE, calibrate_surface)
    register(JobType.SCAN_ANOMALIES, scan_anomalies)
