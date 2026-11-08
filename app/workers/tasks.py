from __future__ import annotations

import logging
from time import monotonic

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="stockbot.diagnostics.ping")
def diagnostic_ping(marker: str) -> dict[str, str]:
    """A side-effect-free task used to prove worker delivery."""

    return {"status": "ok", "marker": marker}


@celery_app.task(name="stockbot.datasets.validate")
def validate_dataset_version(version_id: str) -> dict[str, str]:
    """Validate a persisted dataset version; redelivery is safe after terminal status."""

    from uuid import UUID

    from app.application.datasets.service import DatasetService
    from app.core.config import get_settings
    from app.db.session import create_session_factory
    from app.infrastructure.datasets.storage import LocalDatasetStorage

    settings = get_settings()
    factory = create_session_factory(settings)
    session = factory()
    started = monotonic()
    task_id = getattr(validate_dataset_version.request, "id", None)
    try:
        version = DatasetService(
            session, LocalDatasetStorage(settings.storage_root), settings
        ).validate_version(UUID(version_id))
        result = {
            "status": version.status if version else "MISSING",
            "dataset_version_id": version_id,
        }
        logger.info(
            "dataset validation task completed",
            extra={
                "task_id": task_id,
                "dataset_version_id": version_id,
                "duration_ms": round((monotonic() - started) * 1000, 3),
            },
        )
        return result
    except Exception:
        logger.exception(
            "dataset validation task failed",
            extra={"task_id": task_id, "dataset_version_id": version_id},
        )
        raise
    finally:
        session.close()


@celery_app.task(name="stockbot.backtests.run")
def run_backtest(run_id: str) -> dict[str, object]:
    """Run one persisted snapshot; duplicate delivery is guarded by the database lease."""

    from uuid import UUID

    from app.application.backtests import BacktestService
    from app.core.config import get_settings
    from app.db.session import create_session_factory
    from app.infrastructure.datasets.storage import LocalDatasetStorage
    from app.infrastructure.realtime import RedisRunEventPublisher

    settings = get_settings()
    session = create_session_factory(settings)()
    publisher = RedisRunEventPublisher(settings)
    started = monotonic()
    task_id = getattr(run_backtest.request, "id", None)
    try:
        result = BacktestService(
            session,
            LocalDatasetStorage(settings.storage_root),
            settings,
            publisher,
        ).execute(UUID(run_id))
        logger.info(
            "backtest task completed",
            extra={
                "task_id": task_id,
                "run_id": run_id,
                "duration_ms": round((monotonic() - started) * 1000, 3),
            },
        )
        return result
    except Exception:
        logger.exception("backtest task failed", extra={"task_id": task_id, "run_id": run_id})
        raise
    finally:
        session.close()
        publisher.close()


@celery_app.task(name="stockbot.backtests.export")
def build_backtest_export(job_id: str) -> dict[str, object]:
    """Build one complete evidence archive outside the request cycle."""

    from uuid import UUID

    from app.application.exports import BacktestExportService
    from app.core.config import get_settings
    from app.db.session import create_session_factory

    settings = get_settings()
    session = create_session_factory(settings)()
    try:
        job = BacktestExportService(session, settings).build(UUID(job_id))
        return {"export_job_id": str(job.id), "status": job.status}
    finally:
        session.close()
