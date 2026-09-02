"""Job handler registry.

Handlers are looked up lazily so that the jobs domain does not import every
other domain at module load, which would create cycles as engines are added.
The registry is the contract: a job type with no handler is a configuration
error surfaced as a failed job, not a silent no-op.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from domains.jobs.models import Job, JobType

JobHandler = Callable[[AsyncSession, Job], Awaitable[dict]]

_REGISTRY: dict[JobType, JobHandler] = {}


class UnknownJobType(Exception):
    pass


def register(job_type: JobType, handler: JobHandler) -> None:
    _REGISTRY[job_type] = handler


def get_handler(job_type: JobType) -> JobHandler:
    if job_type not in _REGISTRY:
        _load_builtin_handlers()
    handler = _REGISTRY.get(job_type)
    if handler is None:
        raise UnknownJobType(f"no handler registered for job type {job_type}")
    return handler


def _load_builtin_handlers() -> None:
    # Imported here rather than at module scope to keep the jobs domain free of
    # compile-time dependencies on the domains it executes work for.
    from domains.derivatives.jobs import register_handlers as register_derivatives
    from domains.derivatives.surface_jobs import register_handlers as register_surface
    from domains.market_data.jobs import register_handlers as register_market_data

    register_market_data()
    register_derivatives()
    register_surface()
