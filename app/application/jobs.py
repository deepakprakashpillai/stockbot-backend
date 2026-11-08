"""Reliable delivery of committed jobs; task payloads contain only resource IDs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models.backtests import BacktestExportJob, BacktestRun
from app.db.models.jobs import JobDispatch
from app.db.session import create_session_factory

logger = logging.getLogger(__name__)


def enqueue_job(
    session: Session, kind: str, resource_id: UUID, depends_on_run_id: UUID | None = None
) -> JobDispatch:
    intent = JobDispatch(
        id=uuid4(), kind=kind, resource_id=resource_id, depends_on_run_id=depends_on_run_id
    )
    session.add(intent)
    return intent


def dispatch_pending_jobs(
    settings: Settings, limit: int = 20, *, session_factory: Callable[[], Session] | None = None
) -> int:
    """A failed publish leaves PENDING intent; a crash may redeliver, never erase intent."""
    from app.workers.celery_app import celery_app

    task_names = {
        "BACKTEST": "stockbot.backtests.run",
        "DATASET_VALIDATION": "stockbot.datasets.validate",
        "EXPORT": "stockbot.backtests.export",
    }
    delivered = 0
    factory = session_factory or create_session_factory(settings)
    with factory() as session:
        interrupted_exports = list(
            session.scalars(
                select(BacktestExportJob)
                .where(
                    BacktestExportJob.status == "RUNNING",
                    BacktestExportJob.lease_expires_at < datetime.now(UTC),
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for export in interrupted_exports:
            export.status = "PENDING"
            export.lease_owner = None
            export.lease_expires_at = None
            intent = session.scalar(
                select(JobDispatch)
                .where(JobDispatch.kind == "EXPORT", JobDispatch.resource_id == export.id)
                .with_for_update()
            )
            if intent:
                intent.status = "PENDING"
            else:
                enqueue_job(session, "EXPORT", export.id)
        session.flush()
        jobs = list(
            session.scalars(
                select(JobDispatch)
                .outerjoin(BacktestRun, BacktestRun.id == JobDispatch.depends_on_run_id)
                .where(
                    JobDispatch.status == "PENDING",
                    or_(
                        JobDispatch.depends_on_run_id.is_(None),
                        BacktestRun.id.is_(None),
                        BacktestRun.status.in_(["COMPLETED", "FAILED", "CANCELLED"]),
                    ),
                )
                .order_by(JobDispatch.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True, of=JobDispatch)
            )
        )
        for job in jobs:
            if job.depends_on_run_id:
                previous = session.get(BacktestRun, job.depends_on_run_id)
                if previous is None or previous.status in {"FAILED", "CANCELLED"}:
                    job.status = "CANCELLED"
                    run = session.get(BacktestRun, job.resource_id)
                    if run and run.status == "PENDING":
                        run.status = "CANCELLED"
                        run.completed_at = datetime.now(UTC)
                        run.error_code = "EXPERIMENT_PREDECESSOR_FAILED"
                    continue
                if previous.status != "COMPLETED":
                    continue
            if job.kind == "BACKTEST":
                run = session.get(BacktestRun, job.resource_id)
                if run is None or run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    job.status = "CANCELLED"
                    continue
            job.attempts += 1
            try:
                celery_app.send_task(
                    task_names[job.kind],
                    args=[str(job.resource_id)],
                    task_id=str(job.id),
                    retry=False,
                )
            except Exception:
                logger.warning(
                    "job delivery deferred", extra={"job_id": str(job.id)}, exc_info=True
                )
            else:
                job.status = "DELIVERED"
                job.delivered_at = datetime.now(UTC)
                delivered += 1
        session.commit()
    return delivered
