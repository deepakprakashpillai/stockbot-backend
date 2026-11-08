"""Saved run diagnostics and closed-outcome analytics routes."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.backtests import (
    AnalyticsDefinitionResponse,
    AnalyticsDefinitionsResponse,
    AnalyticsPeriodListResponse,
    AnalyticsSummaryDataResponse,
    DiagnosticGroupResponse,
    RunAnalyticsSummaryResponse,
    RunDiagnosticsResponse,
)
from app.api.v1.backtest_helpers import (
    AnalyticsPeriodRepositoryOptions,
    _aggregate_analytics_periods,
    _analytics_period,
    _analytics_safeguards,
    _error,
    _not_found,
    _trade_analytics_period,
)
from app.application.backtests import BacktestServiceError
from app.core.errors import error_response
from app.db.repositories.backtests import (
    BacktestRepository,
)
from app.domain.run_analytics import ClosedOutcome, calculate_trade_periods

router = APIRouter()


@router.get("/{run_id}/analytics/diagnostics", response_model=RunDiagnosticsResponse)
def get_run_diagnostics(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> RunDiagnosticsResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    signal_count = repository.count_signals(run_id)
    trade_count = repository.count_trades(run_id)
    signals, _ = repository.list_signals(run_id, None, max(signal_count, 1))
    trades, _ = repository.list_trades(run_id, None, max(trade_count, 1))
    signal_by_id = {item.id: item for item in signals}
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def bucket(group: str, category: str) -> dict[str, Any]:
        return grouped[group].setdefault(
            category,
            {
                "category": category,
                "sample_count": 0,
                "trade_count": 0,
                "wins": 0,
                "losses": 0,
                "gross_outcome": Decimal("0"),
                "costs": Decimal("0"),
                "net_outcome": Decimal("0"),
            },
        )

    for signal in signals:
        bucket(
            "confirmation_score",
            str(signal.score) if signal.score is not None else "not_calculated",
        )["sample_count"] += 1
        if signal.rejection_reason:
            bucket("rejection_reason", signal.rejection_reason)["sample_count"] += 1

    adverse_movement = Decimal("0")
    cost_drag = Decimal("0")
    for trade in trades:
        local_entry = trade.entry_timestamp.astimezone(ZoneInfo("Asia/Kolkata"))
        entry_signal = (
            signal_by_id.get(trade.entry_signal_evaluation_id)
            if trade.entry_signal_evaluation_id is not None
            else None
        )
        expiry_raw = (
            trade.assumptions.get("expiry") if isinstance(trade.assumptions, dict) else None
        )
        categories = {
            "confirmation_score": (
                str(entry_signal.score)
                if entry_signal is not None and entry_signal.score is not None
                else "not_calculated"
                if entry_signal is not None
                else "unknown"
            ),
            "entry_time": local_entry.strftime("%H:%M"),
            "days_to_expiry": str((date.fromisoformat(str(expiry_raw)) - local_entry.date()).days)
            if expiry_raw
            else "unknown",
            "exit_reason": trade.exit_reason,
        }
        for group, category in categories.items():
            value = bucket(group, category)
            if group != "confirmation_score":
                value["sample_count"] += 1
            value["trade_count"] += 1
            value["wins"] += int(trade.net_demo_units > 0)
            value["losses"] += int(trade.net_demo_units < 0)
            value["gross_outcome"] += trade.gross_demo_units
            value["costs"] += trade.costs_demo_units
            value["net_outcome"] += trade.net_demo_units
        adverse_movement += max(
            Decimal("0"),
            (trade.entry_reference - trade.exit_reference) * trade.quantity,
        )
        cost_drag += trade.costs_demo_units
    return RunDiagnosticsResponse(
        run_id=run_id,
        unit="INR" if run.mode == "OPTION_SPREAD_SIMULATION" else "DEMO_UNITS",
        groups={
            key: [
                DiagnosticGroupResponse.model_validate(value)
                for _, value in sorted(
                    values.items(),
                    key=lambda item: (
                        (0, int(item[0])) if item[0].lstrip("-").isdigit() else (1, item[0])
                    ),
                )
            ]
            for key, values in grouped.items()
        },
        adverse_movement_amount=adverse_movement,
        cost_drag_amount=cost_drag,
    )


@router.get("/{run_id}/analytics/summary", response_model=RunAnalyticsSummaryResponse)
def get_analytics_summary(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> RunAnalyticsSummaryResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    analytics = repository.get_analytics(run_id)
    if analytics is None:
        return error_response(
            status_code=409,
            code="RUN_ANALYTICS_UNAVAILABLE",
            message="Completed-run analytics are not available for this backtest yet.",
            request=request,
        )
    return RunAnalyticsSummaryResponse(
        run_id=run.id,
        schema_version=analytics.schema_version,
        calculator_version=analytics.calculator_version,
        source_fingerprint=analytics.source_fingerprint,
        safeguards=_analytics_safeguards(
            analytics.timezone, mode=getattr(run, "mode", "PLATFORM_DEMO")
        ),
        summary=AnalyticsSummaryDataResponse.model_validate(analytics.summary),
    )


@router.get("/{run_id}/analytics/periods", response_model=AnalyticsPeriodListResponse)
def list_analytics_periods(
    run_id: UUID,
    request: Request,
    granularity: Literal["TRADE", "DAY", "MONTH"] = "DAY",
    purpose: Literal["chart", "table"] = "chart",
    cursor: int | date | None = None,
    limit: int = 500,
    session: Session = Depends(get_db_session),
    start_date: date | None = None,
    end_date: date | None = None,
    include_zero_periods: bool = True,
    sort: Literal["PERIOD_ASC", "PERIOD_DESC"] = "PERIOD_ASC",
) -> AnalyticsPeriodListResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    analytics = repository.get_analytics(run_id)
    if analytics is None:
        return error_response(
            status_code=409,
            code="RUN_ANALYTICS_UNAVAILABLE",
            message="Completed-run analytics are not available for this backtest yet.",
            request=request,
        )
    if start_date is not None and end_date is not None and start_date > end_date:
        return _error(
            request,
            BacktestServiceError(
                "INVALID_ANALYTICS_DATE_RANGE",
                "start_date must be on or before end_date.",
                422,
            ),
        )
    if purpose == "chart" and sort != "PERIOD_ASC":
        return _error(
            request,
            BacktestServiceError(
                "INVALID_ANALYTICS_SORT",
                "Chart analytics remain chronological; PERIOD_DESC is available for table views.",
                422,
            ),
        )
    next_cursor: int | date | None = None
    resolution = ""
    warnings: list[str] = []
    aggregated = False
    if granularity == "TRADE":
        trade_records = repository.list_trades_chronological(run_id)
        trade_periods = calculate_trade_periods(
            [
                ClosedOutcome(
                    sequence=value.sequence,
                    entry_timestamp=value.entry_timestamp,
                    exit_timestamp=value.exit_timestamp,
                    gross=value.gross_demo_units,
                    costs=value.costs_demo_units,
                    net=value.net_demo_units,
                )
                for value in trade_records
            ],
            timezone=analytics.timezone,
        )
        trades_by_sequence = {value.sequence: value for value in trade_records}
        items = [
            _trade_analytics_period(value, trades_by_sequence[value.sequence])
            for value in trade_periods
            if (start_date is None or value.period_start >= start_date)
            and (end_date is None or value.period_start <= end_date)
        ]
        if cursor is not None:
            if not isinstance(cursor, int):
                return _error(
                    request,
                    BacktestServiceError(
                        "INVALID_ANALYTICS_CURSOR",
                        "Trade analytics cursors must be trade sequences.",
                        422,
                    ),
                )
            cursor_index = next(
                (index for index, item in enumerate(items) if item.sequence == cursor), None
            )
            if cursor_index is None:
                return _error(
                    request,
                    BacktestServiceError(
                        "INVALID_ANALYTICS_CURSOR",
                        "The analytics cursor does not belong to this filtered trade series.",
                        422,
                    ),
                )
            items = items[cursor_index + 1 :]
        total = len(items)
        if purpose == "table":
            bounded_limit = min(max(limit, 1), 200)
            source_items = items
            items = source_items[:bounded_limit]
            next_cursor = (
                items[-1].sequence if len(source_items) > bounded_limit and items else None
            )
            resolution = "TRADE"
            warnings = []
            aggregated = False
        else:
            if total > 100_000:
                return _error(
                    request,
                    BacktestServiceError(
                        "ANALYTICS_CHART_SOURCE_TOO_LARGE",
                        "The requested chart range is too large to aggregate safely in one response.",
                        422,
                        {"source_count": total, "maximum_source_count": 100_000},
                    ),
                )
            items, bucket_size = _aggregate_analytics_periods(items, max_points=1000)
            next_cursor = None
            aggregated = bucket_size > 1
            resolution = "TRADE" if not aggregated else f"TRADE_ROLLUP_{bucket_size}_TRADES"
            warnings = (
                []
                if not aggregated
                else [f"{total} source trades were rolled into {len(items)} exact chart points."]
            )
    elif purpose == "table":
        if isinstance(cursor, int):
            return _error(
                request,
                BacktestServiceError(
                    "INVALID_ANALYTICS_CURSOR",
                    "Day and month analytics cursors must be ISO dates.",
                    422,
                ),
            )
        table_repository_kwargs: AnalyticsPeriodRepositoryOptions = {
            "start_date": start_date,
            "end_date": end_date,
        }
        if not include_zero_periods:
            table_repository_kwargs["include_zero_periods"] = False
        if sort != "PERIOD_ASC":
            table_repository_kwargs["sort"] = sort
        values, next_cursor, total = repository.list_analytics_periods(
            run_id,
            granularity,
            cursor,
            min(max(limit, 1), 200),
            **table_repository_kwargs,
        )
        items = [_analytics_period(value) for value in values]
        resolution = granularity
        warnings = []
        aggregated = False
    else:
        if isinstance(cursor, int):
            return _error(
                request,
                BacktestServiceError(
                    "INVALID_ANALYTICS_CURSOR",
                    "Day and month analytics cursors must be ISO dates.",
                    422,
                ),
            )
        # Day and month rows are precomputed, so a bounded source read can be
        # rolled up without ever approximating gross, cost, net, or counts.
        chart_repository_kwargs: AnalyticsPeriodRepositoryOptions = {
            "start_date": start_date,
            "end_date": end_date,
        }
        if not include_zero_periods:
            chart_repository_kwargs["include_zero_periods"] = False
        values, _, total = repository.list_analytics_periods(
            run_id,
            granularity,
            cursor,
            100_000,
            **chart_repository_kwargs,
        )
        if total > 100_000:
            return _error(
                request,
                BacktestServiceError(
                    "ANALYTICS_CHART_SOURCE_TOO_LARGE",
                    "The requested chart range is too large to aggregate safely in one response.",
                    422,
                    {"source_count": total, "maximum_source_count": 100_000},
                ),
            )
        items, bucket_size = _aggregate_analytics_periods(
            [_analytics_period(value) for value in values], max_points=1000
        )
        next_cursor = None
        aggregated = bucket_size > 1
        resolution = (
            granularity if not aggregated else f"{granularity}_ROLLUP_{bucket_size}_PERIODS"
        )
        warnings = (
            []
            if not aggregated
            else [f"{total} source periods were rolled into {len(items)} exact chart points."]
        )
    metrics = getattr(getattr(request.scope.get("app"), "state", None), "metrics", None)
    if metrics is not None and purpose == "chart" and aggregated:
        metrics.increment(
            "stockbot_analytics_chart_point_reductions_total",
            {"granularity": granularity},
            max(total - len(items), 0),
        )
    return AnalyticsPeriodListResponse(
        run_id=run_id,
        granularity=granularity,
        purpose=purpose,
        items=items,
        next_cursor=next_cursor,
        original_count=total,
        returned_count=len(items),
        aggregated=aggregated,
        resolution=resolution,
        warnings=warnings,
        safeguards=_analytics_safeguards(
            analytics.timezone, mode=getattr(run, "mode", "PLATFORM_DEMO")
        ),
    )


@router.get("/{run_id}/analytics/definitions", response_model=AnalyticsDefinitionsResponse)
def get_analytics_definitions(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> AnalyticsDefinitionsResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    analytics = repository.get_analytics(run_id)
    if analytics is None:
        return error_response(
            status_code=409,
            code="RUN_ANALYTICS_UNAVAILABLE",
            message="Completed-run analytics are not available for this backtest yet.",
            request=request,
        )
    definitions = [
        (
            "gross_outcome",
            "Gross outcome",
            "Closed outcome before costs.",
            "sum(trade gross)",
            "Always available",
            "DEMO_UNITS",
        ),
        (
            "total_costs",
            "Explicit costs",
            "Recorded closed-trade costs.",
            "sum(trade costs)",
            "Always available",
            "DEMO_UNITS",
        ),
        (
            "net_outcome",
            "Net outcome",
            "Closed outcome after costs.",
            "gross outcome − costs",
            "Always available",
            "DEMO_UNITS",
        ),
        (
            "cumulative_net_outcome",
            "Cumulative net outcome",
            "Final closed-outcome equity change.",
            "sum(trade net)",
            "Always available",
            "DEMO_UNITS",
        ),
        (
            "total_trades",
            "Closed trades",
            "Completed closed trades.",
            "count(closed trades)",
            "Always available",
            "COUNT",
        ),
        (
            "wins",
            "Wins",
            "Trades with positive net outcome.",
            "count(net > 0)",
            "Always available",
            "COUNT",
        ),
        (
            "losses",
            "Losses",
            "Trades with negative net outcome.",
            "count(net < 0)",
            "Always available",
            "COUNT",
        ),
        (
            "breakevens",
            "Breakevens",
            "Trades with zero net outcome.",
            "count(net = 0)",
            "Always available",
            "COUNT",
        ),
        (
            "eligible_sessions",
            "Eligible sessions",
            "Dataset sessions in the run range.",
            "count(eligible sessions)",
            "Always available",
            "COUNT",
        ),
        (
            "active_sessions",
            "Active sessions",
            "Eligible sessions with a closed trade.",
            "count(sessions with closed trades)",
            "Always available",
            "COUNT",
        ),
        (
            "win_rate",
            "Win rate",
            "Share of positive closed trades.",
            "wins ÷ total closed trades",
            "Zero when no trades exist",
            "RATE",
        ),
        (
            "loss_rate",
            "Loss rate",
            "Share of negative closed trades.",
            "losses ÷ total closed trades",
            "Zero when no trades exist",
            "RATE",
        ),
        (
            "breakeven_rate",
            "Breakeven rate",
            "Share of flat closed trades.",
            "breakevens ÷ total closed trades",
            "Zero when no trades exist",
            "RATE",
        ),
        (
            "average_trade",
            "Average trade",
            "Mean closed net outcome.",
            "sum(net) ÷ total trades",
            "Zero when no trades exist",
            "DEMO_UNITS",
        ),
        (
            "median_trade",
            "Median trade",
            "Middle closed net outcome.",
            "median(net)",
            "Zero when no trades exist",
            "DEMO_UNITS",
        ),
        (
            "average_win",
            "Average win",
            "Mean positive closed outcome.",
            "sum(positive net) ÷ wins",
            "Zero when no wins exist",
            "DEMO_UNITS",
        ),
        (
            "average_loss",
            "Average loss",
            "Mean negative closed outcome.",
            "sum(negative net) ÷ losses",
            "Zero when no losses exist",
            "DEMO_UNITS",
        ),
        (
            "largest_win",
            "Largest win",
            "Largest positive closed outcome.",
            "max(positive net)",
            "Zero when no wins exist",
            "DEMO_UNITS",
        ),
        (
            "largest_loss",
            "Largest loss",
            "Most negative closed outcome.",
            "min(negative net)",
            "Zero when no losses exist",
            "DEMO_UNITS",
        ),
        (
            "expectancy",
            "Expectancy",
            "Average net outcome per closed trade.",
            "net outcome ÷ total trades",
            "Zero when no trades exist",
            "DEMO_UNITS",
        ),
        (
            "profit_factor",
            "Profit factor",
            "Positive net divided by absolute negative net.",
            "sum(positive net) ÷ abs(sum(negative net))",
            "Unavailable when no losses exist",
            "RATIO",
        ),
        (
            "payoff_ratio",
            "Payoff ratio",
            "Average win divided by absolute average loss.",
            "avg(win) ÷ abs(avg(loss))",
            "Unavailable without wins and losses",
            "RATIO",
        ),
        (
            "max_consecutive_wins",
            "Maximum win streak",
            "Longest positive closed-outcome sequence.",
            "max consecutive net > 0",
            "Always available",
            "COUNT",
        ),
        (
            "max_consecutive_losses",
            "Maximum loss streak",
            "Longest negative closed-outcome sequence.",
            "max consecutive net < 0",
            "Always available",
            "COUNT",
        ),
        (
            "average_duration_seconds",
            "Average duration",
            "Mean entry-to-exit duration.",
            "mean(exit − entry)",
            "Zero when no trades exist",
            "SECONDS",
        ),
        (
            "median_duration_seconds",
            "Median duration",
            "Middle entry-to-exit duration.",
            "median(exit − entry)",
            "Zero when no trades exist",
            "SECONDS",
        ),
        (
            "average_winning_duration_seconds",
            "Average winning duration",
            "Mean duration of positive outcomes.",
            "mean(duration where net > 0)",
            "Zero when no wins exist",
            "SECONDS",
        ),
        (
            "average_losing_duration_seconds",
            "Average losing duration",
            "Mean duration of negative outcomes.",
            "mean(duration where net < 0)",
            "Zero when no losses exist",
            "SECONDS",
        ),
        (
            "max_drawdown_amount",
            "Maximum drawdown",
            "Largest peak-to-trough closed-outcome decline.",
            "abs(min(equity − previous peak))",
            "Always available",
            "DEMO_UNITS",
        ),
        (
            "drawdown_peak_timestamp",
            "Drawdown peak timestamp",
            "Peak preceding the maximum decline.",
            "timestamp at previous equity peak",
            "Unavailable when opening baseline is the peak",
            "TIMESTAMP",
        ),
        (
            "drawdown_trough_timestamp",
            "Drawdown trough timestamp",
            "Trough of the maximum decline.",
            "timestamp at minimum equity − previous peak",
            "Unavailable when no drawdown exists",
            "TIMESTAMP",
        ),
        (
            "drawdown_recovery_timestamp",
            "Drawdown recovery timestamp",
            "First return to the maximum-drawdown peak.",
            "first equity ≥ peak after trough",
            "Unavailable when unrecovered or no drawdown exists",
            "TIMESTAMP",
        ),
    ]
    return AnalyticsDefinitionsResponse(
        schema_version=analytics.schema_version,
        safeguards=_analytics_safeguards(
            analytics.timezone, mode=getattr(run, "mode", "PLATFORM_DEMO")
        ),
        items=[
            AnalyticsDefinitionResponse(
                key=key,
                label=label,
                definition=definition,
                formula=formula,
                availability=availability,
                unit_semantics=(
                    "INR"
                    if unit_semantics == "DEMO_UNITS"
                    and getattr(run, "mode", "PLATFORM_DEMO") == "OPTION_SPREAD_SIMULATION"
                    else unit_semantics
                ),
            )
            for key, label, definition, formula, availability, unit_semantics in definitions
        ],
    )
