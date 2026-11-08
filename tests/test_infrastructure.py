from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from app.api.dependencies import get_db_session
from app.core.errors import error_response
from app.core.logging import JsonFormatter, request_id_context
from app.db.base import Base
from app.db.session import create_database_engine, create_session_factory, get_session
from app.readiness import probe_dependencies
from tests.conftest import make_test_settings


def test_database_session_helpers_create_a_lazy_session() -> None:
    settings = make_test_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(settings)
    session_generator = get_session(factory)
    session = next(session_generator)

    assert "stockbot_test" in str(engine.url)
    assert session.bind is not None

    session_generator.close()
    engine.dispose()


def test_request_session_dependency_closes_the_request_scoped_session() -> None:
    closed: list[bool] = []
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_factory=lambda: SimpleNamespace(close=lambda: closed.append(True))
            )
        )
    )
    generator = get_db_session(request)
    next(generator)
    generator.close()

    assert closed == [True]


def test_metadata_registers_the_phase_five_tables() -> None:
    assert set(Base.metadata.tables) == {
        "algorithms",
        "algorithm_versions",
        "datasets",
        "dataset_versions",
        "dataset_upload_sessions",
        "backtest_runs",
        "job_dispatches",
        "signal_evaluations",
        "run_events",
        "run_metrics",
        "run_analytics",
        "run_analytics_periods",
        "trades",
        "trade_legs",
        "equity_points",
        "backtest_execution_attempts",
        "backtest_journal_events",
        "backtest_day_classifications",
        "backtest_export_jobs",
        "experiment_plans",
        "experiment_runs",
    }


def test_readiness_probe_checks_all_dependencies(monkeypatch) -> None:
    monkeypatch.setattr("app.readiness._database_check", lambda _: "ok")
    monkeypatch.setattr("app.readiness._redis_check", lambda _url, _timeout: "ok")

    checks = asyncio.run(probe_dependencies(make_test_settings()))

    assert checks == {"database": "ok", "celery_broker": "ok", "realtime_bus": "ok"}


def test_readiness_probe_marks_failed_checks_as_errors(monkeypatch) -> None:
    monkeypatch.setattr("app.readiness._database_check", lambda _: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("app.readiness._redis_check", lambda _url, _timeout: "ok")

    checks = asyncio.run(probe_dependencies(make_test_settings()))

    assert checks["database"] == "error"


def test_error_and_json_logging_include_request_context() -> None:
    request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))
    error = error_response(
        status_code=400,
        code="BAD_REQUEST",
        message="Request is invalid.",
        details={"field": "value"},
        request=request,
    )
    token = request_id_context.set("request-123")
    try:
        log = JsonFormatter().format(
            logging.LogRecord("stockbot", logging.INFO, __file__, 1, "ready", (), None)
        )
    finally:
        request_id_context.reset(token)

    assert json.loads(error.body)["request_id"] == "request-123"
    assert json.loads(log)["request_id"] == "request-123"
