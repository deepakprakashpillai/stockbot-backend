from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.application.backtest_coverage import analyze_derivatives_coverage
from app.application.backtests import BacktestService
from app.core.metrics import MetricsRegistry
from app.core.security import rate_limit_policy
from app.db.repositories.backtests import BacktestRepository
from app.domain.algorithms import BULL_CALL_SPREAD, BullCallSpreadHardGateParameters
from app.domain.backtests import WilderRsi, _rsi
from app.domain.derivatives_provider import ParquetDerivativesProvider
from app.domain.market_data_sources import ExecutableOptionQuote, executable_quote_coverage
from app.infrastructure.datasets.derivatives_bundle import (
    build_derivatives_bundle,
    load_derivatives_bundle,
    verify_derivatives_bundle_files,
)
from app.main import create_app
from tests.conftest import make_test_settings
from tests.market_fixture import IST, market_bundle


def coverage(bundle, parameters=None, **kwargs):
    return analyze_derivatives_coverage(
        bundle_dir=bundle,
        spot_path=bundle / "spot/nifty_spot_5min.csv",
        start_date=date(2025, 7, 21),
        end_date=date(2025, 7, 21),
        parameters=parameters or BullCallSpreadHardGateParameters(),
        algorithm=BULL_CALL_SPREAD,
        **kwargs,
    )


@pytest.mark.parametrize("day,hour,minute", [(21, 15, 0), (21, 10, 45), (18, 12, 0)])
def test_coverage_rejects_missing_clock_even_when_other_files_agree(tmp_path, day, hour, minute):
    bundle = market_bundle(tmp_path, missing=datetime(2025, 7, day, hour, minute, tzinfo=IST))
    result = coverage(bundle)
    assert result["coverage_status"] == "BLOCKED"
    assert result["excluded_sessions"][0]["reason_code"] == "SESSION_CLOCK_INCOMPLETE"


def test_coverage_binds_parameters_and_saved_version_identity(tmp_path):
    bundle = market_bundle(tmp_path)
    original = coverage(bundle, dataset_version_id="a", algorithm_version_id="one")
    assert original["coverage_status"] == "COMPLETE"
    for parameters, dataset, version in [
        (BullCallSpreadHardGateParameters(lots=2), "a", "one"),
        (None, "b", "one"),
        (None, "a", "two"),
    ]:
        assert (
            coverage(bundle, parameters, dataset_version_id=dataset, algorithm_version_id=version)[
                "coverage_digest"
            ]
            != original["coverage_digest"]
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("lot_size", None),
        ("lot_size", 0),
        ("lot_size", -1),
        ("lot_size", 1.5),
        ("tick_size", None),
        ("tick_size", -1),
    ],
)
def test_contract_metadata_is_never_invented(tmp_path, key, value):
    bundle = market_bundle(tmp_path)
    path = bundle / "contracts_master.json"
    rows = json.loads(path.read_text())
    rows[0][key] = value
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        ParquetDerivativesProvider(bundle)


def test_immutable_builder_does_not_replace_registered_bytes(tmp_path):
    bundle = market_bundle(tmp_path)
    manifest = bundle / "bundle_manifest.json"
    before = manifest.read_bytes()
    build_derivatives_bundle(bundle, bundle / "spot/nifty_spot_5min.csv")
    assert manifest.read_bytes() == before
    with pytest.raises(ValueError, match="immutable bundle"):
        build_derivatives_bundle(bundle, tmp_path / "new-source.csv")
    assert manifest.read_bytes() == before


def test_verifier_cannot_validate_another_path_than_reader(tmp_path):
    bundle = market_bundle(tmp_path)
    manifest = load_derivatives_bundle(bundle)
    manifest["components"]["spot"]["path"] = "../escape.csv"
    with pytest.raises(ValueError, match="component path"):
        verify_derivatives_bundle_files(bundle, manifest)


def test_metrics_storage_is_constant_and_emitted_totals_are_correct():
    metrics = MetricsRegistry()
    for _ in range(10000):
        metrics.observe("latency", 0.5)
    assert "latency_count 10000" in metrics.render()
    assert "latency_sum 5000" in metrics.render()
    assert len(next(iter(metrics._histograms.values()))) == 2


def test_cancellation_before_execution_releases_the_newly_claimed_lease():
    run = SimpleNamespace(
        id=uuid4(),
        status="PENDING",
        cancellation_requested=True,
        lease_owner="claimed-worker",
        lease_expires_at=datetime.now(),
        progress=0,
    )
    service = object.__new__(BacktestService)
    service.session = SimpleNamespace(commit=lambda: None)
    service.runs = SimpleNamespace(claim_lease=lambda *_args: run, append_event=lambda *_args: None)
    assert service.execute(run.id)["status"] == "CANCELLED"
    assert run.progress == 100
    assert run.lease_owner is None and run.lease_expires_at is None


def test_failed_session_dependency_returns_controlled_unavailable_response():
    settings = make_test_settings().model_copy(update={"authentication_required": True})
    app = create_app(settings=settings)

    class OfflineStore:
        async def authenticate_session(self, _token):
            raise ConnectionError("session store unavailable")

    app.state.realtime_ticket_store = OfflineStore()
    with TestClient(app) as client:
        client.cookies.set("stockbot_session", "old-token")
        response = client.get("/api/v1/backtests", headers={"X-Request-ID": "x" * 1000})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SECURITY_DEPENDENCY_UNAVAILABLE"
        assert len(response.headers["X-Request-ID"]) <= 128


def test_liveness_with_cookie_never_calls_session_dependency():
    settings = make_test_settings().model_copy(update={"authentication_required": True})
    app = create_app(settings=settings)

    class OfflineStore:
        async def authenticate_session(self, token):
            raise AssertionError("Liveness must not call Redis")

    app.state.realtime_ticket_store = OfflineStore()
    with TestClient(app) as client:
        client.cookies.set("stockbot_session", "expired")
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/backtests/x/exports/signals", "GET"),
        ("/api/v1/backtests/export-jobs/x/download", "GET"),
        ("/api/v1/experiments", "POST"),
        ("/api/v1/experiments/x/start", "POST"),
        ("/api/v1/realtime/tickets", "POST"),
    ],
)
def test_expensive_routes_have_server_side_budgets(path, method):
    assert rate_limit_policy(path, method, make_test_settings()) is not None


def test_iterator_quote_validation_matches_list():
    quote = ExecutableOptionQuote(
        "TEST", datetime.now(IST), Decimal("20"), Decimal("10"), 1, 1, "fixture"
    )
    requested = [(quote.contract_symbol, quote.timestamp)]
    result = executable_quote_coverage(requested, iter([quote]))
    assert result == executable_quote_coverage(requested, [quote])
    assert result["complete"] is False


def test_incremental_rsi_preserves_reference_values_for_every_prefix():
    closes = [100.0 + ((i * 37) % 19) - ((i * 11) % 13) for i in range(200)]
    for period in (2, 14, 100):
        indicator = WilderRsi(period)
        for index, close in enumerate(closes):
            assert indicator.update(close) == _rsi(closes[: index + 1], period)


@pytest.mark.parametrize("value", [time(9, 30, 1), time(9, 30, tzinfo=IST)])
def test_session_times_cannot_hide_seconds_or_offsets(value):
    with pytest.raises(ValidationError):
        BullCallSpreadHardGateParameters(entry_window_1_start=value)


def test_terminal_task_delivery_does_not_mutate_lease():
    run = SimpleNamespace(status="COMPLETED", lease_owner=None, lease_expires_at=None)
    repository = BacktestRepository(SimpleNamespace())
    repository.get_run = lambda *_a, **_k: run
    assert repository.claim_lease(uuid4(), "new-worker") is None
    assert run.lease_owner is None
