from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import backtests as backtest_application
from app.application.datasets import service as dataset_service
from app.core import config
from app.db import session as session_module
from app.infrastructure import realtime
from app.workers import tasks
from app.workers.tasks import diagnostic_ping


def test_diagnostic_task_is_idempotent() -> None:
    assert diagnostic_ping.run("first") == {"status": "ok", "marker": "first"}
    assert diagnostic_ping.run("first") == {"status": "ok", "marker": "first"}


def test_dataset_validation_task_reports_terminal_state_and_closes_session(monkeypatch) -> None:
    closed: list[bool] = []
    local_session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(storage_root=Path("/tmp")))
    monkeypatch.setattr(
        session_module, "create_session_factory", lambda _settings: lambda: local_session
    )
    monkeypatch.setattr(
        dataset_service,
        "DatasetService",
        lambda *_args: SimpleNamespace(
            validate_version=lambda _id: SimpleNamespace(status="VALID")
        ),
    )

    result = tasks.validate_dataset_version.run(str(uuid4()))

    assert result["status"] == "VALID"
    assert closed == [True]


def test_dataset_validation_task_closes_session_when_validation_fails(monkeypatch) -> None:
    closed: list[bool] = []
    local_session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(storage_root=Path("/tmp")))
    monkeypatch.setattr(
        session_module, "create_session_factory", lambda _settings: lambda: local_session
    )
    monkeypatch.setattr(
        dataset_service,
        "DatasetService",
        lambda *_args: SimpleNamespace(
            validate_version=lambda _id: (_ for _ in ()).throw(RuntimeError("bad"))
        ),
    )

    with pytest.raises(RuntimeError, match="bad"):
        tasks.validate_dataset_version.run(str(uuid4()))
    assert closed == [True]


def test_backtest_task_closes_publisher_and_session_for_success_and_error(monkeypatch) -> None:
    closed: list[str] = []
    local_session = SimpleNamespace(close=lambda: closed.append("session"))

    class Publisher:
        def __init__(self, _settings) -> None:
            pass

        def close(self) -> None:
            closed.append("publisher")

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(storage_root=Path("/tmp")))
    monkeypatch.setattr(
        session_module, "create_session_factory", lambda _settings: lambda: local_session
    )
    monkeypatch.setattr(realtime, "RedisRunEventPublisher", Publisher)
    monkeypatch.setattr(
        backtest_application,
        "BacktestService",
        lambda *_args: SimpleNamespace(execute=lambda _id: {"status": "COMPLETED"}),
    )

    assert tasks.run_backtest.run(str(uuid4())) == {"status": "COMPLETED"}
    assert closed == ["session", "publisher"]

    closed.clear()
    monkeypatch.setattr(
        backtest_application,
        "BacktestService",
        lambda *_args: SimpleNamespace(
            execute=lambda _id: (_ for _ in ()).throw(RuntimeError("bad"))
        ),
    )
    with pytest.raises(RuntimeError, match="bad"):
        tasks.run_backtest.run(str(uuid4()))
    assert closed == ["session", "publisher"]
