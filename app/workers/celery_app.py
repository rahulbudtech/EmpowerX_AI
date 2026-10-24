"""
Celery application (broker = Redis).

Run worker (Windows-friendly):
  celery -A app.workers.celery_app.celery_app worker --loglevel=INFO --pool=solo

Run worker (Linux / prod):
  celery -A app.workers.celery_app.celery_app worker --loglevel=INFO --concurrency=2 -Q radiology,celery
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

_settings = get_settings()

celery_app = Celery("empowerx")

celery_app.conf.update(
    broker_url=_settings.resolved_celery_broker_url,
    result_backend=_settings.resolved_celery_result_backend,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Explicit imports so worker registers modality tasks without autodiscover magic.
    imports=("app.workers.tasks.radiology_tasks",),
    # Route radiology work to a dedicated queue (scale workers per queue later).
    task_routes={
        "radiology.analyze_dicom": {"queue": "radiology"},
    },
    task_default_queue="celery",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # Broker connection resilience
    broker_connection_retry_on_startup=True,
)

# Alias used by some deploy scripts: `celery -A app.workers.celery_app`
app = celery_app
