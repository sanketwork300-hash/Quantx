"""One user action, the whole options pipeline.

"Select NIFTY and show me the volatility surface" is four steps: capture the
live chain into a snapshot, solve implied volatilities, fit SVI, and read the
shape off the fit. Each of those already exists and none of them is rewritten
here. What this module adds is that they run as one job, against one captured
moment, with each stage's identifier carried into the next so the whole chain is
traceable from the surface back to the individual quotes it was built from.

Running it as a job rather than a request is not politeness about latency: an
SVI calibration across a dozen expiries is seconds of numerical work, and build
spec rule 11 puts that on a worker.

A stage that fails stops the ones that depend on it and says so. A chain with no
solvable implied volatilities produces no surface, and the job reports that
rather than an empty surface that would calibrate to nothing and plot as flat.
"""

from __future__ import annotations

import uuid
from datetime import date, time

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.application import (
    AnalyzeChainParams,
    CalibrateSurfaceParams,
    DerivativesService,
)
from domains.derivatives.delta_skew import DELTA_LEVELS, surface_delta_skew
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.quality.flags import Severity
from domains.market_data.service import MarketDataService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


class LiveOptionsStage:
    """Named so a partial result says exactly how far it got."""

    CAPTURE = "capture"
    ANALYSE = "analyse"
    GREEKS = "greeks"
    CALIBRATE = "calibrate"
    SKEW = "delta_skew"


async def analyse_live_chain(session: AsyncSession, job: Job) -> dict:
    """Capture a live option chain and take it through to a fitted surface."""
    payload = job.input_reference
    settings = get_settings()

    underlying_id = uuid.UUID(payload["underlying_id"])
    expiry = date.fromisoformat(payload["expiry"]) if payload.get("expiry") else None
    settlement = (
        time.fromisoformat(payload["settlement_time_utc"])
        if payload.get("settlement_time_utc")
        else None
    )

    market_data = MarketDataService(session, settings, get_object_store(settings))
    derivatives = DerivativesService(session, settings)

    stages: dict[str, str] = {}
    warnings: list[dict] = []

    # ---------------------------------------------------------------- capture
    capture = await market_data.capture_live_chain(
        user_id=job.user_id,
        underlying_id=underlying_id,
        expiry=expiry,
        exclusion_threshold=Severity.ERROR,
    )
    summary = capture.results
    stages[LiveOptionsStage.CAPTURE] = "OK"
    warnings.extend(warning.to_dict() for warning in capture.warnings)

    result: dict = {
        "underlying_id": str(underlying_id),
        "expiry": expiry.isoformat() if expiry else None,
        "capture": summary.to_dict(),
        "stages": stages,
        "warnings": warnings,
    }

    if summary.quotes_kept == 0:
        # Nothing survived the quality gate. Every later stage would produce an
        # empty object that reads like a flat market rather than like no data.
        stages[LiveOptionsStage.ANALYSE] = "SKIPPED_NO_USABLE_QUOTES"
        return result

    # ---------------------------------------------------------------- analyse
    analysis_result, analysis_id = await derivatives.analyze_chain(
        user_id=job.user_id,
        snapshot_id=summary.snapshot_id,
        params=AnalyzeChainParams(
            risk_free_rate=float(payload.get("risk_free_rate") or 0.0),
            dividend_yield=float(payload.get("dividend_yield") or 0.0),
            dividend_yield_assumed=payload.get("dividend_yield") is None,
            settlement_time_utc=settlement,
        ),
        market_data=market_data,
    )
    stages[LiveOptionsStage.ANALYSE] = "OK"
    warnings.extend(warning.to_dict() for warning in analysis_result.warnings)
    result["analysis_id"] = str(analysis_id)
    result["analysis"] = analysis_result.results.to_dict(include_points=False)

    # ``total_solved`` counts converged solves. Counting points instead would
    # let a chain where every solve failed proceed to a calibration with nothing
    # to fit, which produces a surface that plots as flat.
    if analysis_result.results.total_solved == 0:
        stages[LiveOptionsStage.CALIBRATE] = "SKIPPED_NO_IMPLIED_VOLS"
        return result

    # Greeks against each contract's own solved volatility, so the chain
    # describes the market as quoted rather than as fitted.
    greeks = await derivatives.chain_greeks(analysis_id, job.user_id)
    if greeks is not None:
        result["greeks"] = greeks.to_dict(include_contracts=False)
        stages[LiveOptionsStage.GREEKS] = "OK"

    if not payload.get("calibrate", True):
        stages[LiveOptionsStage.CALIBRATE] = "NOT_REQUESTED"
        return result

    # -------------------------------------------------------------- calibrate
    surface_result, surface_id = await derivatives.calibrate_surface(
        user_id=job.user_id,
        analysis_id=analysis_id,
        params=CalibrateSurfaceParams(),
    )
    stages[LiveOptionsStage.CALIBRATE] = "OK"
    warnings.extend(warning.to_dict() for warning in surface_result.warnings)
    result["surface_id"] = str(surface_id)
    result["surface"] = surface_result.results

    # ------------------------------------------------------------- delta skew
    # Computed from the surface that was just stored, and not stored itself: it
    # is a pure function of five SVI numbers per slice, so recomputing it on
    # read can never disagree with a stored copy that has drifted.
    loaded = await derivatives.load_surface(surface_id, job.user_id)
    if loaded is not None:
        _row, surface = loaded
        result["delta_skew"] = surface_delta_skew(surface, DELTA_LEVELS).to_dict()
        stages[LiveOptionsStage.SKEW] = "OK"

    return result


def register_handlers() -> None:
    register(JobType.ANALYSE_LIVE_CHAIN, analyse_live_chain)
