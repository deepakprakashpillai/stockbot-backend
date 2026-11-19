"""Requeue only expired Model 2 leases; preserve all original attempt evidence."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.jobs import enqueue_job
from app.db.models.backtests import BacktestRun
from app.db.models.jobs import JobDispatch
from app.db.models.model2 import ResearchScenarioGroup, ResearchScenarioMember
from app.db.repositories.backtests import BacktestRepository


def recover_expired(session: Session, limit: int = 20) -> int:
    candidates = list(
        session.scalars(
            select(BacktestRun.id)
            .where(
                BacktestRun.mode == "OPTION_PORTFOLIO_SIMULATION",
                BacktestRun.status == "RUNNING",
                BacktestRun.lease_expires_at < datetime.now(UTC),
            )
            .limit(limit)
        )
    )
    recovered = 0
    repository = BacktestRepository(session)
    for run_id in candidates:
        member = session.get(ResearchScenarioMember, run_id)
        if member is None:
            continue
        group = session.scalar(
            select(ResearchScenarioGroup)
            .where(ResearchScenarioGroup.id == member.group_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if group is None:
            continue
        run = session.scalar(
            select(BacktestRun)
            .where(BacktestRun.id == run_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if (
            run is None
            or run.status != "RUNNING"
            or run.lease_expires_at is None
            or run.lease_expires_at >= datetime.now(UTC)
        ):
            continue
        if run.cancellation_requested or group.cancellation_requested:
            run.status = "CANCELLED"
            run.completed_at = datetime.now(UTC)
            if run.active_attempt_id:
                repository.finish_attempt(
                    run.active_attempt_id,
                    "CANCELLED",
                    "Expired worker after cancellation; open evidence retained without an exit.",
                )
            group.status = "CANCELLED"
            group.completed_at = datetime.now(UTC)
            for sibling in session.scalars(
                select(BacktestRun)
                .join(ResearchScenarioMember, ResearchScenarioMember.run_id == BacktestRun.id)
                .where(ResearchScenarioMember.group_id == group.id, BacktestRun.status == "PENDING")
            ):
                sibling.status = "CANCELLED"
                sibling.completed_at = datetime.now(UTC)
            repository.append_event(
                run_id,
                "BACKTEST_CANCELLED",
                {
                    "mode": run.mode,
                    "reason": "EXPIRED_CANCELLED_WORKER",
                    "open_evidence_retained": True,
                },
            )
        else:
            run.status = "PENDING"
            run.progress = 0
            job = session.scalar(
                select(JobDispatch)
                .where(JobDispatch.kind == "BACKTEST", JobDispatch.resource_id == run_id)
                .with_for_update()
            )
            if job is None:
                enqueue_job(session, "BACKTEST", run_id)
            else:
                job.status = "PENDING"
                job.delivered_at = None
            repository.append_event(
                run_id, "BACKTEST_ADMIN_REQUEUED", {"reason": "expired_model2_worker_lease"}
            )
        run.lease_owner = None
        run.lease_expires_at = None
        recovered += 1
    session.flush()
    return recovered
