from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.domain.backtests import SignalDecision
from app.domain.demo_execution import DemoExecutionAssumptions, PlatformDemoExecutionAdapter

IST = timezone(timedelta(hours=5, minutes=30))


def decision(index: int, close: str, *, accepted: bool = False, day: int = 2) -> SignalDecision:
    timestamp = datetime(2026, 1, day, 9, 15, tzinfo=IST) + timedelta(minutes=5 * index)
    return SignalDecision(
        timestamp=timestamp,
        session_date=date(2026, 1, day),
        decision="ACCEPTED" if accepted else "REJECTED",
        rejection_reason=None if accepted else "DEMO_INTERVAL_NOT_REACHED",
        window="platform_demo",
        score=1 if accepted else 0,
        inputs={"close": close},
    )


def test_hand_calculated_trades_legs_equity_drawdown_and_metrics() -> None:
    decisions = [
        decision(0, "100", accepted=True),
        decision(1, "102"),
        decision(2, "104"),
        decision(3, "110", accepted=True),
        decision(4, "107"),
        decision(5, "105"),
    ]

    result = PlatformDemoExecutionAdapter().execute(decisions, hold_candles=2)

    assert [trade.net_demo_units for trade in result.trades] == [Decimal("4"), Decimal("-5")]
    assert [leg.name for leg in result.trades[0].legs] == [
        "DEMO_REFERENCE_A",
        "DEMO_REFERENCE_B",
    ]
    assert sum((leg.demo_units for leg in result.trades[1].legs), Decimal("0")) == Decimal("-5")
    assert [point.equity_demo_units for point in result.equity_points] == [
        Decimal("4"),
        Decimal("-1"),
    ]
    assert [point.drawdown_demo_units for point in result.equity_points] == [
        Decimal("0"),
        Decimal("-5"),
    ]
    assert result.metrics() == {
        "total_trades": 2,
        "wins": 1,
        "losses": 1,
        "expectancy_demo_units": "-0.50000000",
        "max_drawdown_demo_units": "-5.00000000",
        "synthetic": True,
        "unit": "DEMO_UNITS",
        "financial_pnl_available": False,
    }


def test_missing_or_cross_session_exit_is_skipped() -> None:
    decisions = [
        decision(0, "100", accepted=True),
        decision(1, "101"),
        decision(0, "102", day=3),
        decision(1, "103", accepted=True, day=3),
    ]

    result = PlatformDemoExecutionAdapter().execute(decisions, hold_candles=2)

    assert result.trades == ()
    assert result.equity_points == ()
    assert result.metrics()["total_trades"] == 0


def test_execution_is_repeatable_and_enforces_non_overlap() -> None:
    decisions = [
        decision(0, "100", accepted=True),
        decision(1, "101", accepted=True),
        decision(2, "102"),
        decision(3, "103", accepted=True),
        decision(4, "104"),
        decision(5, "105"),
    ]
    adapter = PlatformDemoExecutionAdapter()

    first = adapter.execute(decisions, hold_candles=2)
    repeated = adapter.execute(decisions, hold_candles=2)

    assert first == repeated
    assert [trade.entry_timestamp for trade in first.trades] == [
        decisions[0].timestamp,
        decisions[3].timestamp,
    ]


def test_same_candle_exit_is_supported_without_overlap() -> None:
    decisions = [decision(0, "100", accepted=True), decision(1, "101", accepted=True)]

    result = PlatformDemoExecutionAdapter().execute(decisions, hold_candles=0)

    assert len(result.trades) == 2
    assert all(trade.net_demo_units == 0 for trade in result.trades)


def test_demo_safeguards_cannot_be_relabelled() -> None:
    try:
        DemoExecutionAssumptions(unit="INR")
    except ValueError as exc:
        assert str(exc) == "Platform-demo execution safeguards cannot be changed."
    else:
        raise AssertionError("Currency relabelling must be rejected.")
