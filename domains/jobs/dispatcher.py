"""Job dispatch: enqueue to a worker, or run inline in eager mode."""

from __future__ import annotations

import contextlib
import uuid

from domains.jobs.runner import run_job
from infrastructure.settings import JobExecutionMode, Settings

CELERY_TASK_NAME = "qip.run_job"


async def submit_job(job_id: uuid.UUID, settings: Settings) -> None:
    if settings.job_execution_mode is JobExecutionMode.EAGER:
        # Inline execution for tests and single-process development. Production
        # startup refuses this mode (see Settings.validate_for_runtime).
        #
        # A handler that raises is *not* re-raised into the caller. In queue
        # mode the exception reaches a worker and the submitting request has
        # long since returned its 202; eager mode has to behave the same way, or
        # a failing job turns a submission into a 500 and the client never
        # learns the job id it would use to read the failure. ``run_job`` has
        # already recorded FAILED with the traceback, and the job row is the
        # authoritative record of what happened either way.
        with contextlib.suppress(Exception):
            await run_job(job_id)
        return

    from infrastructure.queue.celery_app import celery_app

    celery_app.send_task(CELERY_TASK_NAME, args=[str(job_id)])
