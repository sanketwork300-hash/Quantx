"""Phase 9 job handlers.

Both of these are jobs for the same reason: they are seconds of numerical work,
not milliseconds. An SSVI fit runs ten SLSQP starts over every expiry at once
with a Durrleman constraint evaluated on a grid per slice, a Heston fit prices
the whole surface by quadrature on every iteration, and the consensus runs a
Crank-Nicolson PDE and a hundred thousand simulated paths for one contract.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from domains.derivatives.advanced import (
    AdvancedDerivativesService,
    CalibrateGlobalSurfaceParams,
    PriceConsensusParams,
)
from domains.derivatives.consensus import PricingModelKind
from domains.jobs.handlers import register
from domains.jobs.models import Job, JobType
from domains.market_data.service import MarketDataService
from infrastructure.settings import get_settings
from infrastructure.storage.factory import get_object_store


async def calibrate_global_surface(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()
    service = AdvancedDerivativesService(session, settings)

    params = CalibrateGlobalSurfaceParams(
        seed=int(payload.get("seed") or 20_260_924),
        use_weights=bool(payload.get("use_weights", True)),
        enforce_butterfly_bounds=bool(payload.get("enforce_butterfly_bounds", True)),
        calibrate_heston=bool(payload.get("calibrate_heston", True)),
        require_feller=bool(payload.get("require_feller", False)),
        build_local_volatility=bool(payload.get("build_local_volatility", True)),
        build_densities=bool(payload.get("build_densities", True)),
    )
    result, row_id = await service.calibrate_global_surface(
        job.user_id, uuid.UUID(payload["analysis_id"]), params
    )
    out = result.to_dict()
    if out.get("results") is not None:
        out["results"]["global_surface_row_id"] = str(row_id)
    return out


async def price_consensus(session: AsyncSession, job: Job) -> dict:
    payload = job.input_reference
    settings = get_settings()
    service = AdvancedDerivativesService(session, settings)
    market_data = MarketDataService(session, settings, get_object_store(settings))

    params = PriceConsensusParams(
        instrument_id=uuid.UUID(payload["instrument_id"]),
        models=tuple(PricingModelKind(name) for name in payload.get("models") or ()),
        risk_free_rate=float(payload.get("risk_free_rate") or 0.0),
        dividend_yield=float(payload.get("dividend_yield") or 0.0),
        paths=int(payload.get("paths") or 100_000),
        seed=int(payload.get("seed") or 20_260_924),
        grid_nodes=int(payload.get("grid_nodes") or 401),
        grid_steps=int(payload.get("grid_steps") or 200),
        global_surface_row_id=(
            uuid.UUID(payload["global_surface_row_id"])
            if payload.get("global_surface_row_id")
            else None
        ),
    )
    result, row_id = await service.price_consensus(job.user_id, params, market_data)
    out = result.to_dict()
    if out.get("results") is not None:
        out["results"]["consensus_row_id"] = str(row_id)
    return out


def register_handlers() -> None:
    register(JobType.CALIBRATE_GLOBAL_SURFACE, calibrate_global_surface)
    register(JobType.PRICE_CONSENSUS, price_consensus)
