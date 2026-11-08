from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import recover_stalled_runs
from scripts.backfill_completed_run_analytics import build_parser as backfill_parser
from scripts.benchmark_platform_demo import run_benchmark
from scripts.recover_stalled_runs import CONFIRMATION, build_parser, classify_stall


def run(**overrides: object) -> SimpleNamespace:
    values = {
        "status": "RUNNING",
        "cancellation_requested": False,
        "lease_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        "lease_owner": "dead-worker",
        "created_at": datetime.now(UTC) - timedelta(hours=1),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_stall_classification_is_conservative() -> None:
    now = datetime.now(UTC)
    pending_before = now - timedelta(minutes=15)
    assert classify_stall(run(), now=now, pending_before=pending_before) == "expired_worker_lease"
    assert (
        classify_stall(
            run(lease_expires_at=now + timedelta(seconds=1)), now=now, pending_before=pending_before
        )
        is None
    )
    assert (
        classify_stall(run(cancellation_requested=True), now=now, pending_before=pending_before)
        is None
    )
    assert classify_stall(run(status="COMPLETED"), now=now, pending_before=pending_before) is None


def test_old_unclaimed_pending_delivery_is_recoverable() -> None:
    now = datetime.now(UTC)
    reason = classify_stall(
        run(status="PENDING", lease_owner=None),
        now=now,
        pending_before=now - timedelta(minutes=15),
    )
    assert reason == "stale_pending_delivery"


def test_recovery_cli_defaults_to_dry_run() -> None:
    args = build_parser().parse_args([])
    assert args.apply is False
    assert args.confirm is None
    assert CONFIRMATION == "RECOVER_STALLED_RUNS"


def test_stalled_recovery_commits_retry_intent_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    stalled = run(
        id=run_id,
        progress=72,
        started_at=datetime.now(UTC),
        completed_at=None,
        error_code="WORKER_LOST",
        error_message="Worker stopped",
    )
    intent = SimpleNamespace(status="DELIVERED", delivered_at=datetime.now(UTC))
    events: list[str] = []

    class Session:
        def scalar(self, _statement: object) -> SimpleNamespace:
            return intent

        def commit(self) -> None:
            events.append("commit")

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, _run_id: object, *, lock: bool) -> SimpleNamespace:
            assert lock
            return stalled

        def append_event(self, _run_id: object, event_type: str, _payload: object) -> None:
            assert event_type == "BACKTEST_ADMIN_REQUEUED"

    monkeypatch.setattr(recover_stalled_runs, "BacktestRepository", Repository)

    def failed_dispatch() -> int:
        assert events == ["commit"]
        assert intent.status == "PENDING" and intent.delivered_at is None
        raise ConnectionError("broker unavailable")

    with pytest.raises(ConnectionError, match="broker unavailable"):
        recover_stalled_runs.recover_one(
            Session(), run_id, "expired_worker_lease", dispatch=failed_dispatch
        )

    assert stalled.status == "PENDING" and stalled.progress == 0
    assert stalled.lease_owner is None and stalled.lease_expires_at is None
    assert intent.status == "PENDING"


def test_benchmark_is_deterministic_and_explicitly_synthetic() -> None:
    first = run_benchmark(100, 2)
    second = run_benchmark(100, 1)
    assert first["decision_digest"] == second["decision_digest"]
    assert first["synthetic"] is True
    assert first["unit"] == "DEMO_UNITS"
    assert first["financial_pnl_available"] is False


def test_analytics_backfill_cli_is_bounded() -> None:
    assert backfill_parser().parse_args([]).limit == 100
    assert backfill_parser().parse_args(["--limit", "25"]).limit == 25
