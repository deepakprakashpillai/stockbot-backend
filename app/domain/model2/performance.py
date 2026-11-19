"""Versioned priced-population diagnostics; data and economic gates stay distinct."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.model2.market import exchange_time

PERFORMANCE_POLICY = "model2.section12.priced.v1"


@dataclass(frozen=True)
class PricedOutcome:
    exit_at: datetime
    gross: Decimal
    costs: Decimal
    net: Decimal


def priced_statistics(
    outcomes: tuple[PricedOutcome, ...], start: date, end: date, capital: Decimal
) -> dict[str, Any]:
    if capital <= 0 or end < start:
        raise ValueError("An ordered range and positive fixed capital are required.")
    monthly: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    years: dict[str, Decimal] = defaultdict(
        lambda: Decimal(0), {str(year): Decimal(0) for year in range(start.year, end.year + 1)}
    )
    dev: list[Decimal] = []
    later: list[Decimal] = []
    earlier: list[Decimal] = []
    equity = peak = capital
    drawdown = Decimal(0)
    total = gross = costs = Decimal(0)
    points = []
    for outcome in sorted(outcomes, key=lambda value: value.exit_at):
        day = exchange_time(outcome.exit_at).date()
        if not start <= day <= end:
            raise ValueError("Priced outcome outside the requested cycle range.")
        if outcome.gross - outcome.costs != outcome.net:
            raise ValueError("Priced monetary amounts do not reconcile.")
        monthly[day.strftime("%Y-%m")] += outcome.net
        years[str(day.year)] += outcome.net
        (
            dev
            if date(2023, 11, 16) <= day <= date(2026, 7, 17)
            else later
            if day >= date(2026, 7, 18)
            else earlier
        ).append(outcome.net)
        total += outcome.net
        gross += outcome.gross
        costs += outcome.costs
        equity += outcome.net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        points.append(
            {
                "timestamp": outcome.exit_at.isoformat(),
                "equity_inr": str(equity),
                "drawdown_inr": str(peak - equity),
            }
        )
    cursor = date(start.year, start.month, 1)
    month_rows: list[dict[str, Any]] = []
    while cursor <= end:
        next_month = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
        amount = monthly[cursor.strftime("%Y-%m")]
        month_rows.append(
            {
                "month": cursor.strftime("%Y-%m"),
                "net_pnl_inr": str(amount),
                "return_fraction": str(amount / capital),
                "partial": cursor < start or end < next_month - timedelta(days=1),
            }
        )
        cursor = next_month
    wins = sum(value.net > 0 for value in outcomes)
    losses = sum(value.net < 0 for value in outcomes)
    without_best = total - sum(
        sorted((value.net for value in outcomes), reverse=True)[:3], Decimal(0)
    )
    return {
        "policy_version": PERFORMANCE_POLICY,
        "population": "PRICED_CYCLES_ONLY",
        "trade_count": len(outcomes),
        "wins": wins,
        "losses": losses,
        "breakevens": len(outcomes) - wins - losses,
        "win_rate_fraction": str(Decimal(wins) / len(outcomes)) if outcomes else None,
        "gross_pnl_inr": str(gross),
        "costs_inr": str(costs),
        "net_pnl_inr": str(total) if outcomes else None,
        "largest_priced_loss_inr": str(
            max((max(-value.net, Decimal(0)) for value in outcomes), default=Decimal(0))
        )
        if outcomes
        else None,
        "max_drawdown_inr": str(drawdown) if outcomes else None,
        "profit_without_best_three_inr": str(without_best) if len(outcomes) > 3 else None,
        "monthly_denominator": "ALL_CALENDAR_MONTHS_INTERSECTING_RANGE_INCLUDING_ZERO_AND_PARTIAL_MONTHS; partial returns are not annualized",
        "month_count": len(month_rows),
        "average_monthly_return_fraction": str(total / capital / len(month_rows))
        if month_rows and outcomes
        else None,
        "positive_month_fraction": str(
            Decimal(sum(Decimal(row["net_pnl_inr"]) > 0 for row in month_rows)) / len(month_rows)
        )
        if month_rows and outcomes
        else None,
        "worst_month_return_fraction": str(
            min(Decimal(row["return_fraction"]) for row in month_rows)
        )
        if month_rows and outcomes
        else None,
        "months": month_rows,
        "years": [
            {"year": year, "net_pnl_inr": str(amount)} for year, amount in sorted(years.items())
        ],
        "development": {
            "start": "2023-11-16",
            "end": "2026-07-17",
            "priced_cycles": len(dev),
            "net_pnl_inr": str(sum(dev, Decimal(0))),
        },
        "later_exposed_period": {
            "priced_cycles": len(later),
            "net_pnl_inr": str(sum(later, Decimal(0))),
            "clean_holdout": False,
        },
        "earlier_history": {
            "priced_cycles": len(earlier),
            "net_pnl_inr": str(sum(earlier, Decimal(0))),
            "clean_holdout_not_automatically_claimed": True,
        },
        "closed_trade_equity": points,
    }


def assess_performance(
    statistics: dict[str, Any],
    *,
    scope: str,
    reliability: dict[str, Any],
    slippage: Decimal,
    capital: Decimal,
) -> tuple[str, list[dict[str, Any]]]:
    criteria = []

    def check(key: str, value: object, allowed: bool | None, threshold: str) -> None:
        criteria.append(
            {
                "key": key,
                "value": value,
                "threshold": threshold,
                "status": "INSUFFICIENT_SAMPLE"
                if allowed is None
                else "PASS"
                if allowed
                else "FAIL",
            }
        )

    dev = statistics["development"]
    check(
        "development_net_and_sample",
        dev,
        Decimal(dev["net_pnl_inr"]) > 0 if dev["priced_cycles"] >= 100 else None,
        "At least 100 priced development cycles and positive net after charges at 2% slippage",
    )
    sample = statistics["trade_count"] > 0
    for key, field, threshold, condition in (
        (
            "average_monthly_return",
            "average_monthly_return_fraction",
            ">= 0.003",
            lambda value: value >= Decimal(".003"),
        ),
        (
            "positive_months",
            "positive_month_fraction",
            ">= 0.70",
            lambda value: value >= Decimal(".70"),
        ),
        (
            "worst_month",
            "worst_month_return_fraction",
            ">= -0.03",
            lambda value: value >= Decimal("-.03"),
        ),
        (
            "largest_priced_loss",
            "largest_priced_loss_inr",
            f"<= {capital * Decimal('.025')}",
            lambda value: value <= capital * Decimal(".025"),
        ),
        ("without_best_three", "profit_without_best_three_inr", "> 0", lambda value: value > 0),
    ):
        raw = statistics[field]
        check(key, raw, condition(Decimal(raw)) if raw is not None else None, threshold)
    check(
        "each_calendar_year",
        statistics["years"],
        all(Decimal(row["net_pnl_inr"]) > 0 for row in statistics["years"]) if sample else None,
        "Each tested calendar year positive; partial-year coverage is not a full-year claim",
    )
    criteria.extend(
        [
            {
                "key": "holdout",
                "status": "NOT_ASSESSED_EXPOSED_DATES",
                "value": statistics["later_exposed_period"],
                "threshold": "Positive genuinely unseen validation; known-exposed dates are reported separately",
            },
            {
                "key": "random_day_control",
                "status": "NOT_ASSESSED_POLICY_PENDING",
                "value": None,
                "threshold": "Beat the frozen comparable random-day control; protocol remains unspecified",
            },
            {
                "key": "paper_quotes",
                "status": "DEFERRED_OUT_OF_SCOPE",
                "value": None,
                "threshold": "60 trading days with real quotes, within 25% of backtest; separate paper phase",
            },
        ]
    )
    if scope != "FULL_REQUESTED_RANGE":
        status = "INCOMPLETE"
    elif not reliability.get("data_gate_passed"):
        status = reliability.get("status", "INSUFFICIENT_SAMPLE")
    elif slippage != Decimal(2):
        status = "NOT_ASSESSED_SLIPPAGE"
    elif any(item["status"] == "INSUFFICIENT_SAMPLE" for item in criteria):
        status = "INSUFFICIENT_SAMPLE"
    elif any(item["status"] == "FAIL" for item in criteria):
        status = "FAIL_HISTORICAL_CHECKS"
    else:
        status = "NOT_ASSESSED_REMAINING_CRITERIA"
    return status, criteria
