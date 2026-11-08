"""Pure, versioned analytics for closed backtest outcomes.

The calculator deliberately knows nothing about persistence, HTTP, workers, or
the demo algorithm.  It operates on closed outcomes only: current platform-demo
records are synthetic DEMO_UNITS, not financial P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

ANALYTICS_SCHEMA_VERSION = "stockbot.run-analytics.v1"
ANALYTICS_CALCULATOR_VERSION = "1.0.0"
ZERO = Decimal("0")


@dataclass(frozen=True)
class ClosedOutcome:
    sequence: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    gross: Decimal
    costs: Decimal
    net: Decimal


@dataclass(frozen=True)
class AnalyticsPeriod:
    granularity: str
    period_start: date
    period_end: date
    partial: bool
    eligible_session_count: int
    trade_count: int
    wins: int
    losses: int
    breakevens: int
    gross: Decimal
    costs: Decimal
    net: Decimal
    cumulative_net: Decimal
    equity_end: Decimal
    local_max_drawdown: Decimal
    run_drawdown_end: Decimal


@dataclass(frozen=True)
class AnalyticsTradePeriod:
    sequence: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    period_start: date
    gross: Decimal
    costs: Decimal
    net: Decimal
    cumulative_net: Decimal
    equity_end: Decimal
    local_max_drawdown: Decimal
    run_drawdown_end: Decimal


@dataclass(frozen=True)
class AnalyticsResult:
    summary: dict[str, object]
    daily: tuple[AnalyticsPeriod, ...]
    monthly: tuple[AnalyticsPeriod, ...]
    source_fingerprint: str


def calculate_trade_periods(
    outcomes: list[ClosedOutcome], *, timezone: str = "Asia/Kolkata"
) -> tuple[AnalyticsTradePeriod, ...]:
    """Return immutable closed-trade rows in the same order as the equity curve."""

    zone = ZoneInfo(timezone)
    equity = peak = ZERO
    periods: list[AnalyticsTradePeriod] = []
    for item in sorted(outcomes, key=lambda value: (value.exit_timestamp, value.sequence)):
        equity += item.net
        peak = max(peak, equity)
        periods.append(
            AnalyticsTradePeriod(
                sequence=item.sequence,
                entry_timestamp=item.entry_timestamp,
                exit_timestamp=item.exit_timestamp,
                period_start=item.exit_timestamp.astimezone(zone).date(),
                gross=item.gross,
                costs=item.costs,
                net=item.net,
                cumulative_net=equity,
                equity_end=equity,
                local_max_drawdown=min(ZERO, item.net),
                run_drawdown_end=equity - peak,
            )
        )
    return tuple(periods)


def _decimal_median(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values))) if values else ZERO


def _ratio(numerator: Decimal, denominator: Decimal, reason: str) -> dict[str, object]:
    if denominator == ZERO:
        return {"value": None, "availability": "UNAVAILABLE", "reason": reason}
    return {"value": numerator / denominator, "availability": "AVAILABLE", "reason": None}


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)


def _streaks(outcomes: list[ClosedOutcome]) -> tuple[int, int]:
    win_streak = loss_streak = max_wins = max_losses = 0
    for outcome in outcomes:
        if outcome.net > ZERO:
            win_streak, loss_streak = win_streak + 1, 0
        elif outcome.net < ZERO:
            loss_streak, win_streak = loss_streak + 1, 0
        else:
            win_streak = loss_streak = 0
        max_wins, max_losses = max(max_wins, win_streak), max(max_losses, loss_streak)
    return max_wins, max_losses


def calculate_run_analytics(
    outcomes: list[ClosedOutcome],
    session_dates: list[date],
    *,
    start_date: date,
    end_date: date,
    timezone: str = "Asia/Kolkata",
) -> AnalyticsResult:
    """Calculate reconciled close-date analytics for a completed immutable run."""

    zone = ZoneInfo(timezone)
    ordered = sorted(outcomes, key=lambda item: (item.exit_timestamp, item.sequence))
    sessions = sorted({item for item in session_dates if start_date <= item <= end_date})
    by_day: dict[date, list[ClosedOutcome]] = {item: [] for item in sessions}
    for item in ordered:
        exit_day = item.exit_timestamp.astimezone(zone).date()
        if start_date <= exit_day <= end_date:
            by_day.setdefault(exit_day, []).append(item)

    daily: list[AnalyticsPeriod] = []
    cumulative = peak = ZERO
    maximum_drawdown = ZERO
    peak_timestamp: datetime | None = None
    maximum_peak_timestamp: datetime | None = None
    trough_timestamp: datetime | None = None
    recovery_timestamp: datetime | None = None
    for day in sorted(by_day):
        values = by_day[day]
        gross = costs = net = ZERO
        wins = losses = breakevens = 0
        local_equity = cumulative
        local_peak = cumulative
        local_drawdown = ZERO
        for item in values:
            gross += item.gross
            costs += item.costs
            net += item.net
            if item.net > ZERO:
                wins += 1
            elif item.net < ZERO:
                losses += 1
            else:
                breakevens += 1
            local_equity += item.net
            local_peak = max(local_peak, local_equity)
            local_drawdown = min(local_drawdown, local_equity - local_peak)
            cumulative += item.net
            if (
                maximum_drawdown < ZERO
                and recovery_timestamp is None
                and cumulative >= peak
                and item.exit_timestamp > (trough_timestamp or item.exit_timestamp)
            ):
                recovery_timestamp = item.exit_timestamp
            if cumulative > peak:
                peak = cumulative
                peak_timestamp = item.exit_timestamp
            drawdown = cumulative - peak
            if drawdown < maximum_drawdown:
                maximum_drawdown = drawdown
                maximum_peak_timestamp = peak_timestamp
                trough_timestamp = item.exit_timestamp
                recovery_timestamp = None
        run_drawdown = cumulative - peak
        daily.append(
            AnalyticsPeriod(
                "DAY",
                day,
                day,
                False,
                1,
                len(values),
                wins,
                losses,
                breakevens,
                gross,
                costs,
                net,
                cumulative,
                cumulative,
                local_drawdown,
                run_drawdown,
            )
        )

    by_month: dict[date, list[AnalyticsPeriod]] = {}
    cursor = _month_start(start_date)
    while cursor <= end_date:
        by_month.setdefault(cursor, [])
        cursor = _next_month(cursor)
    for daily_period in daily:
        by_month.setdefault(_month_start(daily_period.period_start), []).append(daily_period)
    monthly: list[AnalyticsPeriod] = []
    monthly_start_equity = ZERO
    for month, days in sorted(by_month.items()):
        period_end = _next_month(month)
        month_end = date.fromordinal(period_end.toordinal() - 1)
        gross = sum((daily_period.gross for daily_period in days), ZERO)
        costs = sum((daily_period.costs for daily_period in days), ZERO)
        net = sum((daily_period.net for daily_period in days), ZERO)
        local_equity = monthly_start_equity
        local_peak = monthly_start_equity
        local_drawdown = ZERO
        for session_day in sorted(by_day):
            if _month_start(session_day) != month:
                continue
            for item in by_day[session_day]:
                local_equity += item.net
                local_peak = max(local_peak, local_equity)
                local_drawdown = min(local_drawdown, local_equity - local_peak)
        month_equity_end = days[-1].equity_end if days else monthly_start_equity
        monthly.append(
            AnalyticsPeriod(
                "MONTH",
                month,
                month_end,
                month < start_date or month_end > end_date,
                len(days),
                sum(daily_period.trade_count for daily_period in days),
                sum(daily_period.wins for daily_period in days),
                sum(daily_period.losses for daily_period in days),
                sum(daily_period.breakevens for daily_period in days),
                gross,
                costs,
                net,
                days[-1].cumulative_net if days else monthly_start_equity,
                month_equity_end,
                local_drawdown,
                days[-1].run_drawdown_end if days else ZERO,
            )
        )
        monthly_start_equity = month_equity_end

    nets = [item.net for item in ordered]
    winners = [item for item in nets if item > ZERO]
    losers = [item for item in nets if item < ZERO]
    durations = [
        Decimal(str((item.exit_timestamp - item.entry_timestamp).total_seconds()))
        for item in ordered
    ]
    winning_durations = [
        Decimal(str((item.exit_timestamp - item.entry_timestamp).total_seconds()))
        for item in ordered
        if item.net > ZERO
    ]
    losing_durations = [
        Decimal(str((item.exit_timestamp - item.entry_timestamp).total_seconds()))
        for item in ordered
        if item.net < ZERO
    ]

    max_wins, max_losses = _streaks(ordered)
    fingerprint = "|".join(
        f"{item.sequence}:{item.entry_timestamp.isoformat()}:{item.exit_timestamp.isoformat()}:{item.gross}:{item.costs}:{item.net}"
        for item in ordered
    )
    total = len(ordered)
    summary: dict[str, object] = {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "calculator_version": ANALYTICS_CALCULATOR_VERSION,
        "total_trades": total,
        "wins": len(winners),
        "losses": len(losers),
        "breakevens": total - len(winners) - len(losers),
        "eligible_sessions": len(sessions),
        "active_sessions": sum(item.trade_count > 0 for item in daily),
        "gross_outcome": sum((item.gross for item in ordered), ZERO),
        "total_costs": sum((item.costs for item in ordered), ZERO),
        "net_outcome": sum(nets, ZERO),
        "cumulative_net_outcome": sum(nets, ZERO),
        "win_rate": Decimal(len(winners)) / total if total else ZERO,
        "loss_rate": Decimal(len(losers)) / total if total else ZERO,
        "breakeven_rate": Decimal(total - len(winners) - len(losers)) / total if total else ZERO,
        "average_trade": sum(nets, ZERO) / total if total else ZERO,
        "median_trade": _decimal_median(nets),
        "average_win": sum(winners, ZERO) / len(winners) if winners else ZERO,
        "average_loss": sum(losers, ZERO) / len(losers) if losers else ZERO,
        "largest_win": max(winners, default=ZERO),
        "largest_loss": min(losers, default=ZERO),
        "expectancy": sum(nets, ZERO) / total if total else ZERO,
        "profit_factor": _ratio(sum(winners, ZERO), abs(sum(losers, ZERO)), "NO_LOSING_TRADES"),
        "payoff_ratio": _ratio(
            sum(winners, ZERO) / len(winners) if winners else ZERO,
            abs(sum(losers, ZERO) / len(losers)) if losers else ZERO,
            "MISSING_WIN_OR_LOSS_SAMPLE",
        ),
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
        "average_duration_seconds": sum(durations, ZERO) / total if total else ZERO,
        "median_duration_seconds": _decimal_median(durations),
        "average_winning_duration_seconds": (
            sum(winning_durations, ZERO) / len(winning_durations) if winning_durations else ZERO
        ),
        "average_losing_duration_seconds": (
            sum(losing_durations, ZERO) / len(losing_durations) if losing_durations else ZERO
        ),
        "max_drawdown_amount": abs(maximum_drawdown),
        # Date fields remain temporarily for earlier clients. New clients use
        # the timestamp fields so a peak/trough/recovery can be audited exactly.
        "drawdown_peak_date": (
            maximum_peak_timestamp.astimezone(zone).date()
            if maximum_peak_timestamp is not None
            else None
        ),
        "drawdown_trough_date": (
            trough_timestamp.astimezone(zone).date() if trough_timestamp is not None else None
        ),
        "drawdown_recovery_date": (
            recovery_timestamp.astimezone(zone).date() if recovery_timestamp is not None else None
        ),
        "drawdown_peak_timestamp": maximum_peak_timestamp,
        "drawdown_trough_timestamp": trough_timestamp,
        "drawdown_recovery_timestamp": recovery_timestamp,
    }
    return AnalyticsResult(summary, tuple(daily), tuple(monthly), fingerprint)
