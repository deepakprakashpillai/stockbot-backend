from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from random import Random
from types import SimpleNamespace

from app.application import analytics as analytics_application
from app.db.models.backtests import RunAnalytics
from app.domain.run_analytics import ClosedOutcome, calculate_run_analytics, calculate_trade_periods

IST = timezone(timedelta(hours=5, minutes=30))


def outcome(sequence: int, day: int, net: str) -> ClosedOutcome:
    exit_timestamp = datetime(2026, 1 if day < 30 else 2, day if day < 30 else 1, 15, tzinfo=IST)
    return ClosedOutcome(
        sequence=sequence,
        entry_timestamp=exit_timestamp - timedelta(minutes=10),
        exit_timestamp=exit_timestamp,
        gross=Decimal(net) + Decimal("1"),
        costs=Decimal("1"),
        net=Decimal(net),
    )


def test_close_date_periods_reconcile_and_keep_zero_sessions() -> None:
    result = calculate_run_analytics(
        [outcome(1, 2, "4"), outcome(2, 3, "-5")],
        [date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)],
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 4),
    )

    assert [item.trade_count for item in result.daily] == [1, 1, 0]
    assert sum((item.net for item in result.daily), Decimal("0")) == Decimal("-1")
    assert sum((item.net for item in result.monthly), Decimal("0")) == Decimal("-1")
    assert result.summary["net_outcome"] == Decimal("-1")
    assert result.summary["max_drawdown_amount"] == Decimal("5")
    assert result.summary["profit_factor"] == {
        "value": Decimal("0.8"),
        "availability": "AVAILABLE",
        "reason": None,
    }


def test_ratios_are_honest_when_no_losing_trade_exists() -> None:
    result = calculate_run_analytics(
        [outcome(1, 2, "4")],
        [date(2026, 1, 2)],
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )

    assert result.summary["profit_factor"] == {
        "value": None,
        "availability": "UNAVAILABLE",
        "reason": "NO_LOSING_TRADES",
    }


def test_drawdown_is_trade_level_and_recovers_within_one_session() -> None:
    opened = datetime(2026, 1, 2, 9, 15, tzinfo=IST)
    outcomes = [
        ClosedOutcome(
            1, opened, opened + timedelta(minutes=10), Decimal("11"), Decimal("1"), Decimal("10")
        ),
        ClosedOutcome(
            2, opened, opened + timedelta(minutes=20), Decimal("1"), Decimal("1"), Decimal("0")
        ),
        ClosedOutcome(
            3, opened, opened + timedelta(minutes=30), Decimal("-5"), Decimal("1"), Decimal("-6")
        ),
        ClosedOutcome(
            4, opened, opened + timedelta(minutes=45), Decimal("8"), Decimal("1"), Decimal("7")
        ),
    ]

    result = calculate_run_analytics(
        outcomes,
        [date(2026, 1, 2)],
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )

    assert result.daily[0].run_drawdown_end == Decimal("0")
    assert result.daily[0].local_max_drawdown == Decimal("-6")
    assert result.monthly[0].local_max_drawdown == Decimal("-6")
    assert result.summary["max_drawdown_amount"] == Decimal("6")
    assert result.summary["drawdown_peak_timestamp"] == opened + timedelta(minutes=10)
    assert result.summary["drawdown_trough_timestamp"] == opened + timedelta(minutes=30)
    assert result.summary["drawdown_recovery_timestamp"] == opened + timedelta(minutes=45)
    assert result.summary["average_winning_duration_seconds"] == Decimal("1650")
    assert result.summary["average_losing_duration_seconds"] == Decimal("1800")


def test_same_timestamp_outcomes_use_sequence_as_the_tie_breaker() -> None:
    closed_at = datetime(2026, 1, 2, 10, 0, tzinfo=IST)
    outcomes = [
        ClosedOutcome(
            2,
            closed_at - timedelta(minutes=5),
            closed_at,
            Decimal("-4"),
            Decimal("1"),
            Decimal("-5"),
        ),
        ClosedOutcome(
            1, closed_at - timedelta(minutes=5), closed_at, Decimal("6"), Decimal("1"), Decimal("5")
        ),
    ]

    result = calculate_run_analytics(
        outcomes,
        [date(2026, 1, 2)],
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )

    assert result.summary["max_drawdown_amount"] == Decimal("5")
    assert result.summary["drawdown_peak_timestamp"] == closed_at
    assert result.summary["drawdown_trough_timestamp"] == closed_at


def test_trade_periods_follow_chronological_equity_and_keep_exact_decimals() -> None:
    first_exit = datetime(2026, 1, 2, 10, 0, tzinfo=IST)
    periods = calculate_trade_periods(
        [
            ClosedOutcome(
                2,
                first_exit,
                first_exit + timedelta(minutes=5),
                Decimal("4.2"),
                Decimal("0.2"),
                Decimal("4"),
            ),
            ClosedOutcome(
                1,
                first_exit - timedelta(minutes=5),
                first_exit,
                Decimal("-2.1"),
                Decimal("0.1"),
                Decimal("-2"),
            ),
        ]
    )

    assert [item.sequence for item in periods] == [1, 2]
    assert [item.cumulative_net for item in periods] == [Decimal("-2"), Decimal("2")]
    assert [item.run_drawdown_end for item in periods] == [Decimal("-2"), Decimal("0")]
    assert periods[0].period_start == date(2026, 1, 2)


def test_randomized_reconciliation_and_fingerprint_are_repeatable() -> None:
    random = Random(20260816)
    sessions = [date(2026, 1, day) for day in range(2, 9)]

    def random_outcome(index: int) -> ClosedOutcome:
        net = Decimal(random.randrange(-20, 21))
        costs = Decimal("1")
        return ClosedOutcome(
            sequence=index + 1,
            entry_timestamp=datetime(2026, 1, 2, 9, 15, tzinfo=IST) + timedelta(minutes=index * 5),
            exit_timestamp=datetime(2026, 1, 2, 9, 20, tzinfo=IST) + timedelta(minutes=index * 5),
            gross=net + costs,
            costs=costs,
            net=net,
        )

    outcomes = [random_outcome(index) for index in range(50)]

    first = calculate_run_analytics(
        outcomes, sessions, start_date=sessions[0], end_date=sessions[-1]
    )
    second = calculate_run_analytics(
        list(reversed(outcomes)), sessions, start_date=sessions[0], end_date=sessions[-1]
    )

    daily_net = sum((period.net for period in first.daily), Decimal("0"))
    monthly_net = sum((period.net for period in first.monthly), Decimal("0"))
    assert daily_net == monthly_net == first.summary["net_outcome"]
    assert first.summary["gross_outcome"] - first.summary["total_costs"] == daily_net
    assert first.source_fingerprint == second.source_fingerprint
    assert first.summary["net_outcome"] == second.summary["net_outcome"]


def test_backfill_repeats_only_derived_analytics_for_completed_runs(monkeypatch) -> None:
    run = SimpleNamespace(id="run-1", dataset_version=SimpleNamespace())
    calls: list[str] = []

    class Session:
        def scalars(self, _statement):
            return iter([run])

        def flush(self):
            return None

    monkeypatch.setattr(
        analytics_application,
        "persist_completed_run_analytics",
        lambda _session, value, dataset: calls.append(
            f"{value.id}:{dataset is value.dataset_version}"
        ),
    )

    assert analytics_application.backfill_completed_run_analytics(Session(), limit=1) == 1
    assert analytics_application.backfill_completed_run_analytics(Session(), limit=1) == 1
    assert calls == ["run-1:True", "run-1:True"]


def test_persisted_analytics_are_json_safe_and_replace_existing_periods() -> None:
    closed_at = datetime(2026, 1, 2, 10, 0, tzinfo=IST)
    run = SimpleNamespace(
        id="run-1",
        trades=[
            SimpleNamespace(
                sequence=1,
                entry_timestamp=closed_at - timedelta(minutes=5),
                exit_timestamp=closed_at,
                gross_demo_units=Decimal("2"),
                costs_demo_units=Decimal("1"),
                net_demo_units=Decimal("1"),
            )
        ],
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )
    dataset = SimpleNamespace(session_dates=["2026-01-02"], timezone="Asia/Kolkata")

    class Session:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.executed = 0

        def execute(self, _statement) -> None:
            self.executed += 1

        def get(self, _model, _id):
            return None

        def add(self, value: object) -> None:
            self.added.append(value)

    session = Session()
    persisted = analytics_application.persist_completed_run_analytics(session, run, dataset)

    assert isinstance(persisted, RunAnalytics)
    assert persisted.summary["net_outcome"] == "1"
    assert session.executed == 1
    assert len(session.added) == 3
    payload = analytics_application.analytics_summary_payload(persisted)
    assert payload["summary"]["total_trades"] == 1
