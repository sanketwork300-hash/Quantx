"""Celery beat schedule.

Holds no business logic: it only decides *when* work is enqueued. Periodic
tasks are added here as later phases introduce them (surface recalibration,
snapshot rollups, data-quality sweeps).
"""

from __future__ import annotations

from infrastructure.queue.celery_app import celery_app

celery_app.conf.beat_schedule = {}

__all__ = ["celery_app"]
