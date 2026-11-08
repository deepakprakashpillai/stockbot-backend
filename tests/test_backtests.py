from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api.v1 import backtests as routes
from app.application import backtests as service_module
from app.application.backtests import BacktestService, BacktestServiceError
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun, EquityPoint, Trade
from app.db.models.datasets import DatasetVersion
from app.db.repositories.backtests import BacktestRepository
from app.domain.algorithms import BULL_CALL_SPREAD, LEGACY_BULL_CALL_SPREAD, PLATFORM_DEMO


def request() -> Request:
    value = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    value.state.request_id = "backtest-test"
    return value


def dataset(capabilities: dict[str, bool] | None = None) -> DatasetVersion:
    return DatasetVersion(
        id=uuid4(),
        dataset_id=uuid4(),
        version_number=1,
        schema_profile="fixture",
        original_filename="fixture.csv",
        checksum="a" * 64,
        storage_key="fixture.csv",
        status="VALID",
        row_count=32,
        session_count=1,
        session_dates=["2026-01-02"],
        timezone="Asia/Kolkata",
        resolution_minutes=5,
        capabilities=capabilities or {"price_candles": True},
        warnings=[],
        errors=[],
        validation_progress=100,
        created_at=datetime.now(UTC),
    )


def algorithm() -> AlgorithmVersion:
    return AlgorithmVersion(
        id=uuid4(),
        algorithm_key="bull_call_spread",
        version_number=1,
        name="fixture",
        code_version=BULL_CALL_SPREAD.code_version,
        schema_version=1,
        parameters=BULL_CALL_SPREAD.templates[0].parameters,
        creator_context={},
        archived=False,
        created_at=datetime.now(UTC),
    )


def demo_algorithm() -> AlgorithmVersion:
    return AlgorithmVersion(
        id=uuid4(),
        algorithm_key="platform_demo",
        version_number=1,
        name="Default platform demo",
        code_version=PLATFORM_DEMO.code_version,
        schema_version=PLATFORM_DEMO.schema_version,
        parameters=PLATFORM_DEMO.templates[0].parameters,
        creator_context={"type": "system_fixture", "synthetic": "true"},
        archived=False,
        created_at=datetime.now(UTC),
    )


def legacy_algorithm() -> AlgorithmVersion:
    return AlgorithmVersion(
        id=uuid4(),
        algorithm_key="bull_call_spread",
        version_number=1,
        name="legacy fixture",
        code_version=LEGACY_BULL_CALL_SPREAD.code_version,
        schema_version=LEGACY_BULL_CALL_SPREAD.schema_version,
        parameters=LEGACY_BULL_CALL_SPREAD.templates[0].parameters,
        creator_context={},
        archived=False,
        created_at=datetime.now(UTC),
    )


def test_preflight_blocks_the_candle_only_dataset(monkeypatch) -> None:
    candle_only, configured = dataset(), algorithm()

    class DatasetRepository:
        def __init__(self, _session: object) -> None:
            pass

        def get_version(self, _id):
            return candle_only

    class AlgorithmRepository:
        def __init__(self, _session: object) -> None:
            pass

        def get_version(self, _id):
            return configured

    class RunRepository:
        def __init__(self, _session: object) -> None:
            pass

    class RangeService:
        def __init__(self, *_args):
            pass

        def range_preflight(self, *_args):
            return {
                "selection_allowed": True,
                "issues": [],
                "usable_session_count": 1,
                "estimated_row_count": 32,
            }

    monkeypatch.setattr(service_module, "DatasetRepository", DatasetRepository)
    monkeypatch.setattr(service_module, "AlgorithmRepository", AlgorithmRepository)
    monkeypatch.setattr(service_module, "BacktestRepository", RunRepository)
    monkeypatch.setattr(service_module, "DatasetService", RangeService)
    result = BacktestService(object(), object(), object()).preflight(
        candle_only.id, configured.id, date(2026, 1, 2), date(2026, 1, 2), False
    )  # type: ignore[arg-type]
    assert result["allowed"] is False
    assert result["missing_capabilities"] == [
        "futures_vwap",
        "india_vix",
        "option_contracts",
        "option_ohlc",
    ]


def test_preflight_allows_platform_demo_with_candle_only_dataset(monkeypatch) -> None:
    candle_only, configured = dataset(), demo_algorithm()

    monkeypatch.setattr(
        service_module,
        "DatasetRepository",
        lambda _session: SimpleNamespace(get_version=lambda _id: candle_only),
    )
    monkeypatch.setattr(
        service_module,
        "AlgorithmRepository",
        lambda _session: SimpleNamespace(get_version=lambda _id: configured),
    )
    monkeypatch.setattr(service_module, "BacktestRepository", lambda _session: object())
    monkeypatch.setattr(
        service_module.DatasetService,
        "range_preflight",
        lambda _self, *_args: {
            "selection_allowed": True,
            "issues": [],
            "usable_session_count": 1,
            "estimated_row_count": 32,
        },
    )

    result = BacktestService(object(), object(), object()).preflight(
        candle_only.id, configured.id, date(2026, 1, 2), date(2026, 1, 2), False
    )  # type: ignore[arg-type]

    assert result["allowed"] is True
    assert result["mode"] == "PLATFORM_DEMO"
    assert result["required_capabilities"] == ["price_candles"]
    assert result["missing_capabilities"] == []


def test_route_preflight_and_not_found_responses(monkeypatch) -> None:
    value = dataset()

    class Service:
        def preflight(self, *_args):
            return {
                "allowed": False,
                "mode": "SIGNAL_ONLY",
                "dataset_version_id": value.id,
                "algorithm_version_id": uuid4(),
                "requested_start_date": date(2026, 1, 2),
                "requested_end_date": date(2026, 1, 2),
                "usable_session_count": 1,
                "estimated_row_count": 32,
                "required_capabilities": ["price_candles"],
                "missing_capabilities": ["india_vix"],
                "acknowledgement_required": False,
                "warnings": [],
                "issues": [],
            }

    monkeypatch.setattr(routes, "_service", lambda *_args: Service())
    response = routes.preflight(
        routes.BacktestPreflightRequest(
            dataset_version_id=value.id,
            algorithm_version_id=uuid4(),
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            starting_capital=Decimal("100"),
        ),
        request(),
        object(),
    )
    assert response.allowed is False

    class Repository:
        def __init__(self, _session):
            pass

        def get_run(self, _id):
            return None

    monkeypatch.setattr(routes, "BacktestRepository", Repository)
    missing = routes.get_backtest(uuid4(), request(), object())
    assert missing.status_code == 404


def test_live_snapshot_is_derived_from_durable_records(monkeypatch) -> None:
    run_id = uuid4()
    completed_at = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
    run = SimpleNamespace(
        id=run_id,
        status="COMPLETED",
        progress=100,
        completed_at=completed_at,
        metrics=SimpleNamespace(
            metrics={
                "processed_candles": 120,
                "estimated_candles": 120,
                "last_market_timestamp": "2026-01-02T09:59:00+00:00",
            }
        ),
    )

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, _id):
            return run

        def latest_signal_sequence(self, _id):
            return 120

        def latest_trade_sequence(self, _id):
            return 3

        def latest_equity_sequence(self, _id):
            return 3

        def latest_event_sequence(self, _id):
            return 18

    monkeypatch.setattr(routes, "BacktestRepository", Repository)
    snapshot = routes.get_live_snapshot(run_id, request(), object())
    assert snapshot.processed_candles == 120
    assert snapshot.last_processed_candle_at == datetime(2026, 1, 2, 9, 59, tzinfo=UTC)
    assert snapshot.latest_signal_sequence == 120
    assert snapshot.latest_event_sequence == 18
    assert snapshot.financial_pnl_available is False


def test_create_rejects_failed_preflight(monkeypatch) -> None:
    service = SimpleNamespace(
        create=lambda **_kwargs: (_ for _ in ()).throw(
            BacktestServiceError("BACKTEST_PREFLIGHT_FAILED", "blocked", 409)
        )
    )
    monkeypatch.setattr(routes, "_service", lambda *_args: service)
    response = routes.create_backtest(
        routes.BacktestCreateRequest(
            dataset_version_id=uuid4(),
            algorithm_version_id=uuid4(),
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            starting_capital=Decimal("100"),
        ),
        request(),
        None,
        object(),
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("configured", "mode", "expected_model", "expected_synthetic"),
    [
        (legacy_algorithm(), "SIGNAL_ONLY", "hard_gates_confirmation_5", False),
        (demo_algorithm(), "PLATFORM_DEMO", "platform_demo", True),
    ],
)
def test_execute_persists_mode_safe_metrics(
    monkeypatch, tmp_path, configured, mode, expected_model, expected_synthetic
) -> None:
    stored = dataset({"price_candles": True, "futures_vwap": True, "india_vix": True})
    run = SimpleNamespace(
        id=uuid4(),
        dataset_version_id=stored.id,
        algorithm_version_id=configured.id,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
        mode=mode,
        status="PENDING",
        cancellation_requested=False,
        progress=0,
        started_at=None,
        completed_at=None,
        lease_owner=None,
        lease_expires_at=None,
        snapshot={},
    )
    csv_file = tmp_path / "fixture.csv"
    csv_file.write_text(
        "timestamp,high,close,futures_vwap,india_vix,previous_india_vix\n"
        + "\n".join(
            f"{(datetime.fromisoformat('2026-01-02T09:15:00+05:30') + timedelta(minutes=index * 5)).isoformat()},{101 + index},{100 + index},{99 + index},18,20"
            for index in range(32)
        )
        + "\n",
        encoding="utf-8",
    )
    stored.checksum = hashlib.sha256(csv_file.read_bytes()).hexdigest()
    run.snapshot = {
        "dataset": {
            "id": str(stored.id),
            "checksum": stored.checksum,
            "schema_profile": stored.schema_profile,
            "storage_key": stored.storage_key,
            "capabilities": stored.capabilities,
        },
        "algorithm": {
            "key": configured.algorithm_key,
            "version_id": str(configured.id),
            "version_number": configured.version_number,
            "code_version": configured.code_version,
            "schema_version": configured.schema_version,
            "parameters": configured.parameters,
        },
        "range": {"start_date": "2026-01-02", "end_date": "2026-01-02"},
        "starting_capital": "100000.00",
        "engine_version": "0.1.0-signal-only",
        "assumptions": {
            "mode": mode,
            "synthetic": expected_synthetic,
            "unit": "DEMO_UNITS" if expected_synthetic else None,
            "financial_pnl_available": False,
            "demo_execution": {
                "adapter_version": "1.0.0-platform-demo",
                "synthetic": True,
                "unit": "DEMO_UNITS",
                "financial_pnl_available": False,
                "quantity": "1",
                "costs_demo_units": "0",
                "exit_rule": "DEMO_HOLD_INTERVAL_COMPLETE",
            }
            if expected_synthetic
            else None,
        },
    }

    class Session:
        def __init__(self):
            self.added = []
            self.merged = []
            self.commits = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            raise AssertionError("successful run must not roll back")

        def add_all(self, values):
            self.added.extend(values)

        def merge(self, value):
            self.merged.append(value)

        def query(self, _model):
            return SimpleNamespace(filter=lambda *_args: SimpleNamespace(delete=lambda: None))

    class DatasetRepository:
        def __init__(self, _session):
            pass

        def get_version(self, _id):
            return stored

    class AlgorithmRepository:
        def __init__(self, _session):
            pass

        def get_version(self, _id):
            return configured

    class Runs:
        def __init__(self, _session):
            self.events = []

        def claim_lease(self, _id, _owner):
            run.lease_owner = _owner
            return run

        def renew_lease(self, _id, _owner):
            return run if run.lease_owner == _owner else None

        def get_run(self, _id, **_kwargs):
            return run

        def append_event(self, _id, event_type, payload):
            self.events.append((event_type, payload))

    monkeypatch.setattr(service_module, "DatasetRepository", DatasetRepository)
    monkeypatch.setattr(service_module, "AlgorithmRepository", AlgorithmRepository)
    monkeypatch.setattr(service_module, "BacktestRepository", Runs)
    session = Session()
    result = BacktestService(
        session, SimpleNamespace(resolve=lambda _key: csv_file), object()
    ).execute(run.id)
    assert result["status"] == "COMPLETED"
    assert run.status == "COMPLETED"
    assert session.added
    metrics = session.merged[0].metrics
    assert metrics["pnl_available"] is False
    assert metrics["financial_pnl_available"] is False
    assert metrics["synthetic"] is expected_synthetic
    assert metrics["unit"] == ("DEMO_UNITS" if expected_synthetic else None)
    assert metrics["model"] == expected_model
    if expected_synthetic:
        trades = [item for item in session.added if isinstance(item, Trade)]
        equity = [item for item in session.added if isinstance(item, EquityPoint)]
        assert len(trades) == len(equity) == 5
        assert all(len(trade.legs) == 2 for trade in trades)
        assert all(trade.net_demo_units == Decimal("3.0") for trade in trades)
        assert metrics["total_trades"] == 5
        added_before_duplicate = len(session.added)
        assert (
            BacktestService(
                session, SimpleNamespace(resolve=lambda _key: csv_file), object()
            ).execute(run.id)["status"]
            == "COMPLETED"
        )
        assert len(session.added) == added_before_duplicate
    else:
        assert not any(isinstance(item, Trade | EquityPoint) for item in session.added)
        assert "total_trades" not in metrics


def test_repository_paginates_events_and_claims_leases() -> None:
    run_id = uuid4()
    run = SimpleNamespace(id=run_id, status="PENDING", lease_expires_at=None, lease_owner=None)
    events = [SimpleNamespace(id=uuid4(), sequence=1), SimpleNamespace(id=uuid4(), sequence=2)]
    signals = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]

    class Session:
        def __init__(self):
            self.values = [[run, run], signals, events]
            self.scalar_values = [run, run, None]
            self.added = []
            self.flushes = 0

        def scalar(self, _statement):
            return self.scalar_values.pop(0)

        def scalars(self, _statement):
            return self.values.pop(0)

        def add(self, value):
            self.added.append(value)

        def flush(self):
            self.flushes += 1

    session = Session()
    repository = BacktestRepository(session)  # type: ignore[arg-type]
    assert repository.get_idempotent("key") is run
    assert repository.list_runs("PENDING", run_id, 1)[1] == run_id
    assert repository.list_signals(run_id, signals[0].id, 1)[1] == signals[0].id
    assert repository.list_events(run_id, 1, 1)[1] == 1
    claimed = repository.claim_lease(run_id, "worker")
    assert claimed.lease_owner == "worker"
    assert session.flushes == 1
    assert repository.append_event(run_id, "EVENT", {}).sequence == 1


def test_create_snapshots_immutable_inputs_and_is_idempotent() -> None:
    stored = dataset()
    configured = demo_algorithm()

    class Session:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.commits = 0

        def add(self, value: object) -> None:
            self.added.append(value)
            if isinstance(value, BacktestRun) and value.id is None:
                value.id = uuid4()

        def flush(self) -> None:
            pass

        def commit(self) -> None:
            self.commits += 1

    class Runs:
        def __init__(self) -> None:
            self.existing = None
            self.events: list[tuple[str, dict[str, object]]] = []

        def get_idempotent(self, _key: str):
            return self.existing

        def append_event(self, _run_id, event_type: str, payload: dict[str, object]) -> None:
            self.events.append((event_type, payload))

    session = Session()
    runs = Runs()
    service = object.__new__(BacktestService)
    service.session = session  # type: ignore[assignment]
    service.storage = object()  # type: ignore[assignment]
    service.settings = object()  # type: ignore[assignment]
    service.datasets = SimpleNamespace(get_version=lambda _id: stored)
    service.algorithms = SimpleNamespace(get_version=lambda _id: configured)
    service.runs = runs  # type: ignore[assignment]
    service.preflight = lambda *_args: {"allowed": True}  # type: ignore[method-assign]

    created, duplicate = service.create(
        dataset_version_id=stored.id,
        algorithm_version_id=configured.id,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
        starting_capital=Decimal("100000.00"),
        warning_acknowledged=False,
        idempotency_key="repeatable-run",
    )

    assert duplicate is False
    assert created.snapshot["dataset"]["checksum"] == stored.checksum
    assert created.snapshot["algorithm"]["parameters"] == configured.parameters
    assert created.snapshot["assumptions"]["option_execution"] == "not_evaluated"
    assert created.snapshot["assumptions"]["demo_execution"] == {
        "adapter_version": "1.0.0-platform-demo",
        "synthetic": True,
        "unit": "DEMO_UNITS",
        "financial_pnl_available": False,
        "quantity": "1",
        "costs_demo_units": "0",
        "exit_rule": "DEMO_HOLD_INTERVAL_COMPLETE",
    }
    assert runs.events == [("BACKTEST_QUEUED", {"mode": "PLATFORM_DEMO"})]
    runs.existing = created
    repeated, duplicate = service.create(
        dataset_version_id=stored.id,
        algorithm_version_id=configured.id,
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
        starting_capital=Decimal("100000.00"),
        warning_acknowledged=False,
        idempotency_key="repeatable-run",
    )
    assert repeated is created and duplicate is True
    with pytest.raises(BacktestServiceError, match="different run inputs"):
        service.create(
            dataset_version_id=uuid4(),
            algorithm_version_id=configured.id,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            starting_capital=Decimal("100000.00"),
            warning_acknowledged=False,
            idempotency_key="repeatable-run",
        )


def test_cancel_and_early_execution_states_are_durable() -> None:
    class Session:
        def __init__(self) -> None:
            self.commits = 0

        def commit(self) -> None:
            self.commits += 1

    class Runs:
        def __init__(self, run) -> None:
            self.run = run
            self.events: list[str] = []

        def get_run(self, _id, **_kwargs):
            return self.run

        def claim_lease(self, _id, _owner):
            return self.run

        def append_event(self, _id, event_type, _payload) -> None:
            self.events.append(event_type)

    run = SimpleNamespace(
        id=uuid4(),
        status="PENDING",
        progress=0,
        cancellation_requested=False,
        completed_at=None,
    )
    session = Session()
    service = object.__new__(BacktestService)
    service.session = session  # type: ignore[assignment]
    service.runs = Runs(run)  # type: ignore[assignment]

    cancelled = service.cancel(run.id)
    assert cancelled.cancellation_requested is True
    assert service.runs.events == ["CANCELLATION_REQUESTED", "BACKTEST_CANCELLED"]  # type: ignore[attr-defined]
    result = service.execute(run.id)
    assert result["status"] == "CANCELLED"
    assert run.progress == 100

    run.status, run.cancellation_requested = "COMPLETED", False
    assert service.execute(run.id)["status"] == "COMPLETED"
    with pytest.raises(BacktestServiceError, match="Terminal backtests"):
        service.cancel(run.id)

    service.runs.run = None  # type: ignore[attr-defined]
    assert service.execute(run.id)["status"] == "NOT_CLAIMED"
    with pytest.raises(BacktestServiceError, match="does not exist"):
        service.cancel(run.id)


def test_invalid_snapshot_fails_run_and_releases_lease() -> None:
    run = SimpleNamespace(
        id=uuid4(),
        dataset_version_id=uuid4(),
        algorithm_version_id=uuid4(),
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
        snapshot={},
        status="PENDING",
        progress=0,
        cancellation_requested=False,
        started_at=None,
        completed_at=None,
        lease_owner=None,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
    )

    class Session:
        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    class Runs:
        def claim_lease(self, _id, owner):
            run.lease_owner = owner
            return run

        def get_run(self, _id, **_kwargs):
            return run

        def append_event(self, _id, _event_type, _payload) -> None:
            pass

    service = object.__new__(BacktestService)
    service.session = Session()  # type: ignore[assignment]
    service.runs = Runs()  # type: ignore[assignment]
    service.datasets = SimpleNamespace(get_version=lambda _id: None)
    service.storage = object()  # type: ignore[assignment]
    service.settings = SimpleNamespace()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="snapshot is invalid"):
        service.execute(run.id)
    assert run.status == "FAILED"
    assert run.error_code == "BACKTEST_EXECUTION_FAILED"
    assert run.lease_owner is None and run.lease_expires_at is None
