import hashlib
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import exports
from app.application.exports import (
    BacktestExportService,
    ExportServiceError,
    _csv_bytes,
    _csv_chunks,
    _json_array_chunks,
    _json_with_periods_chunks,
    _jsonl_bytes,
    _jsonl_chunks,
)
from app.db.models.backtests import BacktestRun, JournalEvent, RunAnalytics, SignalEvaluation
from app.db.models.jobs import JobDispatch
from tests.conftest import make_test_settings


def export_service(monkeypatch, tmp_path, *, format="ZIP", status="PENDING"):
    run = SimpleNamespace(
        id=uuid4(),
        status="COMPLETED",
        mode="PLATFORM_DEMO",
        start_date=date(2025, 7, 21),
        end_date=date(2025, 7, 21),
        starting_capital=Decimal("100"),
        dataset_version_id=uuid4(),
        algorithm_version_id=uuid4(),
        snapshot={
            "assumptions": {
                "synthetic": True,
                "unit": "DEMO_UNITS",
                "financial_pnl_available": False,
            },
            "coverage": {"included_sessions": ["2025-07-21"]},
        },
    )
    job = SimpleNamespace(
        id=uuid4(),
        run_id=run.id,
        attempt_id=uuid4(),
        format=format,
        status=status,
        lease_owner=None,
        lease_expires_at=None,
    )

    class Session:
        def __init__(self):
            self.added = []
            self.commits = 0
            self.rollbacks = 0
            self.lose_ownership = False

        def add(self, value):
            self.added.append(value)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def get(self, model, identity):
            if model is BacktestRun:
                return run
            assert model is RunAnalytics
            return None

        def scalars(self, _statement):
            if self.lose_ownership:
                job.lease_owner = "replacement-worker"
            return []

        def scalar(self, _statement):
            return 0

    session = Session()
    repository = SimpleNamespace(
        get_run=lambda _id: run,
        get_export_job=lambda _id, **_kwargs: job,
        create_export_job=lambda _run, **_kwargs: job,
    )
    monkeypatch.setattr(exports, "BacktestRepository", lambda _session: repository)
    settings = make_test_settings().model_copy(update={"storage_root": tmp_path})
    return BacktestExportService(session, settings), session, run, job


@pytest.mark.parametrize("format", ["ZIP", "XLSX"])
def test_export_build_is_atomic_checksummed_and_idempotent(monkeypatch, tmp_path, format):
    service, session, run, job = export_service(monkeypatch, tmp_path, format=format)
    assert service.create(run.id) is job
    assert any(
        isinstance(value, JobDispatch) and value.resource_id == job.id for value in session.added
    )
    assert service.build(job.id) is job
    artifact = tmp_path / job.artifact_path
    assert job.status == "COMPLETED"
    assert job.lease_owner is None and job.lease_expires_at is None
    assert job.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert not list(artifact.parent.glob("*.tmp"))
    before = artifact.read_bytes()
    assert service.build(job.id) is job
    assert artifact.read_bytes() == before
    if format == "ZIP":
        with zipfile.ZipFile(artifact) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["synthetic"] is True
            assert manifest["schema_version"] == "stockbot.backtest-export.v3"
            assert manifest["estimate_limitations"] == []
            assert manifest["financial_pnl_available"] is False
            assert manifest["attempt_id"] == str(job.attempt_id)
            assert manifest["coverage"] == run.snapshot["coverage"]
            for name, identity in manifest["files"].items():
                content = archive.read(name)
                assert identity == {
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }


def test_zip_streams_signal_and_journal_rows_without_changing_member_bytes(monkeypatch, tmp_path):
    service, session, _run, job = export_service(monkeypatch, tmp_path)
    timestamp = datetime(2025, 7, 21, 9, 15, tzinfo=UTC)
    signal = SimpleNamespace(
        id=uuid4(),
        sequence=1,
        timestamp=timestamp,
        session_date=timestamp.date(),
        decision="ACCEPTED",
        rejection_reason=None,
        window="window_1",
        score=3,
        inputs={"vix": "20.00"},
        diagnostic={"reason": "passed"},
    )
    journal = SimpleNamespace(
        sequence=1,
        timestamp=timestamp,
        session_date=timestamp.date(),
        event_type="SIGNAL_EVALUATED",
        position_state=None,
        signal_sequence=1,
        trade_sequence=None,
        payload={"decision": "ACCEPTED"},
    )
    reads = {SignalEvaluation: 0, JournalEvent: 0}

    def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity in reads:
            reads[entity] += 1
        return {SignalEvaluation: [signal], JournalEvent: [journal]}.get(entity, [])

    session.scalars = scalars
    service.build(job.id)
    with zipfile.ZipFile(tmp_path / job.artifact_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["row_counts"]["signals"] == 1
        assert manifest["row_counts"]["journal"] == 1
        assert json.loads(archive.read("signals.jsonl"))["id"] == str(signal.id)
        assert json.loads(archive.read("journal.jsonl"))["event_type"] == "SIGNAL_EVALUATED"
        assert archive.read("signals.csv").count(b"\n") == 2
        assert archive.read("journal.csv").count(b"\n") == 2
    assert reads == {SignalEvaluation: 2, JournalEvent: 2}


@pytest.mark.parametrize("rows", [[], [{"id": 1, "payload": {"x": 2}}, {"id": 2, "payload": []}]])
def test_streamed_evidence_encoding_matches_existing_archive_members(rows):
    assert b"".join(_jsonl_chunks(iter(rows))) == _jsonl_bytes(rows)
    assert b"".join(_csv_chunks(iter(rows))) == _csv_bytes(rows)
    assert b"".join(_json_array_chunks(iter(rows))) == exports._json_bytes(rows)
    analytics = {"schema_version": "v1", "summary": {"net": "3.00"}}
    assert b"".join(_json_with_periods_chunks(analytics, iter(rows))) == exports._json_bytes(
        {**analytics, "periods": rows}
    )


def test_export_duplicate_worker_and_lost_owner_cannot_publish(monkeypatch, tmp_path):
    service, session, _run, job = export_service(monkeypatch, tmp_path, status="RUNNING")
    job.lease_owner = "other-worker"
    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    assert service.build(job.id).status == "RUNNING"
    assert session.commits == 0
    job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.lose_ownership = True
    with pytest.raises(RuntimeError, match="ownership was lost"):
        service.build(job.id)
    assert session.rollbacks == 1
    assert job.lease_owner == "replacement-worker"
    assert not list(tmp_path.rglob("*.zip"))


def test_export_rejects_incomplete_run_and_records_build_failure(monkeypatch, tmp_path):
    service, session, run, job = export_service(monkeypatch, tmp_path)
    with pytest.raises(ExportServiceError, match="Unsupported"):
        service.create(run.id, format="CSV")
    run.status = "RUNNING"
    with pytest.raises(ExportServiceError, match="Only completed"):
        service.create(run.id)
    with pytest.raises(ValueError, match="no longer available"):
        service.build(job.id)
    assert session.rollbacks == 1
    assert job.status == "FAILED" and job.error_code == "EXPORT_BUILD_FAILED"
    assert job.lease_owner is None
