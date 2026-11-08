from __future__ import annotations

from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level)

celery_app = Celery(
    "stockbot",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    task_default_queue="stockbot",
    task_track_started=True,
    task_acks_late=True,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=settings.dependency_timeout_seconds,
    task_publish_retry=False,
    worker_hijack_root_logger=False,
)
