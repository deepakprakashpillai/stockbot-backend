from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from app.api.schemas.backtests import (
    ComparisonResponse,
    ComparisonSeriesResponse,
    JsonExportResponse,
)
from app.api.v1 import backtest_analytics as analytics_routes
from app.api.v1 import backtests as routes
from app.application import results as results_module
from app.application.backtests import BacktestServiceError
from app.application.results import DEMO_NOTICE, EXPORT_ROW_LIMIT, ResultService
from app.core.metrics import MetricsRegistry
from app.db.repositories.backtests import BacktestRunFilters


def request() -> Request:
    value = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    value.state.request_id = "phase-7-results-test"
    return value


def metrics_request(registry: MetricsRegistry) -> Request:
    value = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(metrics=registry)),
        }
    )
    value.state.request_id = "phase-12-analytics-export-test"
    return value


def run(
    *,
    run_id: UUID | None = None,
    dataset_version_id: UUID | None = None,
    start_date: date = date(2026, 1, 2),
    end_date: date = date(2026, 1, 2),
    mode: str = "PLATFORM_DEMO",
    status: str = "COMPLETED",
    threshold: int = 2,
    code_version: str = "1.0.0-platform-demo",
) -> SimpleNamespace:
    dataset_id = dataset_version_id or uuid4()
    algorithm_version_id = uuid4()
    return SimpleNamespace(
        id=run_id or uuid4(),
        dataset_version_id=dataset_id,
        dataset_version=SimpleNamespace(
            version_number=1, dataset=SimpleNamespace(name="NIFTY fixture")
        ),
        algorithm_version_id=algorithm_version_id,
        algorithm_version=SimpleNamespace(
            algorithm_key="platform_demo",
            version_number=1,
            name="Default platform demo",
            code_version=code_version,
            algorithm=SimpleNamespace(display_name="Platform Demo"),
        ),
        start_date=start_date,
        end_date=end_date,
        mode=mode,
        starting_capital=Decimal("100000"),
        status=status,
        progress=100,
        cancellation_requested=False,
        error_code=None,
        error_message=None,
        snapshot={
            "dataset": {"id": "dataset", "checksum": "a" * 64},
            "algorithm": {
                "key": "platform_demo",
                "version_id": "version",
                "code_version": code_version,
                "parameters": {"threshold": threshold, "nested": {"hold": 3}},
            },
            "assumptions": {
                "demo_execution": {
                    "synthetic": True,
                    "unit": "DEMO_UNITS",
                    "financial_pnl_available": False,
                }
            },
            "engine_version": "1.0.0",
        },
        metrics=SimpleNamespace(
            metrics={
                "total_trades": 1,
                "expectancy_demo_units": "1.00000000",
                "mode": "PLATFORM_DEMO",
                "synthetic": True,
                "unit": "DEMO_UNITS",
                "financial_pnl_available": False,
            }
        ),
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


class Repository:
    def __init__(self, _session: object, runs: list[SimpleNamespace]) -> None:
        self.values = runs

    def get_runs(self, run_ids: list[UUID]) -> list[SimpleNamespace]:
        by_id = {value.id: value for value in self.values}
        return [by_id[run_id] for run_id in run_ids if run_id in by_id]

    def get_run(self, run_id: UUID) -> SimpleNamespace | None:
        return next((value for value in self.values if value.id == run_id), None)

    def count_trades(self, _run_id: UUID) -> int:
        return 1

    def list_trades(self, run_id: UUID, _cursor: int | None, _limit: int):
        entered = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
        leg = lambda number: SimpleNamespace(  # noqa: E731
            leg_number=number,
            name=f"Placeholder leg {number}",
            entry_reference=Decimal("100"),
            exit_reference=Decimal("101"),
            demo_units=Decimal("0.5"),
        )
        return [
            SimpleNamespace(
                run_id=run_id,
                sequence=1,
                entry_timestamp=entered,
                exit_timestamp=entered,
                exit_reason="DEMO_HOLD_INTERVAL_COMPLETE",
                quantity=Decimal("1"),
                entry_reference=Decimal("100"),
                exit_reference=Decimal("101"),
                gross_demo_units=Decimal("1"),
                net_demo_units=Decimal("1"),
                assumptions={"synthetic": True},
                legs=[leg(1), leg(2)],
            )
        ], None

    def count_signals(self, _run_id: UUID) -> int:
        return 0

    def list_signals(self, _run_id: UUID, _cursor: UUID | None, _limit: int):
        return [], None


def service(monkeypatch, values: list[SimpleNamespace]) -> ResultService:
    monkeypatch.setattr(
        results_module,
        "BacktestRepository",
        lambda session: Repository(session, values),
    )
    return ResultService(object())  # type: ignore[arg-type]


def test_comparison_returns_parameter_differences_and_safe_metrics(monkeypatch) -> None:
    dataset_version_id = uuid4()
    values = [
        run(dataset_version_id=dataset_version_id, threshold=2),
        run(dataset_version_id=dataset_version_id, threshold=4),
    ]
    result = service(monkeypatch, values).compare([value.id for value in values])
    response = ComparisonResponse.model_validate(result)

    assert response.parameter_differences == {
        "threshold": {str(values[0].id): 2, str(values[1].id): 4}
    }
    assert response.assumption_differences == {}
    assert all(item.unit == "DEMO_UNITS" for item in response.runs)
    assert all(item.financial_pnl_available is False for item in response.runs)
    assert "strategy performance" in response.notice


@pytest.mark.parametrize(
    ("replacement", "code", "allowed", "scope"),
    [
        ({"mode": "SIGNAL_ONLY"}, "COMPARISON_MODE_INCOMPATIBLE", False, "DIAGNOSTIC_ONLY"),
        ({"status": "RUNNING"}, "COMPARISON_RUN_NOT_COMPLETED", False, "DIAGNOSTIC_ONLY"),
        (
            {"dataset_version_id": uuid4()},
            "COMPARISON_DATASET_INCOMPATIBLE",
            True,
            "DIAGNOSTIC_ONLY",
        ),
        (
            {"start_date": date(2026, 1, 3), "end_date": date(2026, 1, 3)},
            "COMPARISON_RANGE_INCOMPATIBLE",
            True,
            "DIAGNOSTIC_ONLY",
        ),
    ],
)
def test_comparison_preflight_labels_incompatible_pairs(
    monkeypatch, replacement, code, allowed, scope
) -> None:
    dataset_version_id = uuid4()
    first = run(dataset_version_id=dataset_version_id)
    second = run(dataset_version_id=dataset_version_id)
    for key, value in replacement.items():
        setattr(second, key, value)

    preflight = service(monkeypatch, [first, second]).comparison_preflight([first.id, second.id])
    assert preflight["allowed"] is allowed
    assert preflight["comparison_scope"] == scope
    evidence = [*preflight["issues"], *preflight["warnings"]]
    assert code in {issue["code"] for issue in evidence}


def test_comparison_rejects_duplicates_and_reports_missing_runs(monkeypatch) -> None:
    value = run()
    result_service = service(monkeypatch, [value])
    with pytest.raises(BacktestServiceError) as duplicate:
        result_service.comparison_preflight([value.id, value.id])
    assert duplicate.value.code == "DUPLICATE_COMPARISON_RUN"

    missing = uuid4()
    preflight = result_service.comparison_preflight([value.id, missing])
    assert preflight["allowed"] is False
    assert preflight["issues"][0]["run_ids"] == [missing]


def test_comparison_series_rechecks_safety_and_returns_bounded_persisted_evidence(
    monkeypatch,
) -> None:
    dataset_version_id = uuid4()
    values = [
        run(dataset_version_id=dataset_version_id),
        run(dataset_version_id=dataset_version_id),
    ]
    base_time = datetime(2026, 1, 2, 9, 15, tzinfo=UTC)

    class SeriesRepository(Repository):
        def list_comparison_equity(self, run_id, *, start=None, end=None):
            return [
                SimpleNamespace(
                    timestamp=start_time,
                    sequence=index + 1,
                    equity_demo_units=Decimal(index - 4),
                    drawdown_demo_units=Decimal(-abs(index - 5)),
                )
                for index, start_time in enumerate(
                    base_time + timedelta(minutes=5 * index) for index in range(12)
                )
            ]

        def list_comparison_trades(self, run_id, *, start=None, end=None):
            return [
                SimpleNamespace(
                    exit_timestamp=base_time + timedelta(minutes=15),
                    sequence=1,
                    net_demo_units=Decimal("2.5"),
                )
            ]

        def list_comparison_signals(self, run_id, *, start=None, end=None):
            return [
                SimpleNamespace(
                    timestamp=base_time,
                    decision="ACCEPTED",
                    rejection_reason=None,
                ),
                SimpleNamespace(
                    timestamp=base_time + timedelta(minutes=5),
                    decision="REJECTED",
                    rejection_reason="DEMO_THRESHOLD_NOT_MET",
                ),
            ]

    monkeypatch.setattr(
        results_module,
        "BacktestRepository",
        lambda session: SeriesRepository(session, values),
    )
    result = ResultService(object()).comparison_series(
        [value.id for value in values],
        requested_series=[
            "equity",
            "drawdown",
            "trade_outcomes",
            "signal_decisions",
            "rejection_reasons",
        ],
        start=None,
        end=None,
        resolution="auto",
        max_points_per_run=10,
    )
    response = ComparisonSeriesResponse.model_validate(result)

    assert len(response.comparison_hash) == 64
    assert response.resolution == "envelope"
    assert all(len(item.equity) <= 10 for item in response.runs)
    assert response.runs[0].equity[0].timestamp == base_time
    assert response.runs[0].equity[-1].sequence == 12
    assert response.runs[0].signal_decisions[0].category == "ACCEPTED"
    assert response.category_definitions["rejection_reasons"] == ["DEMO_THRESHOLD_NOT_MET"]


def test_analytics_definitions_publish_safe_research_vocabulary(monkeypatch) -> None:
    run_id = uuid4()

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, _id: UUID):
            return SimpleNamespace(id=run_id)

        def get_analytics(self, _id: UUID):
            return SimpleNamespace(
                schema_version="stockbot.run-analytics.v1", timezone="Asia/Kolkata"
            )

    monkeypatch.setattr(analytics_routes, "BacktestRepository", Repository)
    definitions = analytics_routes.get_analytics_definitions(run_id, request(), object())

    assert definitions.schema_version == "stockbot.run-analytics.v1"
    assert definitions.safeguards.unit == "DEMO_UNITS"
    assert definitions.safeguards.financial_pnl_available is False
    assert {item.key for item in definitions.items} >= {
        "gross_outcome",
        "net_outcome",
        "profit_factor",
        "max_drawdown_amount",
    }
    assert all(item.unit_semantics != "INR" for item in definitions.items)

    class OptionSimulationRepository(Repository):
        def get_run(self, _id: UUID):
            return SimpleNamespace(id=run_id, mode="OPTION_SPREAD_SIMULATION")

    monkeypatch.setattr(analytics_routes, "BacktestRepository", OptionSimulationRepository)
    option_definitions = analytics_routes.get_analytics_definitions(run_id, request(), object())
    assert option_definitions.safeguards.unit == "INR"
    assert option_definitions.safeguards.financial_pnl_available is True
    outcome_units = {
        item.unit_semantics
        for item in option_definitions.items
        if item.key in {"gross_outcome", "net_outcome", "max_drawdown_amount"}
    }
    assert outcome_units == {"INR"}

    class MissingAnalyticsRepository(Repository):
        def get_analytics(self, _id: UUID):
            return None

    monkeypatch.setattr(analytics_routes, "BacktestRepository", MissingAnalyticsRepository)
    unavailable = analytics_routes.get_analytics_definitions(run_id, request(), object())
    assert unavailable.status_code == 409


def test_json_exports_embed_identity_snapshot_and_safeguards(monkeypatch) -> None:
    value = run()
    result_service = service(monkeypatch, [value])

    for kind in ("summary", "trades", "signals"):
        payload = result_service.export(value.id, kind)
        response = JsonExportResponse.model_validate(payload)
        assert response.metadata.run_id == value.id
        assert response.metadata.snapshot == value.snapshot
        assert response.metadata.notice == DEMO_NOTICE
        assert response.metadata.unit == "DEMO_UNITS"
        assert response.metadata.financial_pnl_available is False
        assert "INR" not in json.dumps(payload, default=str)


def test_selected_analytics_export_reconciles_and_carries_research_context(monkeypatch) -> None:
    value = run(start_date=date(2026, 1, 1), end_date=date(2026, 2, 28))

    class AnalyticsRepository(Repository):
        def get_analytics(self, _run_id: UUID):
            return SimpleNamespace(
                schema_version="stockbot.run-analytics.v1",
                calculator_version="1.0.0",
                source_fingerprint="a" * 64,
                timezone="Asia/Kolkata",
                summary={"total_trades": 2, "net_outcome": "3.00"},
            )

        def list_analytics_periods(self, _run_id, granularity, _cursor, _limit, **filters):
            assert granularity == "DAY"
            assert filters["include_zero_periods"] is True
            assert filters["sort"] == "PERIOD_ASC"
            rows = [
                SimpleNamespace(
                    period_start=date(2026, 1, 2),
                    period_end=date(2026, 1, 2),
                    partial=False,
                    eligible_session_count=1,
                    trade_count=1,
                    wins=1,
                    losses=0,
                    breakevens=0,
                    gross_demo_units=Decimal("1.50"),
                    costs_demo_units=Decimal("0"),
                    net_demo_units=Decimal("1.50"),
                    cumulative_net_demo_units=Decimal("1.50"),
                    equity_demo_units=Decimal("1.50"),
                    local_max_drawdown_demo_units=Decimal("0"),
                    run_drawdown_demo_units=Decimal("0"),
                ),
                SimpleNamespace(
                    period_start=date(2026, 2, 2),
                    period_end=date(2026, 2, 2),
                    partial=False,
                    eligible_session_count=1,
                    trade_count=1,
                    wins=1,
                    losses=0,
                    breakevens=0,
                    gross_demo_units=Decimal("1.50"),
                    costs_demo_units=Decimal("0"),
                    net_demo_units=Decimal("1.50"),
                    cumulative_net_demo_units=Decimal("3.00"),
                    equity_demo_units=Decimal("3.00"),
                    local_max_drawdown_demo_units=Decimal("0"),
                    run_drawdown_demo_units=Decimal("0"),
                ),
            ]
            return rows, None, len(rows)

    monkeypatch.setattr(
        results_module,
        "BacktestRepository",
        lambda _session: AnalyticsRepository(_session, [value]),
    )
    payload = ResultService(object()).export(value.id, "analytics", granularity="DAY")
    response = JsonExportResponse.model_validate(payload)

    assert response.metadata.research_view is not None
    assert response.metadata.research_view.granularity == "DAY"
    assert response.metadata.research_view.reconciliation == "PASSED"
    assert response.metadata.research_view.attribution == "EXIT_TIMESTAMP"
    assert [row["period_start"] for row in response.rows] == ["2026-01-02", "2026-02-02"]


def test_selected_analytics_export_rejects_unreconciled_full_view(monkeypatch) -> None:
    value = run()

    class UnreconciledRepository(Repository):
        def get_analytics(self, _run_id: UUID):
            return SimpleNamespace(
                schema_version="stockbot.run-analytics.v1",
                calculator_version="1.0.0",
                source_fingerprint="a" * 64,
                timezone="Asia/Kolkata",
                summary={"total_trades": 1, "net_outcome": "9.00"},
            )

        def list_analytics_periods(self, *_args, **_kwargs):
            return [], None, 0

    monkeypatch.setattr(
        results_module,
        "BacktestRepository",
        lambda _session: UnreconciledRepository(_session, [value]),
    )
    with pytest.raises(BacktestServiceError) as failure:
        ResultService(object()).export(value.id, "analytics", granularity="MONTH")
    assert failure.value.code == "ANALYTICS_EXPORT_RECONCILIATION_FAILED"


def test_selected_analytics_export_records_bounded_success_and_failure_metrics(monkeypatch) -> None:
    value = run()
    payload = {
        "metadata": {
            "schema_version": "stockbot.platform-demo-export.v1",
            "exported_at": datetime.now(UTC),
            "kind": "analytics",
            "run_id": value.id,
            "mode": "PLATFORM_DEMO",
            "synthetic": True,
            "unit": "DEMO_UNITS",
            "financial_pnl_available": False,
            "notice": DEMO_NOTICE,
            "snapshot": value.snapshot,
            "research_view": {
                "granularity": "DAY",
                "start_date": None,
                "end_date": None,
                "include_zero_periods": True,
                "sort": "PERIOD_ASC",
                "schema_version": "stockbot.run-analytics.v1",
                "calculator_version": "1.0.0",
                "source_fingerprint": "a" * 64,
                "timezone": "Asia/Kolkata",
                "attribution": "EXIT_TIMESTAMP",
                "reconciliation": "PASSED",
            },
        },
        "rows": [],
    }
    registry = MetricsRegistry()
    monkeypatch.setattr(
        routes,
        "ResultService",
        lambda _session: SimpleNamespace(export=lambda *_args, **_kwargs: payload),
    )
    response = routes.export_backtest(
        value.id,
        "analytics",
        metrics_request(registry),
        "json",
        object(),
        granularity="DAY",
    )
    assert response.status_code == 200
    assert (
        'stockbot_analytics_export_duration_seconds_count{format="json",granularity="DAY"} 1'
        in registry.render()
    )

    monkeypatch.setattr(
        routes,
        "ResultService",
        lambda _session: SimpleNamespace(
            export=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                BacktestServiceError("ANALYTICS_EXPORT_RECONCILIATION_FAILED", "blocked", 409)
            )
        ),
    )
    failed = routes.export_backtest(
        value.id,
        "analytics",
        metrics_request(registry),
        "json",
        object(),
        granularity="DAY",
    )
    assert failed.status_code == 409
    rendered = registry.render()
    assert (
        'stockbot_analytics_export_failures_total{code="ANALYTICS_EXPORT_RECONCILIATION_FAILED",granularity="DAY"} 1'
        in rendered
    )
    assert str(value.id) not in rendered


def test_export_rejects_non_demo_incomplete_and_excessively_large_runs(monkeypatch) -> None:
    incomplete = run(status="RUNNING")
    with pytest.raises(BacktestServiceError) as failure:
        service(monkeypatch, [incomplete]).export(incomplete.id, "summary")
    assert failure.value.code == "EXPORT_RUN_NOT_COMPLETED"

    non_demo = run(mode="SIGNAL_ONLY")
    with pytest.raises(BacktestServiceError) as failure:
        service(monkeypatch, [non_demo]).export(non_demo.id, "summary")
    assert failure.value.code == "RESULT_MODE_UNSUPPORTED"

    with pytest.raises(BacktestServiceError) as failure:
        ResultService._check_export_limit(EXPORT_ROW_LIMIT + 1)
    assert failure.value.details["maximum"] == EXPORT_ROW_LIMIT


def test_catalog_route_passes_all_filters_and_keeps_total(monkeypatch) -> None:
    value = run()
    captured: dict[str, object] = {}

    class CatalogRepository:
        def __init__(self, _session: object) -> None:
            pass

        def list_runs(self, filters, cursor, limit):
            captured.update(filters=filters, cursor=cursor, limit=limit)
            return [value], None

        def count_runs(self, filters):
            assert filters is captured["filters"]
            return 37

    monkeypatch.setattr(routes, "BacktestRepository", CatalogRepository)
    response = routes.list_backtests(
        status="COMPLETED",
        dataset_id=uuid4(),
        dataset_version_id=value.dataset_version_id,
        algorithm_key="platform_demo",
        algorithm_version_id=value.algorithm_version_id,
        mode="PLATFORM_DEMO",
        start_date=value.start_date,
        end_date=value.end_date,
        created_from=datetime(2026, 1, 1, tzinfo=UTC),
        created_to=datetime(2026, 2, 1, tzinfo=UTC),
        cursor=None,
        limit=999,
        session=object(),  # type: ignore[arg-type]
    )
    assert response.total_count == 37
    assert response.items[0].dataset_name == "NIFTY fixture"
    assert response.items[0].algorithm_display_name == "Platform Demo"
    assert isinstance(captured["filters"], BacktestRunFilters)
    assert captured["limit"] == 100


def test_csv_route_includes_metadata_even_for_empty_result_collection(monkeypatch) -> None:
    value = run()
    payload = service(monkeypatch, [value]).export(value.id, "signals")
    monkeypatch.setattr(
        routes,
        "ResultService",
        lambda _session: SimpleNamespace(export=lambda _run_id, _kind: payload),
    )
    response = routes.export_backtest(value.id, "signals", request(), "csv", object())
    assert response.media_type.startswith("text/csv")
    content = response.body.decode()
    assert "EXPORT_METADATA_ONLY" in content
    assert str(value.id) in content
    assert "DEMO_UNITS" in content


def test_diagnostics_group_entry_times_in_ist_and_sort_numeric_categories(monkeypatch) -> None:
    value = run(mode="OPTION_SPREAD_SIMULATION")
    signal_two = SimpleNamespace(id=uuid4(), score=2, rejection_reason="CONFIRMATION_SCORE_TOO_LOW")
    signal_ten = SimpleNamespace(id=uuid4(), score=10, rejection_reason=None)
    trade = SimpleNamespace(
        entry_signal_evaluation_id=signal_ten.id,
        entry_timestamp=datetime(2026, 1, 2, 4, 0, tzinfo=UTC),
        exit_reason="TARGET_REACHED",
        assumptions={"expiry": "2026-01-02"},
        net_demo_units=Decimal("100"),
        gross_demo_units=Decimal("500"),
        costs_demo_units=Decimal("400"),
        entry_reference=Decimal("80"),
        exit_reference=Decimal("90"),
        quantity=Decimal("75"),
    )

    class DiagnosticsRepository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, _run_id: UUID):
            return value

        def count_signals(self, _run_id: UUID) -> int:
            return 2

        def count_trades(self, _run_id: UUID) -> int:
            return 1

        def list_signals(self, *_args):
            return [signal_ten, signal_two], None

        def list_trades(self, *_args):
            return [trade], None

    monkeypatch.setattr(analytics_routes, "BacktestRepository", DiagnosticsRepository)
    response = analytics_routes.get_run_diagnostics(value.id, request(), object())

    assert response.unit == "INR"
    assert [item.category for item in response.groups["confirmation_score"]] == ["2", "10"]
    assert response.groups["entry_time"][0].category == "09:30"
    assert response.groups["days_to_expiry"][0].category == "0"
