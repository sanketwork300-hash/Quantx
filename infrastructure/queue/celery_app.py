"""Celery application.

``QIP_JOB_EXECUTION_MODE=eager`` sets ``task_always_eager`` so the exact same
task function runs inline. Tests and laptop development therefore exercise the
production code path with no broker running, which is the whole point: an eager
mode that took a different route through the code would test nothing.
"""

from __future__ import annotations

from celery import Celery

from infrastructure.settings import JobExecutionMode, get_settings


def build_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "qip",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["apps.worker.tasks"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_always_eager=settings.job_execution_mode is JobExecutionMode.EAGER,
        task_eager_propagates=True,
        broker_connection_retry_on_startup=True,
    )
    return app


celery_app = build_celery_app()
