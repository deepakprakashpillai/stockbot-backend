#!/usr/bin/env python3
"""Inspect or safely requeue backtests whose worker lease is no longer valid."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.application.jobs import dispatch_pending_jobs, enqueue_job  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models.backtests import BacktestRun  # noqa: E402
from app.db.models.jobs import JobDispatch  # noqa: E402
from app.db.repositories.backtests import BacktestRepository  # noqa: E402
from app.db.session import create_session_factory  # noqa: E402

CONFIRMATION = "RECOVER_STALLED_RUNS"


@dataclass(frozen=True)
class Candidate:
    run_id: str
    status: str
    reason: str
    progress: int
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str


def classify_stall(run: BacktestRun, *, now: datetime, pending_before: datetime) -> str | None:
    """Return a stable operator-facing reason, or None when recovery is unsafe."""

    if run.cancellation_requested:
        return None
    if run.status == "RUNNING" and run.lease_expires_at and run.lease_expires_at <= now:
        return "expired_worker_lease"
    if run.status == "PENDING" and run.created_at <= pending_before and run.lease_owner is None:
        return "stale_pending_delivery"
    return None


def find_candidates(
    session: Session,
    *,
    now: datetime,
    pending_age: timedelta,
    run_id: UUID | None = None,
) -> list[tuple[BacktestRun, str]]:
    pending_before = now - pending_age
    statement = select(BacktestRun).where(
        or_(BacktestRun.status == "RUNNING", BacktestRun.status == "PENDING")
    )
    if run_id is not None:
        statement = statement.where(BacktestRun.id == run_id)
    statement = statement.order_by(BacktestRun.created_at, BacktestRun.id)
    candidates = []
    for run in session.scalars(statement):
        reason = classify_stall(run, now=now, pending_before=pending_before)
        if reason:
            candidates.append((run, reason))
    return candidates


def describe(run: BacktestRun, reason: str) -> Candidate:
    return Candidate(
        run_id=str(run.id),
        status=run.status,
        reason=reason,
        progress=run.progress,
        lease_owner=run.lease_owner,
        lease_expires_at=run.lease_expires_at.isoformat() if run.lease_expires_at else None,
        created_at=run.created_at.isoformat(),
    )


def recover_one(
    session: Session,
    run_id: UUID,
    reason: str,
    *,
    pending_age: timedelta = timedelta(minutes=15),
    dispatch: Callable[[], int] | None = None,
) -> None:
    """Reset one locked, non-terminal run and dispatch only after durable commit."""

    repository = BacktestRepository(session)
    run = repository.get_run(run_id, lock=True)
    if run is None:
        raise RuntimeError(f"Run {run_id} disappeared before recovery.")
    now = datetime.now(UTC)
    pending_before = now - pending_age
    current_reason = classify_stall(run, now=now, pending_before=pending_before)
    if current_reason != reason:
        session.rollback()
        raise RuntimeError(f"Run {run_id} is no longer stalled; no changes were made.")
    previous_status = run.status
    run.status = "PENDING"
    run.progress = 0
    run.started_at = None
    run.completed_at = None
    run.lease_owner = None
    run.lease_expires_at = None
    run.error_code = None
    run.error_message = None
    intent = session.scalar(
        select(JobDispatch)
        .where(JobDispatch.kind == "BACKTEST", JobDispatch.resource_id == run.id)
        .with_for_update()
    )
    if intent is None:
        enqueue_job(session, "BACKTEST", run.id)
    else:
        intent.status = "PENDING"
        intent.delivered_at = None
    repository.append_event(
        run.id,
        "BACKTEST_ADMIN_REQUEUED",
        {
            "reason": reason,
            "previous_status": previous_status,
            "operator_action": "stalled_run_recovery",
        },
    )
    session.commit()
    # A broker failure leaves the committed PENDING intent for the regular dispatcher.
    if dispatch is not None:
        dispatch()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending-age-minutes", type=int, default=15)
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--apply", action="store_true", help="Persist and dispatch recovery.")
    parser.add_argument("--confirm", help=f"Required with --apply: {CONFIRMATION}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.pending_age_minutes < 1:
        raise SystemExit("--pending-age-minutes must be at least 1")
    if args.apply and args.confirm != CONFIRMATION:
        raise SystemExit(f"--apply requires --confirm {CONFIRMATION}")

    session = create_session_factory(get_settings())()
    try:
        now = datetime.now(UTC)
        candidates = find_candidates(
            session,
            now=now,
            pending_age=timedelta(minutes=args.pending_age_minutes),
            run_id=args.run_id,
        )
        report = {
            "mode": "apply" if args.apply else "dry_run",
            "checked_at": now.isoformat(),
            "candidate_count": len(candidates),
            "candidates": [asdict(describe(run, reason)) for run, reason in candidates],
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        if args.apply:
            for run, reason in candidates:
                recover_one(
                    session,
                    run.id,
                    reason,
                    pending_age=timedelta(minutes=args.pending_age_minutes),
                    dispatch=lambda: dispatch_pending_jobs(get_settings()),
                )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
