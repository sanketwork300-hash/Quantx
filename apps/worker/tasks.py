"""Celery tasks.

Thin: a task looks a job up and hands it to the same runner the eager path
uses. All the logic lives in ``domains.jobs.runner``.
"""

from __future__ import annotations

import asyncio
import uuid

from domains.jobs.dispatcher import CELERY_TASK_NAME
from domains.jobs.runner import run_job
from infrastructure.observability.logging import configure_logging, set_correlation_id
from infrastructure.queue.celery_app import celery_app
from infrastructure.settings import get_settings

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)


@celery_app.task(name=CELERY_TASK_NAME, bind=True, max_retries=0)
def run_job_task(self, job_id: str, correlation_id: str | None = None) -> dict:
    if correlation_id:
        set_correlation_id(correlation_id)
    return asyncio.run(run_job(uuid.UUID(job_id)))
