"""Job execution.

One code path runs a job, whether it was dispatched to a Celery worker or run
inline in eager mode. An eager mode that took a different route through the code
would test nothing.
"""

from __future__ import annotations

import traceback
import uuid

from domains.jobs.handlers import get_handler
from domains.jobs.service import JobService
from infrastructure.database.session import session_scope
from infrastructure.observability.logging import get_logger

logger = get_logger(__name__)


async def run_job(job_id: uuid.UUID) -> dict:
    """Execute a persisted job, recording every state transition.

    The handler's own work and the job bookkeeping are deliberately in separate
    transactions: if the handler fails, the FAILED status and the error payload
    must still be written, which cannot happen inside the rolled-back
    transaction that produced the failure.
    """
    async with session_scope() as session:
        service = JobService(session)
        job = await service.get(job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        await service.mark_running(job_id)

    logger.info("job_started", job_id=str(job_id), job_type=str(job.job_type))

    try:
        async with session_scope() as session:
            handler = get_handler(job.job_type)
            result = await handler(session, job)
    except Exception as exc:
        logger.exception("job_failed", job_id=str(job_id), error=str(exc))
        async with session_scope() as session:
            await JobService(session).fail(
                job_id,
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=20),
                },
            )
        raise

    async with session_scope() as session:
        await JobService(session).complete(job_id, result)

    logger.info("job_completed", job_id=str(job_id))
    return result
