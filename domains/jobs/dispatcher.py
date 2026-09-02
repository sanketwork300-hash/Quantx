"""Job dispatch: enqueue to a worker, or run inline in eager mode."""

from __future__ import annotations

import uuid

from domains.jobs.runner import run_job
from infrastructure.settings import JobExecutionMode, Settings

CELERY_TASK_NAME = "qip.run_job"


async def submit_job(job_id: uuid.UUID, settings: Settings) -> None:
    if settings.job_execution_mode is JobExecutionMode.EAGER:
        # Inline execution for tests and single-process development. Production
        # startup refuses this mode (see Settings.validate_for_runtime).
        await run_job(job_id)
        return

    from infrastructure.queue.celery_app import celery_app

    celery_app.send_task(CELERY_TASK_NAME, args=[str(job_id)])
