"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.errors import register_exception_handlers
from api.routes import (
    advanced,
    auth,
    derivatives,
    execution,
    health,
    instruments,
    jobs,
    market,
    microstructure,
    portfolio,
    risk,
    uploads,
)
from infrastructure.database.session import dispose_engine
from infrastructure.observability.logging import (
    configure_logging,
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from infrastructure.settings import Settings, get_settings

API_PREFIX = "/api/v1"
CORRELATION_HEADER = "X-Correlation-Id"

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "api_starting",
        version=settings.app_version,
        environment=str(settings.env),
        job_execution_mode=str(settings.job_execution_mode),
    )
    yield
    await dispose_engine()
    logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    settings.validate_for_runtime()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Derivatives valuation, portfolio risk and execution intelligence.\n\n"
            "This is an analytics and research platform. It produces reference "
            "values, estimated margin, estimated slippage and counterfactual "
            "simulations with stated model confidence. It does not produce trade "
            "recommendations, fair values, guaranteed liquidation levels or "
            "broker-equivalent margin."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER],
    )

    @app.middleware("http")
    async def correlation_and_timing(request: Request, call_next):
        correlation_id = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
        token = set_correlation_id(correlation_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[CORRELATION_HEADER] = correlation_id
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(elapsed_ms, 2),
            correlation_id=correlation_id,
        )
        return response

    register_exception_handlers(app)

    for router in (
        health.router,
        auth.router,
        instruments.router,
        market.router,
        derivatives.router,
        advanced.router,
        uploads.router,
        portfolio.router,
        risk.router,
        execution.router,
        microstructure.router,
        jobs.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    return app


app = create_app()
