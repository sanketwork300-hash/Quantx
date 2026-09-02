"""Derivatives routes.

Thin: parameter validation, ownership, and translation between the HTTP schema
and the domain service. No financial mathematics (enforced by
``scripts/check_layering.py``).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    CurrentUser,
    DerivativesServiceDep,
    JobServiceDep,
    MarketDataServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.derivatives import (
    AnalysisSummaryOut,
    AnalyzeChainRequest,
    CalibrateSurfaceRequest,
    ForwardEstimateOut,
    ForwardRequest,
    GreeksRequest,
    ImpliedVolPointOut,
    ImpliedVolRequest,
    ReferenceRequest,
    ScanAnomaliesRequest,
    SurfaceSummaryOut,
)
from api.schemas.uploads import JobAcceptedOut
from domains.derivatives.application import AnalysisError
from domains.instruments.enums import OptionType
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.reports.provenance import Provenance
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/derivatives", tags=["derivatives"])


def _calculator_envelope(results: dict, settings, model_versions: dict) -> Envelope:
    """Stateless calculators still carry provenance: the model that produced a
    number is part of the number."""
    provenance = Provenance.now(code_commit=settings.code_commit, model_versions=model_versions)
    return Envelope(
        status="OK", results=results, warnings=[], provenance=ProvenanceOut(**provenance.to_dict())
    )


# ---------------------------------------------------------------- calculators
@router.post("/iv", response_model=Envelope)
async def implied_volatility(
    payload: ImpliedVolRequest,
    _user: CurrentUser,
    derivatives: DerivativesServiceDep,
    settings: SettingsDep,
) -> Envelope:
    """Invert one option price.

    A quote with no invertible volatility returns a named reason, never a null
    with no explanation and never a clipped value.
    """
    try:
        results = derivatives.implied_volatility(
            price=payload.price,
            strike=payload.strike,
            tau=payload.time_to_expiry,
            is_call=payload.is_call,
            spot=payload.spot,
            forward=payload.forward,
            rate=payload.rate,
            dividend=payload.dividend_yield,
        )
    except AnalysisError as exc:
        raise UnprocessableEntity("INVALID_IV_REQUEST", str(exc)) from exc

    envelope = _calculator_envelope(
        results, settings, {"implied_volatility": "implied-vol-black76@1.0.0"}
    )
    if results["implied_volatility"] is None:
        return Envelope(
            status="FAILED",
            results=results,
            warnings=[
                {
                    "code": results["error"] or "IV_UNAVAILABLE",
                    "severity": "ERROR",
                    "message": "No implied volatility exists for this price.",
                    "context": {"price": payload.price, "strike": payload.strike},
                }
            ],
            provenance=envelope.provenance,
        )
    return envelope


@router.post("/greeks", response_model=Envelope)
async def greeks(
    payload: GreeksRequest,
    _user: CurrentUser,
    derivatives: DerivativesServiceDep,
    settings: SettingsDep,
) -> Envelope:
    """Price and Greeks for one contract. The response names its own units."""
    try:
        results = derivatives.price_and_greeks(
            strike=payload.strike,
            tau=payload.time_to_expiry,
            sigma=payload.sigma,
            is_call=payload.is_call,
            spot=payload.spot,
            forward=payload.forward,
            rate=payload.rate,
            dividend=payload.dividend_yield,
        )
    except AnalysisError as exc:
        raise UnprocessableEntity("INVALID_GREEKS_REQUEST", str(exc)) from exc
    return _calculator_envelope(results, settings, {"pricing": "black-scholes-merton@1.0.0"})


@router.post("/forward", response_model=Envelope)
async def forward(
    payload: ForwardRequest,
    _user: CurrentUser,
    derivatives: DerivativesServiceDep,
    settings: SettingsDep,
) -> Envelope:
    """Estimate a forward by every applicable method.

    All estimates are returned, not only the selected one: disagreement between
    them usually means bad data or an unstated carry, and averaging it away
    would destroy the signal.
    """
    try:
        results = derivatives.estimate_forward(
            tau=payload.time_to_expiry,
            spot=payload.spot,
            rate=payload.rate,
            dividend=payload.dividend_yield,
            dividend_assumed=payload.dividend_yield_assumed,
            strikes=payload.strikes,
            call_prices=payload.call_prices,
            put_prices=payload.put_prices,
            future_price=payload.future_price,
        )
    except AnalysisError as exc:
        raise UnprocessableEntity("INVALID_FORWARD_REQUEST", str(exc)) from exc

    envelope = _calculator_envelope(results, settings, {"forward": "forward-estimator@1.0.0"})
    if results["selected"] is None:
        return Envelope(
            status="FAILED",
            results=results,
            warnings=[
                {
                    "code": "NO_USABLE_FORWARD",
                    "severity": "ERROR",
                    "message": "No method produced a usable forward.",
                    "context": {},
                }
            ],
            provenance=envelope.provenance,
        )
    return envelope


# -------------------------------------------------------------- chain analysis
@router.post(
    "/chains/{snapshot_id}/analyze",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze_chain(
    snapshot_id: uuid.UUID,
    payload: AnalyzeChainRequest,
    user: CurrentUser,
    market_data: MarketDataServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Solve forwards, implied volatilities and raw smiles for a stored chain.

    A job, not a synchronous call: a full option-market scan solves hundreds of
    thousands of implied volatilities.
    """
    if await market_data.get_chain_snapshot(snapshot_id, user.id) is None:
        raise NotFound("Chain snapshot")

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.ANALYZE_OPTION_CHAIN,
        input_reference={
            "snapshot_id": str(snapshot_id),
            "risk_free_rate": payload.risk_free_rate,
            "dividend_yield": payload.dividend_yield,
            "dividend_yield_assumed": payload.dividend_yield_assumed,
            "settlement_time_utc": (
                payload.settlement_time_utc.isoformat() if payload.settlement_time_utc else None
            ),
            "day_count": str(payload.day_count),
            "include_excluded_quotes": payload.include_excluded_quotes,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.ANALYZE_OPTION_CHAIN),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/analyses", response_model=list[AnalysisSummaryOut])
async def list_analyses(
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AnalysisSummaryOut]:
    rows = await derivatives.list_analyses(user.id, limit=limit, offset=offset)
    return [
        AnalysisSummaryOut(
            analysis_id=row.id,
            snapshot_id=row.chain_snapshot_id,
            underlying_id=row.underlying_id,
            as_of_timestamp=row.as_of_timestamp,
            curve_id=row.curve_id,
            quotes_in=row.quotes_in,
            quotes_solved=row.quotes_solved,
            expiries=row.expiries,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def _load(derivatives, analysis_id, user_id, expiry, used_only, limit):
    row = await derivatives.get_analysis(analysis_id, user_id)
    if row is None:
        raise NotFound("Analysis")
    points = await derivatives.repository.get_implied_vols(
        analysis_id, expiry=expiry, used_for_smile_only=used_only, limit=limit
    )
    forwards = await derivatives.repository.get_forward_estimates(analysis_id)
    return row, points, forwards


def _point_out(row) -> ImpliedVolPointOut:
    envelope_width = (
        None
        if row.market_iv_bid is None or row.market_iv_ask is None
        else row.market_iv_ask - row.market_iv_bid
    )
    return ImpliedVolPointOut(
        instrument_id=row.instrument_id,
        expiry=row.expiry,
        strike=row.strike,
        option_type=row.option_type,
        price_used=row.price_used,
        price_source=row.price_source,
        market_iv=row.market_iv,
        market_iv_bid=row.market_iv_bid,
        market_iv_ask=row.market_iv_ask,
        iv_envelope_width=envelope_width,
        converged=row.converged,
        iterations=row.iterations,
        solver=row.solver,
        error=row.error,
        vega=row.vega,
        uncertainty=row.uncertainty,
        time_to_expiry=row.time_to_expiry,
        log_moneyness=row.log_moneyness,
        total_variance=row.total_variance,
        weight=row.weight,
        used_for_smile=row.used_for_smile,
        smile_exclusion=row.smile_exclusion,
    )


def _assemble(row, points, forwards, expiry_filter: date | None) -> dict:
    summary = dict(row.summary or {})
    by_expiry: dict[str, list] = {}
    for point in points:
        by_expiry.setdefault(point.expiry.isoformat(), []).append(_point_out(point))
    forwards_by_expiry: dict[str, list[ForwardEstimateOut]] = {}
    for estimate in forwards:
        forwards_by_expiry.setdefault(estimate.expiry.isoformat(), []).append(
            ForwardEstimateOut(
                method=estimate.method,
                selected=estimate.selected,
                value=estimate.value,
                confidence=estimate.confidence,
                observations=estimate.observations,
                residual_error=estimate.residual_error,
                discount_factor=estimate.discount_factor,
                error=estimate.error,
                assumptions=list(estimate.assumptions or []),
            )
        )

    slices = []
    for slice_summary in summary.get("slices", []):
        key = slice_summary["expiry"]
        if expiry_filter is not None and key != expiry_filter.isoformat():
            continue
        enriched = dict(slice_summary)
        enriched["points"] = [p.model_dump(mode="json") for p in by_expiry.get(key, [])]
        enriched["forward"] = {
            **slice_summary.get("forward", {}),
            "estimates": [e.model_dump(mode="json") for e in forwards_by_expiry.get(key, [])],
        }
        slices.append(enriched)

    summary["slices"] = slices
    summary["analysis_id"] = str(row.id)
    return summary


@router.get("/analyses/{analysis_id}", response_model=Envelope)
async def get_analysis(
    analysis_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    expiry: date | None = None,
    used_for_smile_only: bool = False,
    limit: int = Query(default=20000, ge=1, le=100000),
) -> Envelope:
    row, points, forwards = await _load(
        derivatives, analysis_id, user.id, expiry, used_for_smile_only, limit
    )
    return Envelope(
        status="OK" if row.quotes_solved > 0 else "PARTIAL",
        results=_assemble(row, points, forwards, expiry),
        warnings=[],
        provenance=ProvenanceOut(**(row.provenance or {})),
    )


@router.get("/chains/{snapshot_id}/smile", response_model=Envelope)
async def get_smile(
    snapshot_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    expiry: date | None = None,
    used_for_smile_only: bool = True,
    limit: int = Query(default=20000, ge=1, le=100000),
) -> Envelope:
    """The most recent analysis of a chain, as a smile."""
    row = await derivatives.latest_analysis(snapshot_id, user.id)
    if row is None:
        raise NotFound("Analysis")
    _row, points, forwards = await _load(
        derivatives, row.id, user.id, expiry, used_for_smile_only, limit
    )
    return Envelope(
        status="OK" if row.quotes_solved > 0 else "PARTIAL",
        results=_assemble(row, points, forwards, expiry),
        warnings=[],
        provenance=ProvenanceOut(**(row.provenance or {})),
    )


# ------------------------------------------------------------------ surfaces
@router.post(
    "/analyses/{analysis_id}/calibrate",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def calibrate_surface(
    analysis_id: uuid.UUID,
    payload: CalibrateSurfaceRequest,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Fit SVI per expiry and run the arbitrage diagnostics.

    A job: SLSQP with ten starts per expiry over a full option market is minutes
    of work, not milliseconds.
    """
    if await derivatives.get_analysis(analysis_id, user.id) is None:
        raise NotFound("Analysis")

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.CALIBRATE_SURFACE,
        input_reference={
            "analysis_id": str(analysis_id),
            "seed": payload.seed,
            "use_weights": payload.use_weights,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.CALIBRATE_SURFACE),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/surfaces", response_model=list[SurfaceSummaryOut])
async def list_surfaces(
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SurfaceSummaryOut]:
    rows = await derivatives.list_surfaces(user.id, limit=limit, offset=offset)
    return [
        SurfaceSummaryOut(
            surface_row_id=row.id,
            surface_id=row.surface_id,
            underlying_id=row.underlying_id,
            analysis_id=row.analysis_id,
            as_of_timestamp=row.as_of_timestamp,
            model=row.model,
            model_version=row.model_version,
            curve_id=row.curve_id,
            slices_total=row.slices_total,
            slices_fitted=row.slices_fitted,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _surface_payload(row, surface) -> dict:
    payload = surface.to_dict(include_slices=True)
    payload["surface_row_id"] = str(row.id)
    return payload


@router.get("/surfaces/latest", response_model=Envelope)
async def get_latest_surface(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
) -> Envelope:
    """The most recent surface for an underlying.

    Declared before ``/surfaces/{surface_id}`` so the literal path wins.
    """
    loaded = await derivatives.latest_surface(user.id, underlying_id)
    if loaded is None:
        raise NotFound("Surface")
    row, surface = loaded
    return Envelope(
        status="OK" if surface.fitted_slices else "PARTIAL",
        results=_surface_payload(row, surface),
        warnings=[],
        provenance=ProvenanceOut(**(row.provenance or {})),
    )


@router.get("/surfaces/{surface_row_id}", response_model=Envelope)
async def get_surface(
    surface_row_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
) -> Envelope:
    """Rebuilt from its persisted parameters, with no re-fitting on read."""
    loaded = await derivatives.load_surface(surface_row_id, user.id)
    if loaded is None:
        raise NotFound("Surface")
    row, surface = loaded
    return Envelope(
        status="OK" if surface.fitted_slices else "PARTIAL",
        results=_surface_payload(row, surface),
        warnings=[],
        provenance=ProvenanceOut(**(row.provenance or {})),
    )


@router.post("/surfaces/{surface_row_id}/reference", response_model=Envelope)
async def reference_values(
    surface_row_id: uuid.UUID,
    payload: ReferenceRequest,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
) -> Envelope:
    """Reference implied volatilities and prices from a stored surface.

    These are **model outputs**. They are called reference values, not fair
    values, they never overwrite an observed market IV, and each one carries
    flags saying whether it came from a fitted slice, an interpolation between
    two, or an extrapolation past the data.
    """
    loaded = await derivatives.load_surface(surface_row_id, user.id)
    if loaded is None:
        raise NotFound("Surface")
    row, surface = loaded

    requests = [
        (
            Decimal(item.strike),
            item.expiry,
            OptionType(item.option_type) if item.option_type else None,
        )
        for item in payload.requests
    ]
    points = [surface.reference(*request) for request in requests]

    unavailable = sum(1 for point in points if not point.ok)
    warnings = []
    if unavailable:
        warnings.append(
            {
                "code": "REFERENCE_UNAVAILABLE",
                "severity": "WARNING",
                "message": (
                    f"{unavailable} of {len(points)} requests could not be valued "
                    "from this surface."
                ),
                "context": {"unavailable": unavailable},
            }
        )
    extrapolated = sum(1 for point in points if any("EXTRAPOLATED" in str(f) for f in point.flags))
    if extrapolated:
        warnings.append(
            {
                "code": "REFERENCE_EXTRAPOLATED",
                "severity": "INFO",
                "message": (
                    f"{extrapolated} value(s) lie outside the fitted range. SVI's "
                    "wings are weakly constrained by a narrow strike window, so "
                    "treat these as indicative."
                ),
                "context": {"extrapolated": extrapolated},
            }
        )

    return Envelope(
        status="OK" if unavailable == 0 else "PARTIAL",
        results={
            "surface_id": surface.surface_id,
            "points": [point.to_dict() for point in points],
        },
        warnings=warnings,
        provenance=ProvenanceOut(**(row.provenance or {})),
    )


@router.get("/arbitrage/{analysis_id}", response_model=Envelope)
async def get_arbitrage(
    analysis_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    min_severity: str = Query(default="INFO", pattern="^(INFO|WARNING|ERROR)$"),
    limit: int = Query(default=2000, ge=1, le=20000),
) -> Envelope:
    """Raw-market and fitted-surface findings, separated by scope.

    A violation in observed quotes is almost always a data artefact — stale
    legs, non-simultaneous quotes, a wrong multiplier — not an executable
    opportunity, and the report says so rather than implying otherwise.
    """
    analysis = await derivatives.get_analysis(analysis_id, user.id)
    if analysis is None:
        raise NotFound("Analysis")

    reports = await derivatives.repository.get_arbitrage_reports(analysis_id)
    payload: dict = {"analysis_id": str(analysis_id), "raw_market": None, "fitted_surface": None}
    for report in reports:
        violations = await derivatives.repository.get_arbitrage_violations(
            report.id, min_severity=min_severity, limit=limit
        )
        block = {
            "scope": report.scope,
            "severity": report.severity,
            "violations_total": report.violations_total,
            "observations": report.observations,
            "checks_run": list(report.checks_run or []),
            "summary": report.summary or {},
            "violations": [
                {
                    "scope": v.scope,
                    "type": v.violation_type,
                    "severity": v.severity,
                    "magnitude": v.magnitude,
                    "tolerance": v.tolerance,
                    "expiry": v.expiry.isoformat() if v.expiry else None,
                    "strike": format(v.strike, "f") if v.strike is not None else None,
                    "option_type": v.option_type,
                    "detail": v.detail or {},
                    "affected_instruments": list(v.affected_instruments or []),
                }
                for v in violations
            ],
        }
        key = "raw_market" if report.scope == "RAW_MARKET" else "fitted_surface"
        payload[key] = block

    return Envelope(
        status="OK",
        results=payload,
        warnings=[],
        provenance=ProvenanceOut(**(analysis.provenance or {})),
    )


# ------------------------------------------------------------------ anomalies
@router.post(
    "/surfaces/{surface_row_id}/anomalies",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def scan_anomalies(
    surface_row_id: uuid.UUID,
    payload: ScanAnomaliesRequest,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Compare every observed implied volatility against the fitted surface.

    This endpoint produces **measurements and explanations, not advice**. There
    is no direction, no rating and no target: a deviation from a model is a
    statement about the model and the data, and the response says which.
    """
    if await derivatives.load_surface(surface_row_id, user.id) is None:
        raise NotFound("Surface")

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.SCAN_ANOMALIES,
        input_reference={
            "surface_row_id": str(surface_row_id),
            "min_z_score": payload.min_z_score,
            "require_outside_envelope": payload.require_outside_envelope,
            "min_confidence": payload.min_confidence,
            "min_liquidity": payload.min_liquidity,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.SCAN_ANOMALIES),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


def _anomaly_payload(scan_row, rows) -> dict:
    return {
        "scan_id": str(scan_row.id),
        "surface_id": str(scan_row.surface_id),
        "analysis_id": str(scan_row.analysis_id),
        "underlying_id": str(scan_row.underlying_id),
        "as_of_timestamp": scan_row.as_of_timestamp.isoformat(),
        "counts": {
            "examined": scan_row.quotes_examined,
            "scored": scan_row.quotes_scored,
            "flagged": scan_row.flagged,
            "returned": len(rows),
        },
        "policy": scan_row.policy or {},
        "anomalies": [
            {
                "instrument_id": str(row.instrument_id),
                "expiry": row.expiry.isoformat(),
                "strike": format(row.strike, "f"),
                "option_type": row.option_type,
                "market_iv": row.market_iv,
                "reference_iv": row.reference_iv,
                "iv_difference": row.iv_difference,
                "iv_difference_vol_points": row.iv_difference * 100.0,
                "relative_deviation": row.relative_deviation,
                "market_iv_bid": row.market_iv_bid,
                "market_iv_ask": row.market_iv_ask,
                "envelope_position": row.envelope_position,
                "excess_over_envelope": row.excess_over_envelope,
                "explained_scale": row.explained_scale,
                "z_score": row.z_score,
                "historical_z_score": row.historical_z_score,
                "historical_observations": row.historical_observations,
                "liquidity_score": row.liquidity_score,
                "data_quality_score": row.data_quality_score,
                "calibration_rmse_vol_points": row.calibration_rmse_vol_points,
                "iv_uncertainty": row.iv_uncertainty,
                "reference_method": row.reference_method,
                "reference_flags": list(row.reference_flags or []),
                "confidence": row.confidence,
                "flagged": row.flagged,
                "explanation": list(row.explanation or []),
            }
            for row in rows
        ],
    }


@router.get("/anomalies/{underlying_id}", response_model=Envelope)
async def get_latest_anomalies(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    flagged_only: bool = True,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=200, ge=1, le=5000),
) -> Envelope:
    """The most recent scan for an underlying."""
    scan_row = await derivatives.latest_anomaly_scan(user.id, underlying_id)
    if scan_row is None:
        raise NotFound("Anomaly scan")
    rows = await derivatives.repository.get_anomalies(
        scan_row.id,
        flagged_only=flagged_only,
        min_confidence=min_confidence,
        limit=limit,
    )
    return Envelope(
        status="OK",
        results=_anomaly_payload(scan_row, rows),
        warnings=[],
        provenance=ProvenanceOut(**(scan_row.provenance or {})),
    )


@router.get("/scans/{scan_id}", response_model=Envelope)
async def get_anomaly_scan(
    scan_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    flagged_only: bool = True,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=200, ge=1, le=5000),
) -> Envelope:
    scan_row = await derivatives.get_anomaly_scan(scan_id, user.id)
    if scan_row is None:
        raise NotFound("Anomaly scan")
    rows = await derivatives.repository.get_anomalies(
        scan_row.id,
        flagged_only=flagged_only,
        min_confidence=min_confidence,
        limit=limit,
    )
    return Envelope(
        status="OK",
        results=_anomaly_payload(scan_row, rows),
        warnings=[],
        provenance=ProvenanceOut(**(scan_row.provenance or {})),
    )


# -------------------------------------------------------------------- history
@router.get("/history/{underlying_id}", response_model=Envelope)
async def get_surface_history(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    settings: SettingsDep,
    tenor_days: int | None = None,
    include_series: bool = True,
    limit: int = Query(default=500, ge=1, le=5000),
) -> Envelope:
    """Percentiles of a surface's shape against its own history.

    Answers "is today's level unusual *for this underlying*?", which is more
    useful than "is it high?". Every answer carries its observation count, and
    below a stated minimum it is marked unreliable rather than presented as if a
    handful of surfaces were a distribution.
    """
    tenors = (
        [tenor_days]
        if tenor_days is not None
        else await derivatives.available_tenors(user.id, underlying_id)
    )
    if not tenors:
        raise NotFound("Surface history")

    histories = [
        await derivatives.tenor_history(user.id, underlying_id, tenor, limit=limit)
        for tenor in tenors
    ]
    histories = [history for history in histories if history.observations > 0]
    if not histories:
        raise NotFound("Surface history")

    warnings = []
    thin = [h for h in histories if not h.is_reliable]
    if thin:
        warnings.append(
            {
                "code": "HISTORY_INSUFFICIENT_OBSERVATIONS",
                "severity": "WARNING",
                "message": (
                    f"{len(thin)} of {len(histories)} tenors have fewer than "
                    f"{histories[0].to_dict(False)['minimum_reliable_observations']} "
                    "observations. Percentiles are reported but should not be read "
                    "as a distribution."
                ),
                "context": {"tenors": [h.tenor_days for h in thin]},
            }
        )

    provenance = Provenance.now(
        code_commit=settings.code_commit,
        model_versions={"history": "surface-history@1.0.0"},
        parameters={"underlying_id": str(underlying_id), "tenors": tenors},
    )
    return Envelope(
        status="OK",
        results={
            "underlying_id": str(underlying_id),
            "tenors": [h.to_dict(include_series=include_series) for h in histories],
        },
        warnings=warnings,
        provenance=ProvenanceOut(**provenance.to_dict()),
    )
