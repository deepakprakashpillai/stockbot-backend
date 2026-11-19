from datetime import date
from decimal import Decimal

import pytest

from app.domain.model2.execution import market_at
from app.domain.model2.performance import PricedOutcome, assess_performance, priced_statistics


def outcome(day, net="200"):
    amount = Decimal(net)
    return PricedOutcome(market_at(day, 14, 30), amount + 400, Decimal(400), amount)


def test_zero_months_partial_months_and_zero_years_are_not_dropped():
    result = priced_statistics(
        (outcome(date(2024, 2, 1), "10"),), date(2023, 12, 31), date(2024, 2, 1), Decimal(500000)
    )
    assert result["month_count"] == 3
    assert [row["partial"] for row in result["months"]] == [True, False, True]
    assert [Decimal(row["net_pnl_inr"]) for row in result["months"]] == [0, 0, 10]
    assert [Decimal(row["net_pnl_inr"]) for row in result["years"]] == [0, 10]
    assert Decimal(result["positive_month_fraction"]) == Decimal(1) / 3
    assert result["development"]["priced_cycles"] == 1


def test_no_priced_outcome_has_no_invented_zero_financial_return():
    result = priced_statistics((), date(2026, 1, 1), date(2026, 1, 31), Decimal(500000))
    assert result["net_pnl_inr"] is result["average_monthly_return_fraction"] is None
    assert result["trade_count"] == 0
    assert result["closed_trade_equity"] == []
    assert result["months"][0]["partial"] is False


@pytest.mark.parametrize(
    "scope,reliable,slip,expected",
    [
        ("CANCELLED", True, "2", "INCOMPLETE"),
        ("HALTED_AT_EXCLUSION", True, "2", "INCOMPLETE"),
        ("FULL_REQUESTED_RANGE", False, "2", "UNRELIABLE_DATA"),
        ("FULL_REQUESTED_RANGE", True, "0", "NOT_ASSESSED_SLIPPAGE"),
        ("FULL_REQUESTED_RANGE", True, "1", "NOT_ASSESSED_SLIPPAGE"),
        ("FULL_REQUESTED_RANGE", True, "2", "INSUFFICIENT_SAMPLE"),
    ],
)
def test_data_scope_slippage_and_sample_gates_remain_distinct(scope, reliable, slip, expected):
    stats = priced_statistics(
        (outcome(date(2026, 1, 6), "-100"),), date(2026, 1, 1), date(2026, 1, 31), Decimal(500000)
    )
    status, criteria = assess_performance(
        stats,
        scope=scope,
        reliability={
            "data_gate_passed": reliable,
            "status": "RELIABLE_DATA" if reliable else "UNRELIABLE_DATA",
        },
        slippage=Decimal(slip),
        capital=Decimal(500000),
    )
    assert status == expected
    assert any(
        row["key"] == "random_day_control" and row["status"] == "NOT_ASSESSED_POLICY_PENDING"
        for row in criteria
    )
    assert any(
        row["key"] == "paper_quotes" and row["status"] == "DEFERRED_OUT_OF_SCOPE"
        for row in criteria
    )


@pytest.mark.parametrize(
    "net,expected", [("200", "NOT_ASSESSED_REMAINING_CRITERIA"), ("-1", "FAIL_HISTORICAL_CHECKS")]
)
def test_hundred_priced_development_outcomes_compute_supported_checks_without_live_approval(
    net, expected
):
    values = tuple(
        outcome(date(2024, month, day), net) for month in range(1, 11) for day in range(1, 11)
    )
    stats = priced_statistics(values, date(2024, 1, 1), date(2024, 10, 31), Decimal(500000))
    status, criteria = assess_performance(
        stats,
        scope="FULL_REQUESTED_RANGE",
        reliability={"data_gate_passed": True},
        slippage=Decimal(2),
        capital=Decimal(500000),
    )
    assert stats["trade_count"] == stats["development"]["priced_cycles"] == 100
    assert status == expected
    assert Decimal(stats["profit_without_best_three_inr"]) == Decimal(net) * 97
    if Decimal(net) > 0:
        assert [row["status"] for row in criteria[:7]] == ["PASS"] * 7
        assert Decimal(stats["average_monthly_return_fraction"]) == Decimal(".004")


def test_priced_periods_and_exposure_windows_use_the_exit_date():
    values = (
        outcome(date(2023, 11, 15), "100"),
        outcome(date(2023, 11, 16), "-50"),
        outcome(date(2026, 7, 17), "200"),
        outcome(date(2026, 7, 18), "-100"),
    )
    stats = priced_statistics(values, date(2023, 11, 1), date(2026, 7, 31), Decimal(500000))
    assert stats["earlier_history"]["priced_cycles"] == 1
    assert stats["development"]["priced_cycles"] == 2
    assert Decimal(stats["development"]["net_pnl_inr"]) == 150
    assert stats["later_exposed_period"]["clean_holdout"] is False
    assert stats["later_exposed_period"]["priced_cycles"] == 1
    assert Decimal(stats["max_drawdown_inr"]) == 100


def test_inconsistent_priced_amounts_and_out_of_range_rows_are_rejected():
    with pytest.raises(ValueError, match="reconcile"):
        priced_statistics(
            (
                PricedOutcome(
                    market_at(date(2026, 1, 6), 14, 30), Decimal(100), Decimal(400), Decimal(100)
                ),
            ),
            date(2026, 1, 1),
            date(2026, 1, 31),
            Decimal(500000),
        )
    with pytest.raises(ValueError, match="outside"):
        priced_statistics(
            (outcome(date(2026, 2, 1)),), date(2026, 1, 1), date(2026, 1, 31), Decimal(500000)
        )
