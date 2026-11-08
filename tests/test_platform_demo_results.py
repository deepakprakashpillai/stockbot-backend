from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.api.schemas.backtests import (
    AnalyticsPeriodResponse,
    DemoEquityPointResponse,
    DemoTradeResponse,
)
from app.api.v1 import backtest_analytics as analytics_routes
from app.api.v1 import backtests as routes
from app.db.models.backtests import EquityPoint, Trade, TradeLeg
from app.db.repositories.backtests import BacktestRepository


def request() -> Request:
    value = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    value.state.request_id = "platform-demo-results-test"
    return value


def result_records() -> tuple[SimpleNamespace, SimpleNamespace]:
    entered = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
    legs = [
        SimpleNamespace(
            id=uuid4(),
            leg_number=number,
            name=f"Placeholder leg {number}",
            entry_reference=Decimal("100.00000000"),
            exit_reference=Decimal("101.00000000"),
            demo_units=Decimal("0.50000000"),
            synthetic=True,
            unit="DEMO_UNITS",
            financial_pnl_available=False,
        )
        for number in (1, 2)
    ]
    trade = SimpleNamespace(
        id=uuid4(),
        sequence=1,
        entry_timestamp=entered,
        exit_timestamp=entered + timedelta(minutes=15),
        exit_reason="DEMO_HOLD_COMPLETE",
        quantity=Decimal("1.00000000"),
        entry_reference=Decimal("100.00000000"),
        exit_reference=Decimal("101.00000000"),
        gross_demo_units=Decimal("1.00000000"),
        net_demo_units=Decimal("1.00000000"),
        synthetic=True,
        unit="DEMO_UNITS",
        financial_pnl_available=False,
        assumptions={"adapter": "platform_demo"},
        legs=legs,
    )
    point = SimpleNamespace(
        id=uuid4(),
        sequence=1,
        timestamp=trade.exit_timestamp,
        equity_demo_units=Decimal("1.00000000"),
        drawdown_demo_units=Decimal("0.00000000"),
        synthetic=True,
        unit="DEMO_UNITS",
        financial_pnl_available=False,
    )
    return trade, point


def test_result_routes_return_ordered_safeguarded_shapes(monkeypatch) -> None:
    run_id = uuid4()
    trade, point = result_records()

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, requested_run_id):
            return SimpleNamespace(id=requested_run_id)

        def list_trades(self, requested_run_id, cursor, limit):
            assert (requested_run_id, cursor, limit) == (run_id, None, 200)
            return [trade], None

        def list_equity_points(self, requested_run_id, cursor, limit):
            assert (requested_run_id, cursor, limit) == (run_id, None, 1000)
            return [point], None

    monkeypatch.setattr(routes, "BacktestRepository", Repository)
    trades = routes.list_trades(run_id, request(), None, 999, object())
    equity = routes.list_equity(run_id, request(), None, 9999, object())

    assert trades.items[0].legs[0].leg_number == 1  # type: ignore[union-attr]
    assert len(trades.items[0].legs) == 2  # type: ignore[union-attr]
    assert trades.items[0].synthetic is True  # type: ignore[union-attr]
    assert trades.items[0].unit == "DEMO_UNITS"  # type: ignore[union-attr]
    assert trades.items[0].financial_pnl_available is False  # type: ignore[union-attr]
    assert equity.items[0].drawdown_demo_units == Decimal("0")  # type: ignore[union-attr]
    assert equity.items[0].financial_pnl_available is False  # type: ignore[union-attr]


def test_trade_route_applies_exchange_date_filters_and_deterministic_sort(monkeypatch) -> None:
    run_id = uuid4()
    trade, _ = result_records()

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, requested_run_id):
            assert requested_run_id == run_id
            return SimpleNamespace(id=requested_run_id)

        def get_analytics(self, requested_run_id):
            assert requested_run_id == run_id
            return SimpleNamespace(timezone="Asia/Kolkata")

        def list_trades(self, requested_run_id, cursor, limit, *, filters, sort):
            assert requested_run_id == run_id
            assert cursor is None
            assert limit == 100
            assert filters.outcome == "WIN"
            assert filters.exit_reason == "DEMO_HOLD_COMPLETE"
            assert filters.exit_from == datetime(2026, 1, 1, 18, 30, tzinfo=UTC)
            assert filters.exit_to == datetime(2026, 1, 2, 18, 30, tzinfo=UTC)
            assert sort == "EXIT_TIMESTAMP_ASC"
            return [trade], None

    monkeypatch.setattr(routes, "BacktestRepository", Repository)
    response = routes.list_trades(
        run_id,
        request(),
        session=object(),
        exit_date_from=date(2026, 1, 2),
        exit_date_to=date(2026, 1, 2),
        outcome="WIN",
        exit_reason="DEMO_HOLD_COMPLETE",
        sort="EXIT_TIMESTAMP_ASC",
    )

    assert response.items[0].sequence == 1  # type: ignore[union-attr]


def test_trade_analytics_periods_are_chronological_and_cursor_bounded(monkeypatch) -> None:
    run_id = uuid4()
    first, _ = result_records()
    first = SimpleNamespace(**(vars(first) | {"costs_demo_units": Decimal("0")}))
    second = SimpleNamespace(
        **(
            vars(first)
            | {
                "sequence": 2,
                "entry_timestamp": first.entry_timestamp + timedelta(minutes=20),
                "exit_timestamp": first.exit_timestamp + timedelta(minutes=20),
                "gross_demo_units": Decimal("-2"),
                "costs_demo_units": Decimal("1"),
                "net_demo_units": Decimal("-3"),
                "exit_reason": "DEMO_STOP",
            }
        )
    )

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, requested_run_id):
            assert requested_run_id == run_id
            return SimpleNamespace(id=requested_run_id)

        def get_analytics(self, requested_run_id):
            assert requested_run_id == run_id
            return SimpleNamespace(timezone="Asia/Kolkata")

        def list_trades_chronological(self, requested_run_id):
            assert requested_run_id == run_id
            return [second, first]

    monkeypatch.setattr(analytics_routes, "BacktestRepository", Repository)
    response = analytics_routes.list_analytics_periods(
        run_id,
        request(),
        granularity="TRADE",
        purpose="table",
        limit=1,
        session=object(),
    )

    assert [item.sequence for item in response.items] == [1]  # type: ignore[union-attr]
    assert response.items[0].cumulative_net_outcome == Decimal("1")  # type: ignore[union-attr]
    assert response.next_cursor == 1  # type: ignore[union-attr]


def test_chart_rollups_keep_exact_period_totals_and_final_equity() -> None:
    def period(day: int, net: str, cumulative: str) -> AnalyticsPeriodResponse:
        value = date(2026, 1, day)
        return AnalyticsPeriodResponse(
            granularity="DAY",
            period_start=value,
            period_end=value,
            partial=False,
            eligible_session_count=1,
            trade_count=1,
            wins=int(Decimal(net) > 0),
            losses=int(Decimal(net) < 0),
            breakevens=0,
            gross_outcome=Decimal(net),
            costs=Decimal("0"),
            net_outcome=Decimal(net),
            cumulative_net_outcome=Decimal(cumulative),
            equity_end=Decimal(cumulative),
            local_max_drawdown=min(Decimal("0"), Decimal(net)),
            run_drawdown_end=min(Decimal("0"), Decimal(net)),
        )

    values, bucket_size = analytics_routes._aggregate_analytics_periods(
        [period(2, "1.1", "1.1"), period(3, "-0.2", "0.9"), period(4, "2", "2.9")],
        max_points=2,
    )

    assert bucket_size == 2
    assert len(values) == 2
    assert values[0].net_outcome == Decimal("0.9")
    assert values[0].cumulative_net_outcome == Decimal("0.9")
    assert values[1].equity_end == Decimal("2.9")


def test_period_table_forwards_zero_visibility_and_descending_order(monkeypatch) -> None:
    run_id = uuid4()
    value = AnalyticsPeriodResponse(
        granularity="DAY",
        period_start=date(2026, 1, 3),
        period_end=date(2026, 1, 3),
        partial=False,
        eligible_session_count=1,
        trade_count=1,
        wins=1,
        losses=0,
        breakevens=0,
        gross_outcome=Decimal("2"),
        costs=Decimal("0"),
        net_outcome=Decimal("2"),
        cumulative_net_outcome=Decimal("2"),
        equity_end=Decimal("2"),
        local_max_drawdown=Decimal("0"),
        run_drawdown_end=Decimal("0"),
    )

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, requested_run_id):
            assert requested_run_id == run_id
            return SimpleNamespace(id=requested_run_id)

        def get_analytics(self, requested_run_id):
            assert requested_run_id == run_id
            return SimpleNamespace(timezone="Asia/Kolkata")

        def list_analytics_periods(self, *args, **kwargs):
            assert args == (run_id, "DAY", None, 25)
            assert kwargs == {
                "start_date": None,
                "end_date": None,
                "include_zero_periods": False,
                "sort": "PERIOD_DESC",
            }
            return [value], None, 1

    monkeypatch.setattr(analytics_routes, "BacktestRepository", Repository)
    monkeypatch.setattr(analytics_routes, "_analytics_period", lambda item: item)
    response = analytics_routes.list_analytics_periods(
        run_id,
        request(),
        granularity="DAY",
        purpose="table",
        limit=25,
        include_zero_periods=False,
        sort="PERIOD_DESC",
        session=object(),
    )

    assert response.items == [value]  # type: ignore[union-attr]


@pytest.mark.parametrize("route", [routes.list_trades, routes.list_equity])
def test_result_routes_return_404_when_run_is_missing(monkeypatch, route) -> None:
    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_run(self, _run_id):
            return None

    monkeypatch.setattr(routes, "BacktestRepository", Repository)
    response = route(uuid4(), request(), None, 10, object())
    assert response.status_code == 404


def test_response_contract_rejects_missing_legs_or_unsafe_markers() -> None:
    trade, point = result_records()
    trade_values = vars(trade) | {"legs": trade.legs[:1]}
    with pytest.raises(ValidationError):
        DemoTradeResponse.model_validate(trade_values)

    point_values = vars(point) | {"unit": "UNSUPPORTED_UNIT"}
    with pytest.raises(ValidationError):
        DemoEquityPointResponse.model_validate(point_values)


def test_repository_paginates_results_by_ordered_sequence() -> None:
    run_id = uuid4()
    trades = [SimpleNamespace(sequence=1), SimpleNamespace(sequence=2)]
    points = [SimpleNamespace(sequence=1), SimpleNamespace(sequence=2)]

    class Session:
        def __init__(self) -> None:
            self.values = [trades, points]

        def scalars(self, _statement):
            return self.values.pop(0)

    repository = BacktestRepository(Session())  # type: ignore[arg-type]
    trade_items, trade_cursor = repository.list_trades(run_id, None, 1)
    point_items, point_cursor = repository.list_equity_points(run_id, None, 1)

    assert [item.sequence for item in trade_items] == [1]
    assert trade_cursor == 1
    assert [item.sequence for item in point_items] == [1]
    assert point_cursor == 1


def test_repository_reads_bounded_comparison_source_records_in_chronological_order() -> None:
    run_id = uuid4()
    timestamp = datetime(2026, 1, 2, 9, 15, tzinfo=UTC)
    points = [SimpleNamespace(sequence=1, timestamp=timestamp)]
    trades = [SimpleNamespace(sequence=1, exit_timestamp=timestamp)]
    signals = [SimpleNamespace(sequence=1, timestamp=timestamp)]

    class Session:
        def __init__(self) -> None:
            self.values = [points, trades, signals]

        def scalars(self, _statement):
            return self.values.pop(0)

    repository = BacktestRepository(Session())  # type: ignore[arg-type]
    start = timestamp - timedelta(minutes=1)
    end = timestamp + timedelta(minutes=1)

    assert repository.list_comparison_equity(run_id, start=start, end=end) == points
    assert repository.list_comparison_trades(run_id, start=start, end=end) == trades
    assert repository.list_comparison_signals(run_id, start=start, end=end) == signals


def test_result_tables_include_database_safeguards() -> None:
    assert {constraint.name for constraint in Trade.__table__.constraints} >= {
        "ck_trades_unit",
        "uq_trades_run_sequence",
    }
    assert {constraint.name for constraint in TradeLeg.__table__.constraints} >= {
        "ck_trade_legs_placeholder_number",
        "ck_trade_legs_unit",
        "uq_trade_legs_number",
    }
    assert {constraint.name for constraint in EquityPoint.__table__.constraints} >= {
        "ck_equity_points_unit",
        "uq_equity_points_run_sequence",
    }
