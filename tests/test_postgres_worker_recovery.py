"""Opt-in process-death recovery proof against isolated PostgreSQL schemas."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.application.algorithms import sync_registry
from app.application.backtests import BacktestService
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun, ExecutionAttempt, SignalEvaluation
from app.db.models.datasets import Dataset
from app.infrastructure.datasets.storage import LocalDatasetStorage
from scripts.recover_stalled_runs import recover_one
from tests.conftest import make_test_settings
from tests.test_backtests import dataset


@pytest.mark.parametrize("crash_event", ["BACKTEST_STARTED", "BACKTEST_PROGRESS"])
def test_process_death_replays_saved_snapshot_without_duplicate_signals(
    postgres_factory, tmp_path: Path, crash_event: str
) -> None:
    fixture = Path(__file__).parent / "fixtures/platform_demo_nifty.csv"
    shutil.copyfile(fixture, tmp_path / "fixture.csv")
    storage = LocalDatasetStorage(tmp_path)
    settings = make_test_settings().model_copy(update={"storage_root": tmp_path})
    with postgres_factory() as session:
        sync_registry(session)
        configured = session.scalar(
            select(AlgorithmVersion).where(AlgorithmVersion.algorithm_key == "platform_demo")
        )
        assert configured is not None
        stored = dataset()
        stored.checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
        stored.coverage_start = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
        stored.coverage_end = datetime.fromisoformat("2026-01-02T11:50:00+05:30")
        session.add(Dataset(id=stored.dataset_id, name="Worker recovery fixture"))
        session.flush()
        session.add(stored)
        session.commit()
        run, _ = BacktestService(session, storage, settings).create(
            dataset_version_id=stored.id,
            algorithm_version_id=configured.id,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            starting_capital=Decimal("100000"),
            warning_acknowledged=False,
            idempotency_key=f"worker-death-{crash_event.lower()}",
        )
        run_id = run.id
        schema = session.scalar(select(func.current_schema()))
        assert isinstance(schema, str)

    child = """
import os
import sys
from uuid import UUID
from app.application.backtests import BacktestService
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.infrastructure.datasets.storage import LocalDatasetStorage

original = BacktestService._commit_and_publish
def crash_after_commit(self, *events):
    original(self, *events)
    if any(getattr(event, 'event_type', None) == sys.argv[2] for event in events):
        os._exit(83)
BacktestService._commit_and_publish = crash_after_commit
settings = get_settings()
with create_session_factory(settings)() as session:
    BacktestService(session, LocalDatasetStorage(settings.storage_root), settings).execute(UUID(sys.argv[1]))
"""
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=os.environ["STOCKBOT_TEST_DATABASE_URL"],
        STORAGE_ROOT=str(tmp_path),
        PGOPTIONS=f"-c search_path={schema}",
    )
    terminated = subprocess.run(
        [sys.executable, "-c", child, str(run_id), crash_event],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert terminated.returncode == 83, terminated.stderr

    with postgres_factory() as session:
        interrupted = session.get(BacktestRun, run_id)
        assert interrupted is not None and interrupted.status == "RUNNING"
        saved_before = session.scalar(
            select(func.count(SignalEvaluation.id)).where(SignalEvaluation.run_id == run_id)
        )
        if crash_event == "BACKTEST_PROGRESS":
            assert (saved_before or 0) > 0
        else:
            assert saved_before == 0
        session.execute(
            update(BacktestRun)
            .where(BacktestRun.id == run_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()
        recover_one(session, run_id, "expired_worker_lease")
        assert session.get(BacktestRun, run_id).status == "PENDING"
        assert BacktestService(session, storage, settings).execute(run_id)["status"] == "COMPLETED"
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.run_id == run_id)
                .order_by(ExecutionAttempt.attempt_number)
            )
        )
        assert [attempt.status for attempt in attempts] == ["INTERRUPTED", "COMPLETED"]
        final_sequences = list(
            session.scalars(
                select(SignalEvaluation.sequence)
                .where(SignalEvaluation.run_id == run_id)
                .order_by(SignalEvaluation.sequence)
            )
        )
        assert final_sequences == list(range(1, len(final_sequences) + 1))
        assert len(final_sequences) == 10
