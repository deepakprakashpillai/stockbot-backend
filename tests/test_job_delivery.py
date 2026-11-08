from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import jobs
from app.db.models.backtests import BacktestRun
from app.db.models.jobs import JobDispatch
from app.workers.celery_app import celery_app
from tests.conftest import make_test_settings


class DeliverySession:
    def __init__(self, intents, runs=None, exports=None):
        self.intents = intents
        self.runs = runs or {}
        self.exports = exports or []
        self.commits = 0
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def scalars(self, statement):
        self.queries.append(statement)
        return self.exports if len(self.queries) == 1 else self.intents

    def scalar(self, _statement):
        return next((item for item in self.intents if item.kind == "EXPORT"), None)

    def get(self, model, identity):
        assert model is BacktestRun
        return self.runs.get(identity)

    def flush(self):
        for item in self.intents:
            if item.attempts is None:
                item.attempts = 0
            if item.status is None:
                item.status = "PENDING"

    def add(self, intent):
        self.intents.append(intent)

    def commit(self):
        self.commits += 1


def intent(kind="BACKTEST", dependency=None):
    return JobDispatch(
        id=uuid4(),
        kind=kind,
        resource_id=uuid4(),
        depends_on_run_id=dependency,
        status="PENDING",
        attempts=0,
    )


def test_broker_failure_retains_durable_intent_then_retry_delivers_same_identity(monkeypatch):
    job = intent()
    session = DeliverySession([job], {job.resource_id: SimpleNamespace(status="PENDING")})
    calls = []

    def send(name, **kwargs):
        calls.append((name, kwargs))
        if len(calls) == 1:
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(celery_app, "send_task", send)
    assert jobs.dispatch_pending_jobs(make_test_settings(), session_factory=lambda: session) == 0
    assert job.status == "PENDING" and job.attempts == 1
    session.queries.clear()
    assert jobs.dispatch_pending_jobs(make_test_settings(), session_factory=lambda: session) == 1
    assert job.status == "DELIVERED" and job.attempts == 2
    assert calls[0] == calls[1]
    assert calls[1][1]["task_id"] == str(job.id)
    assert calls[1][1]["args"] == [str(job.resource_id)]
    assert session.commits == 2


@pytest.mark.parametrize("status", [None, "FAILED", "CANCELLED", "RUNNING", "COMPLETED"])
def test_dependent_job_waits_for_success_and_cancels_after_predecessor_failure(monkeypatch, status):
    previous_id = uuid4()
    job = intent(dependency=previous_id)
    child = SimpleNamespace(status="PENDING")
    runs = {job.resource_id: child}
    if status:
        runs[previous_id] = SimpleNamespace(status=status)
    session = DeliverySession([job], runs)
    sent = []
    monkeypatch.setattr(celery_app, "send_task", lambda *_args, **kwargs: sent.append(kwargs))
    count = jobs.dispatch_pending_jobs(make_test_settings(), session_factory=lambda: session)
    assert count == (1 if status == "COMPLETED" else 0)
    assert len(sent) == count
    if status in {None, "FAILED", "CANCELLED"}:
        assert child.status == job.status == "CANCELLED"
        assert child.error_code == "EXPERIMENT_PREDECESSOR_FAILED"
    elif status == "RUNNING":
        assert child.status == job.status == "PENDING"


def test_terminal_backtest_is_not_delivered_and_expired_export_is_requeued(monkeypatch):
    completed = intent()
    export = SimpleNamespace(
        id=uuid4(),
        status="RUNNING",
        lease_owner="dead-worker",
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session = DeliverySession(
        [completed], {completed.resource_id: SimpleNamespace(status="COMPLETED")}, [export]
    )
    monkeypatch.setattr(celery_app, "send_task", lambda *_args, **_kwargs: None)
    assert jobs.dispatch_pending_jobs(make_test_settings(), session_factory=lambda: session) == 1
    assert completed.status == "CANCELLED"
    assert export.status == "PENDING" and export.lease_owner is None
    assert session.intents[-1].resource_id == export.id
    assert session.intents[-1].status == "DELIVERED"
