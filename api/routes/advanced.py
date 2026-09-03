"""Phase 9 routes: global surface, local volatility, density, model consensus.

Thin: parameter validation, ownership, and translation between the HTTP schema
and the domain service. No financial mathematics (enforced by
``scripts/check_layering.py``).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from api.dependencies.core import (
    AdvancedDerivativesDep,
    CurrentUser,
    DerivativesServiceDep,
    InstrumentServiceDep,
    JobServiceDep,
    SessionDep,
    SettingsDep,
)
from api.errors import NotFound, UnprocessableEntity
from api.schemas.advanced import (
    CalibrateGlobalSurfaceRequest,
    ConfidenceContributionOut,
    DensityOut,
    GlobalSurfaceSummaryOut,
    HestonCalibrationOut,
    LocalVolatilitySummaryOut,
    ModelConsensusOut,
    ModelDispersionOut,
    ModelValueOut,
    PriceConsensusRequest,
    SSVIParametersOut,
)
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.uploads import JobAcceptedOut
from domains.derivatives.consensus import PRICING_MODELS, PricingModelKind
from domains.instruments.enums import AssetClass
from domains.jobs.dispatcher import submit_job
from domains.jobs.models import JobStatus, JobType
from domains.users.models import AuditAction
from domains.users.service import UserService

router = APIRouter(prefix="/derivatives", tags=["advanced derivatives"])

STORED_PERCENTILE_KEYS = ("0.05", "0.25", "0.5", "0.75", "0.95")


def _summary(row) -> GlobalSurfaceSummaryOut:
    return GlobalSurfaceSummaryOut(
        global_surface_row_id=row.id,
        surface_id=row.surface_id,
        underlying_id=row.underlying_id,
        analysis_id=row.analysis_id,
        as_of_timestamp=row.as_of_timestamp,
        model=row.model,
        model_version=row.model_version,
        status=row.status,
        curve_id=row.curve_id,
        parameters=(
            SSVIParametersOut(rho=row.rho, eta=row.eta, gamma=row.gamma)
            if row.rho is not None
            else None
        ),
        n_slices=row.n_slices,
        n_observations=row.n_observations,
        rmse_vol_points=row.rmse_vol_points,
        min_durrleman_g=row.min_durrleman_g,
        max_butterfly_quantity=row.max_butterfly_quantity,
        butterfly_bounds_satisfied=row.butterfly_bounds_satisfied,
        calendar_arbitrage_free=row.calendar_arbitrage_free,
        created_at=row.created_at,
    )


# ------------------------------------------------------------ global surfaces
@router.post(
    "/analyses/{analysis_id}/global-surface",
    response_model=JobAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def calibrate_global_surface(
    analysis_id: uuid.UUID,
    payload: CalibrateGlobalSurfaceRequest,
    user: CurrentUser,
    derivatives: DerivativesServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Fit SSVI to every expiry at once, then derive what follows from it.

    One job produces the global surface, the Dupire local-volatility grid, the
    implied density per expiry and a constrained Heston fit, because all four
    read the same analysis and running them separately would let the four
    disagree about which quotes they were built from.
    """
    if await derivatives.get_analysis(analysis_id, user.id) is None:
        raise NotFound("Analysis")

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.CALIBRATE_GLOBAL_SURFACE,
        input_reference={
            "analysis_id": str(analysis_id),
            "seed": payload.seed,
            "use_weights": payload.use_weights,
            "enforce_butterfly_bounds": payload.enforce_butterfly_bounds,
            "calibrate_heston": payload.calibrate_heston,
            "require_feller": payload.require_feller,
            "build_local_volatility": payload.build_local_volatility,
            "build_densities": payload.build_densities,
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.CALIBRATE_GLOBAL_SURFACE),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/global-surfaces", response_model=list[GlobalSurfaceSummaryOut])
async def list_global_surfaces(
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[GlobalSurfaceSummaryOut]:
    rows = await advanced.list_global_surfaces(user.id, limit=limit, offset=offset)
    return [_summary(row) for row in rows]


@router.get("/global-surfaces/latest", response_model=Envelope)
async def latest_global_surface(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
) -> Envelope:
    """Declared before ``/global-surfaces/{id}`` so the literal path wins."""
    loaded = await advanced.latest_global_surface(user.id, underlying_id)
    if loaded is None:
        raise NotFound("Global surface")
    return _surface_envelope(*loaded)


@router.get("/global-surfaces/{global_surface_row_id}", response_model=Envelope)
async def get_global_surface(
    global_surface_row_id: uuid.UUID,
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
) -> Envelope:
    """Rebuilt from three parameters and one theta per expiry, never re-fitted."""
    loaded = await advanced.load_global_surface(global_surface_row_id, user.id)
    if loaded is None:
        raise NotFound("Global surface")
    return _surface_envelope(*loaded)


def _surface_envelope(row, surface) -> Envelope:
    payload = surface.to_dict(include_slices=True)
    payload["global_surface_row_id"] = str(row.id)
    payload["status"] = row.status
    payload["diagnostics"] = {
        "n_observations": row.n_observations,
        "n_slices": row.n_slices,
        "rmse_total_variance": row.rmse_total_variance,
        "rmse_vol_points": row.rmse_vol_points,
        "max_error_vol_points": row.max_error_vol_points,
        "min_durrleman_g": row.min_durrleman_g,
        "max_butterfly_quantity": row.max_butterfly_quantity,
        "butterfly_bounds_satisfied": row.butterfly_bounds_satisfied,
        "calendar_arbitrage_free": row.calendar_arbitrage_free,
        "starts_attempted": row.starts_attempted,
        "starts_feasible": row.starts_feasible,
        "optimizer": row.optimizer,
        "optimizer_message": row.optimizer_message,
        "error": row.error,
    }
    return Envelope(
        status="OK" if surface.usable else "PARTIAL",
        results=payload,
        warnings=[],
        provenance=ProvenanceOut(**(row.provenance or {})),
    )


@router.get(
    "/global-surfaces/{global_surface_row_id}/local-volatility",
    response_model=LocalVolatilitySummaryOut,
)
async def local_volatility(
    global_surface_row_id: uuid.UUID,
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
) -> LocalVolatilitySummaryOut:
    """The Dupire grid, with its holes intact.

    ``values`` carries ``null`` wherever the formula produced nothing, so a
    plotting layer cannot draw a line through a region the surface never
    described.
    """
    row = await advanced.local_volatility(global_surface_row_id, user.id)
    if row is None:
        raise NotFound("Local volatility surface")
    grid = row.grid or {}
    return LocalVolatilitySummaryOut(
        local_volatility_row_id=row.id,
        global_surface_row_id=row.global_surface_id,
        as_of_timestamp=row.as_of_timestamp,
        model_version=row.model_version,
        spot=row.spot,
        carry=row.carry,
        total_points=row.total_points,
        valid_points=row.valid_points,
        flagged_points=row.flagged_points,
        coverage=row.coverage,
        flag_counts=row.flag_counts or {},
        log_moneyness=grid.get("log_moneyness", []),
        maturities=grid.get("maturities", []),
        values=grid.get("values", []),
    )


@router.get("/global-surfaces/{global_surface_row_id}/densities", response_model=list[DensityOut])
async def densities(
    global_surface_row_id: uuid.UUID,
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
) -> list[DensityOut]:
    """Breeden-Litzenberger densities, one per fitted expiry."""
    rows = await advanced.densities(global_surface_row_id, user.id)
    if rows is None:
        raise NotFound("Global surface")
    return [
        DensityOut(
            density_row_id=row.id,
            expiry=row.expiry,
            time_to_expiry=row.time_to_expiry,
            forward=row.forward,
            discount_factor=row.discount_factor,
            total_mass=row.total_mass,
            implied_mean=row.implied_mean,
            negative_mass=row.negative_mass,
            mean_error=row.mean_error,
            is_admissible=row.is_admissible,
            flags=list(row.flags or []),
            percentiles=dict(
                zip(
                    STORED_PERCENTILE_KEYS,
                    (
                        row.percentile_5,
                        row.percentile_25,
                        row.percentile_50,
                        row.percentile_75,
                        row.percentile_95,
                    ),
                    strict=True,
                )
            ),
            strikes=list(row.strikes or []),
            density=list(row.density or []),
        )
        for row in rows
    ]


@router.get("/underlyings/{underlying_id}/heston", response_model=HestonCalibrationOut)
async def latest_heston(
    underlying_id: uuid.UUID,
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
) -> HestonCalibrationOut:
    row = await advanced.repository.latest_heston_calibration(user.id, underlying_id)
    if row is None:
        raise NotFound("Heston calibration")
    return HestonCalibrationOut(
        heston_calibration_row_id=row.id,
        as_of_timestamp=row.as_of_timestamp,
        model_version=row.model_version,
        status=row.status,
        v0=row.v0,
        kappa=row.kappa,
        theta=row.theta,
        xi=row.xi,
        rho=row.rho,
        n_observations=row.n_observations,
        n_maturities=row.n_maturities,
        rmse_vol_points=row.rmse_vol_points,
        max_error_vol_points=row.max_error_vol_points,
        feller=row.feller,
        satisfies_feller=row.satisfies_feller,
        feller_enforced=row.feller_enforced,
        warnings=list(row.warnings or []),
        error=row.error,
    )


# ----------------------------------------------------------------- consensus
@router.post("/consensus", response_model=JobAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
async def price_consensus(
    payload: PriceConsensusRequest,
    user: CurrentUser,
    instruments: InstrumentServiceDep,
    jobs: JobServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> JobAcceptedOut:
    """Price one contract with every model and report how far apart they landed.

    A job: a Crank-Nicolson PDE and a hundred thousand simulated paths are
    seconds of work, and validating the contract here rather than inside the
    worker means an unknown instrument is a 404 instead of a failed job.
    """
    instrument = await instruments.get(payload.instrument_id)
    if instrument is None:
        raise NotFound("Instrument")
    if instrument.asset_class is not AssetClass.OPTION:
        raise UnprocessableEntity(
            "INSTRUMENT_NOT_AN_OPTION",
            "Model consensus prices vanilla options. "
            f"{instrument.symbol} is a {instrument.asset_class}.",
        )

    unknown = [name for name in payload.models if name not in {str(k) for k in PRICING_MODELS}]
    if unknown:
        raise UnprocessableEntity(
            "UNKNOWN_PRICING_MODEL",
            f"Unknown pricing model(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(str(kind) for kind in PRICING_MODELS))}.",
        )

    job = await jobs.create(
        user_id=user.id,
        job_type=JobType.PRICE_CONSENSUS,
        input_reference={
            "instrument_id": str(payload.instrument_id),
            "models": [str(PricingModelKind(name)) for name in payload.models],
            "risk_free_rate": payload.risk_free_rate,
            "dividend_yield": payload.dividend_yield,
            "paths": payload.paths,
            "seed": payload.seed,
            "grid_nodes": payload.grid_nodes,
            "grid_steps": payload.grid_steps,
            "global_surface_row_id": (
                str(payload.global_surface_row_id) if payload.global_surface_row_id else None
            ),
        },
    )
    await UserService(session).audit(
        AuditAction.JOB_SUBMITTED,
        user_id=user.id,
        resource_type="job",
        resource_id=str(job.id),
        job_type=str(JobType.PRICE_CONSENSUS),
    )
    await session.commit()

    await submit_job(job.id, settings)
    return JobAcceptedOut(job_id=job.id, status=str(JobStatus.QUEUED))


@router.get("/consensus", response_model=list[ModelConsensusOut])
async def list_consensus(
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ModelConsensusOut]:
    rows = await advanced.list_consensus_runs(user.id, limit=limit, offset=offset)
    return [_consensus_out(row, await advanced.repository.get_model_values(row.id)) for row in rows]


@router.get("/consensus/{consensus_row_id}", response_model=ModelConsensusOut)
async def get_consensus(
    consensus_row_id: uuid.UUID,
    user: CurrentUser,
    advanced: AdvancedDerivativesDep,
) -> ModelConsensusOut:
    loaded = await advanced.get_consensus(consensus_row_id, user.id)
    if loaded is None:
        raise NotFound("Consensus")
    return _consensus_out(*loaded)


def _consensus_out(row, values) -> ModelConsensusOut:
    return ModelConsensusOut(
        consensus_row_id=row.id,
        global_surface_row_id=row.global_surface_id,
        instrument_id=row.instrument_id,
        expiry=row.expiry,
        strike=row.strike,
        option_type=row.option_type,
        as_of_timestamp=row.as_of_timestamp,
        model_version=row.model_version,
        spot=row.spot,
        time_to_expiry=row.time_to_expiry,
        risk_free_rate=row.risk_free_rate,
        dividend_yield=row.dividend_yield,
        reference_volatility=row.reference_volatility,
        models_requested=row.models_requested,
        models_available=row.models_available,
        reference_value=row.reference_value,
        reference_range=(
            [row.reference_low, row.reference_high] if row.reference_low is not None else None
        ),
        model_dispersion=ModelDispersionOut(
            absolute=row.dispersion_absolute,
            relative=row.dispersion_relative,
            standard_deviation=row.standard_deviation,
        ),
        market_price=row.market_price,
        market_deviation=row.market_deviation,
        market_deviation_relative=row.market_deviation_relative,
        confidence=row.confidence,
        confidence_contributions=[
            ConfidenceContributionOut(**item) for item in (row.confidence_contributions or [])
        ],
        vanna=row.vanna,
        volga=row.volga,
        charm_per_day=row.charm_per_day,
        values=[
            ModelValueOut(
                model=value.model,
                model_version=value.model_version,
                value=value.value,
                unavailable_reason=value.unavailable_reason,
                method=value.method,
                inputs_used=value.inputs_used or {},
                diagnostics=value.diagnostics or {},
                warnings=list(value.warnings or []),
            )
            for value in values
        ],
        created_at=row.created_at,
    )
