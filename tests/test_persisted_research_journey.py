"""Real SQL persistence/HTTP checks; SQLite does not establish PostgreSQL locking."""

import hashlib
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application.algorithms import sync_registry
from app.application.backtests import BacktestService, BacktestServiceError
from app.application.exports import BacktestExportService
from app.db.base import Base
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import (
    BacktestRun,
    ExperimentRun,
    JournalEvent,
    SignalEvaluation,
    Trade,
)
from app.db.models.datasets import Dataset
from app.db.models.jobs import JobDispatch
from app.db.repositories.backtests import BacktestRepository
from app.infrastructure.datasets.storage import LocalDatasetStorage
from app.main import create_app
from tests.conftest import make_test_settings
from tests.test_backtests import dataset


@compiles(JSONB, "sqlite")
def sqlite_json_storage(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(PostgreSQLUUID, "sqlite")
def sqlite_uuid_storage(_type, _compiler, **_kwargs):
    # UUID's native SQLite numeric affinity can coerce hexadecimal IDs containing
    # e into floats. Keep their bytes as text in this test-only adapter.
    return "CHAR(32)"


@pytest.fixture
def persisted_run(tmp_path):
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    settings = make_test_settings().model_copy(update={"storage_root": tmp_path})
    fixture = Path(__file__).parent / "fixtures/platform_demo_nifty.csv"
    shutil.copyfile(fixture, tmp_path / "fixture.csv")
    storage = LocalDatasetStorage(tmp_path)
    with factory() as session:
        sync_registry(session)
        configured = session.scalar(
            select(AlgorithmVersion).where(AlgorithmVersion.algorithm_key == "platform_demo")
        )
        stored = dataset()
        stored.checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
        stored.coverage_start = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
        stored.coverage_end = datetime.fromisoformat("2026-01-02T11:50:00+05:30")
        session.add(Dataset(id=stored.dataset_id, name="Fixture market"))
        session.flush()
        session.add(stored)
        session.commit()
        service = BacktestService(session, storage, settings)
        run, _ = service.create(
            dataset_version_id=stored.id,
            algorithm_version_id=configured.id,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            starting_capital=Decimal("100000"),
            warning_acknowledged=False,
            idempotency_key="persisted-journey",
        )
        assert run.snapshot["coverage"]["included_sessions"] == ["2026-01-02"]
        assert (
            session.scalar(select(JobDispatch).where(JobDispatch.resource_id == run.id)) is not None
        )
        assert service.execute(run.id)["status"] == "COMPLETED"
        run_id = run.id
    app = create_app(settings=settings)
    app.state.session_factory = factory
    with TestClient(app) as client:
        yield client, factory, run_id, settings
    engine.dispose()


def test_saved_demo_journey_exposes_reconciled_records_and_exact_history(persisted_run):
    client, factory, run_id, _settings = persisted_run
    base = f"/api/v1/backtests/{run_id}"
    saved_run = client.get(base).json()
    assert saved_run["status"] == "COMPLETED"
    assert saved_run["included_session_count"] == 1
    assert client.get("/api/v1/backtests/history-summary").json()["total_runs"] == 1
    assert client.get("/api/v1/backtests", params={"limit": 1}).json()["items"][0]["id"] == str(
        run_id
    )
    for suffix in (
        "signals",
        "trades",
        "equity",
        "events",
        "attempts",
        "journal",
        "days",
        "metrics",
        "live-snapshot",
        "analytics/summary",
        "analytics/periods",
        "analytics/definitions",
        "analytics/diagnostics",
    ):
        response = client.get(f"{base}/{suffix}")
        assert response.status_code == 200, (suffix, response.text)
    with factory() as session:
        signal = session.scalar(select(SignalEvaluation).where(SignalEvaluation.run_id == run_id))
        trade = session.scalar(select(Trade).where(Trade.run_id == run_id))
        assert client.get(f"{base}/signals/{signal.id}/detail").status_code == 200
        assert client.get(f"{base}/trades/{trade.id}").status_code == 200
        assert client.get(f"{base}/signals/{uuid4()}").status_code == 404
    assert client.post(f"{base}/cancel").status_code == 409


def test_legacy_complete_run_with_missing_coverage_dates_does_not_claim_zero_sessions(
    persisted_run,
):
    client, factory, run_id, _settings = persisted_run
    with factory() as session:
        run = session.get(BacktestRun, run_id)
        assert run is not None
        run.snapshot = {
            **run.snapshot,
            "coverage": {**run.snapshot["coverage"], "included_sessions": []},
        }
        session.commit()
    response = client.get(f"/api/v1/backtests/{run_id}")
    assert response.status_code == 200
    assert response.json()["included_session_count"] is None


def test_journal_market_pagination_does_not_skip_equal_timestamps_or_other_attempts(persisted_run):
    client, factory, run_id, _settings = persisted_run
    with factory() as session:
        repository = BacktestRepository(session)
        run = repository.get_run(run_id)
        first = session.scalar(
            select(JournalEvent)
            .where(JournalEvent.run_id == run_id)
            .order_by(JournalEvent.sequence)
        )
        repository.append_journal(
            run_id,
            run.active_attempt_id,
            timestamp=first.timestamp,
            event_type="TEST_TIE",
            position_state=None,
            signal_sequence=None,
            trade_sequence=None,
            payload={},
        )
        session.commit()
        expected = [
            row.sequence
            for row in session.scalars(
                select(JournalEvent)
                .where(JournalEvent.attempt_id == run.active_attempt_id)
                .order_by(JournalEvent.timestamp, JournalEvent.sequence)
            )
        ]
    cursor = None
    actual = []
    while True:
        params = {"market_order": "true", "limit": 7}
        if cursor:
            params["market_cursor"] = cursor
        response = client.get(f"/api/v1/backtests/{run_id}/journal", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        actual.extend(row["sequence"] for row in page["items"])
        cursor = page["next_market_cursor"]
        if cursor is None:
            break
    assert actual == expected
    assert len(actual) == len(set(actual))
    assert (
        client.get(
            f"/api/v1/backtests/{run_id}/journal",
            params={"market_order": "true", "market_cursor": "invalid"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("format", ["ZIP", "XLSX"])
def test_export_round_trip_verifies_download_and_rejects_corruption(persisted_run, format):
    client, factory, run_id, settings = persisted_run
    response = client.post(f"/api/v1/backtests/{run_id}/export-jobs", json={"format": format})
    assert response.status_code == 202, response.text
    from uuid import UUID

    with factory() as session:
        job = BacktestExportService(session, settings).build(UUID(response.json()["id"]))
        artifact = settings.storage_root / job.artifact_path
        assert job.row_counts["signals"] > 0
        session.expire_all()
        assert BacktestRepository(session).get_export_job(job.id).row_counts["signals"] > 0
    download = f"/api/v1/backtests/export-jobs/{job.id}/download"
    assert client.get(download).status_code == 200
    artifact.write_bytes(b"corrupted archive")
    assert client.get(download).status_code == 409


def test_experiment_api_rolls_back_partial_batch_then_starts_without_duplicates(
    persisted_run, monkeypatch
):
    client, factory, run_id, _settings = persisted_run
    with factory() as session:
        baseline = session.scalar(
            select(AlgorithmVersion).where(AlgorithmVersion.algorithm_key == "bull_call_spread")
        )
        dataset_id = BacktestRepository(session).get_run(run_id).dataset_version_id
        baseline_id = baseline.id
        before_versions = len(list(session.scalars(select(AlgorithmVersion))))
        before_jobs = len(list(session.scalars(select(JobDispatch))))
    # This test isolates transaction behaviour. Real derivatives qualification
    # and estimated execution are covered by the separate market fixture tests.
    monkeypatch.setattr(
        BacktestService,
        "preflight",
        lambda *_args, **_kwargs: {
            "allowed": True,
            "issues": [],
            "coverage_digest": "a" * 64,
            "included_session_count": 1,
            "excluded_sessions": [],
        },
    )
    inputs = {
        "name": "Atomic batch",
        "hypothesis": "Compare explicit quantities",
        "baseline_algorithm_version_id": str(baseline_id),
        "dataset_version_id": str(dataset_id),
        "parameter_key": "lots",
        "parameter_values": [2],
        "development_start_date": "2026-01-02",
        "development_end_date": "2026-01-02",
        "validation_start_date": "2026-01-05",
        "validation_end_date": "2026-01-05",
        "starting_capital": "100000",
        "coverage_digest": "0" * 64,
    }
    review = client.post("/api/v1/experiments/preflight", json=inputs)
    assert review.status_code == 200, review.text
    inputs["coverage_digest"] = review.json()["coverage_digest"]
    created = client.post("/api/v1/experiments", json=inputs)
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    original_create = BacktestService.create
    calls = []

    def fail_second(service, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise BacktestServiceError("FIXTURE_FAILURE", "Second variant failed", 409)
        return original_create(service, **kwargs)

    monkeypatch.setattr(BacktestService, "create", fail_second)
    assert client.post(f"/api/v1/experiments/{plan_id}/start").status_code == 409
    with factory() as session:
        assert list(session.scalars(select(ExperimentRun))) == []
        assert len(list(session.scalars(select(AlgorithmVersion)))) == before_versions
        assert len(list(session.scalars(select(JobDispatch)))) == before_jobs
    monkeypatch.setattr(BacktestService, "create", original_create)
    started = client.post(f"/api/v1/experiments/{plan_id}/start")
    assert started.status_code == 202, started.text
    assert len(started.json()["runs"]) == 2
    repeated = client.post(f"/api/v1/experiments/{plan_id}/start")
    assert [row["id"] for row in repeated.json()["runs"]] == [
        row["id"] for row in started.json()["runs"]
    ]
    assert (
        client.get(f"/api/v1/experiments/{plan_id}").json()["plan"]["status"]
        == "DEVELOPMENT_RUNNING"
    )
    assert client.get(f"/api/v1/experiments/{uuid4()}").status_code == 404
    invalid = {**inputs, "parameter_key": "pdh_weight", "parameter_values": [1]}
    assert client.post("/api/v1/experiments/preflight", json=invalid).status_code == 422


def test_idempotency_handles_competing_insert_and_rejects_changed_request(
    persisted_run, monkeypatch
):
    client, factory, run_id, settings = persisted_run
    run = client.get(f"/api/v1/backtests/{run_id}").json()
    inputs = {
        key: run[key]
        for key in (
            "dataset_version_id",
            "algorithm_version_id",
            "start_date",
            "end_date",
            "starting_capital",
        )
    }
    headers = {"Idempotency-Key": "persisted-journey"}
    repeated = client.post("/api/v1/backtests", json=inputs, headers=headers)
    assert repeated.status_code == 202 and repeated.json()["id"] == str(run_id)
    changed = client.post(
        "/api/v1/backtests", json={**inputs, "starting_capital": "999999"}, headers=headers
    )
    assert changed.status_code == 409
    with factory() as session:
        service = BacktestService(session, LocalDatasetStorage(settings.storage_root), settings)
        original_lookup = service.runs.get_idempotent
        lookups = []

        def missed_first_lookup(key):
            lookups.append(key)
            return None if len(lookups) == 1 else original_lookup(key)

        monkeypatch.setattr(service.runs, "get_idempotent", missed_first_lookup)
        from uuid import UUID

        existing, duplicate = service.create(
            dataset_version_id=UUID(inputs["dataset_version_id"]),
            algorithm_version_id=UUID(inputs["algorithm_version_id"]),
            start_date=date.fromisoformat(inputs["start_date"]),
            end_date=date.fromisoformat(inputs["end_date"]),
            starting_capital=Decimal(inputs["starting_capital"]),
            warning_acknowledged=False,
            idempotency_key="persisted-journey",
        )
        assert duplicate and existing.id == run_id
        assert len(lookups) == 3
