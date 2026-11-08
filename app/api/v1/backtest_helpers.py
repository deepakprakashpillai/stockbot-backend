from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from math import ceil
from statistics import median
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.schemas.backtests import (
    AnalyticsPeriodResponse,
    AnalyticsSafeguardsResponse,
    BacktestCatalogRunResponse,
    BacktestHistoryBucketResponse,
    BacktestHistorySummaryResponse,
    BacktestHistoryUsageResponse,
    BacktestRunResponse,
    DemoTradeLegResponse,
    DemoTradeResponse,
    ExecutionRejectionResponse,
    ExportJobResponse,
    ReasonSummaryResponse,
    RelatedSignalTradeResponse,
    RunOutcomeSummaryResponse,
    SignalDecisionDetailResponse,
    SignalDiagnosticResponse,
    SignalEvaluationResponse,
    SignalRunIdentityResponse,
)
from app.application.backtests import BacktestServiceError
from app.application.research_limitations import display_research_limitations
from app.core.errors import error_response
from app.db.models.backtests import (
    BacktestRun,
    JournalEvent,
    RunAnalyticsPeriod,
)
from app.db.repositories.backtests import (
    BacktestRunFilters,
)
from app.domain.run_analytics import AnalyticsTradePeriod
from app.domain.signal_diagnostics import SCHEMA_VERSION


def _export_job(value: object) -> ExportJobResponse:
    return ExportJobResponse(
        id=value.id,  # type: ignore[attr-defined]
        run_id=value.run_id,  # type: ignore[attr-defined]
        attempt_id=value.attempt_id,  # type: ignore[attr-defined]
        status=value.status,  # type: ignore[attr-defined]
        format=value.format,  # type: ignore[attr-defined]
        sha256=value.sha256,  # type: ignore[attr-defined]
        row_counts=value.row_counts,  # type: ignore[attr-defined]
        error_code=value.error_code,  # type: ignore[attr-defined]
        error_message=value.error_message,  # type: ignore[attr-defined]
        download_ready=value.status == "COMPLETED",  # type: ignore[attr-defined]
        created_at=value.created_at,  # type: ignore[attr-defined]
        completed_at=value.completed_at,  # type: ignore[attr-defined]
    )


class AnalyticsPeriodRepositoryOptions(TypedDict, total=False):
    start_date: date | None
    end_date: date | None
    include_zero_periods: bool
    sort: str


def _error(request: Request, exc: BacktestServiceError) -> JSONResponse:
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request=request,
    )


def _reason_summary(raw_code: str, diagnostic: dict[str, Any] | None) -> ReasonSummaryResponse:
    if diagnostic and diagnostic.get("schema_version") == SCHEMA_VERSION:
        try:
            diag_resp = SignalDiagnosticResponse.model_validate(diagnostic)
            primary = diag_resp.primary_reason or raw_code
            relevant = [
                step
                for step in diag_resp.evaluation_steps
                if step.status == "failed" or step.reason_code == primary
            ]
            if not relevant:
                relevant = [
                    step
                    for step in reversed(diag_resp.evaluation_steps)
                    if step.status != "not_evaluated"
                ]
            points = [
                f"{step.label}: {step.reason_code or step.status.replace('_', ' ')}"
                for step in relevant[:3]
            ]
            return ReasonSummaryResponse(
                raw_code=primary,
                user_facing_label=primary.replace("_", " ").title(),
                supporting_points=points,
                structured_explanation_available=True,
            )
        except ValidationError:
            pass
    return ReasonSummaryResponse(
        raw_code=raw_code,
        user_facing_label=raw_code.replace("_", " ").title(),
        supporting_points=[],
        structured_explanation_available=False,
    )


def _run_outcome_summary(value: BacktestRun) -> RunOutcomeSummaryResponse | None:
    analytics = getattr(value, "analytics", None)
    if not analytics:
        return None
    summary = analytics.summary
    is_inr = value.mode == "OPTION_SPREAD_SIMULATION"
    unit = summary.get("unit", "INR" if is_inr else "DEMO_UNITS")
    synthetic = summary.get("synthetic", not is_inr)
    financial_pnl = summary.get("financial_pnl_available", is_inr)
    return RunOutcomeSummaryResponse(
        net_outcome=Decimal(str(summary.get("net_outcome", "0"))),
        max_drawdown_amount=Decimal(str(summary.get("max_drawdown_amount", "0"))),
        trade_count=summary.get("total_trades", 0),
        wins=summary.get("wins", 0),
        losses=summary.get("losses", 0),
        mode=value.mode,
        synthetic=synthetic,
        unit=unit,
        financial_pnl_available=financial_pnl,
    )


def _run(value: BacktestRun) -> BacktestRunResponse:
    assumptions = cast(dict[str, object], value.snapshot.get("assumptions") or {})
    option_execution = assumptions.get("option_execution")
    pricing = (
        cast(dict[str, object], option_execution) if isinstance(option_execution, dict) else {}
    )
    coverage = cast(dict[str, object], value.snapshot.get("coverage") or {})
    included_values = coverage.get("included_sessions")
    included_count = (
        len(included_values)
        if isinstance(included_values, list)
        and (included_values or coverage.get("status") != "COMPLETE")
        else None
    )
    algorithm = cast(dict[str, object], value.snapshot.get("algorithm") or {})
    compatibility_warnings: list[str] = []
    if algorithm.get("code_version") == "1.0.0-bull-call-spread-v1":
        compatibility_warnings.append(
            "This legacy run may contain provider candle-timing bias. Keep it for audit only."
        )
    return BacktestRunResponse(
        id=value.id,
        dataset_version_id=value.dataset_version_id,
        algorithm_version_id=value.algorithm_version_id,
        start_date=value.start_date,
        end_date=value.end_date,
        mode=value.mode,
        starting_capital=value.starting_capital,
        status=value.status,
        progress=value.progress,
        cancellation_requested=value.cancellation_requested,
        error_code=value.error_code,
        error_message=value.error_message,
        snapshot=value.snapshot,
        pricing_quality=cast(str | None, assumptions.get("pricing_quality")),
        executable_quotes=bool(assumptions.get("executable_quotes", False)),
        estimate_limitations=display_research_limitations(pricing),
        outcome_summary=_run_outcome_summary(value),
        created_at=value.created_at,
        started_at=value.started_at,
        completed_at=value.completed_at,
        active_attempt_id=getattr(value, "active_attempt_id", None),
        coverage_status=cast(Any, coverage.get("status")),
        coverage_digest=cast(str | None, coverage.get("digest")),
        included_session_count=included_count,
        excluded_session_count=len(cast(list[object], coverage.get("excluded_sessions") or []))
        if coverage
        else None,
        compatibility_warnings=compatibility_warnings,
    )


def _validate_timestamp_range(
    request: Request, start: datetime | None, end: datetime | None
) -> JSONResponse | None:
    if (start is not None and start.tzinfo is None) or (end is not None and end.tzinfo is None):
        return _error(
            request,
            BacktestServiceError(
                "TIME_RANGE_TIMEZONE_REQUIRED",
                "start and end must include a timezone offset.",
                422,
            ),
        )
    if start is not None and end is not None and start > end:
        return _error(
            request,
            BacktestServiceError(
                "INVALID_TIME_RANGE",
                "start must be on or before end.",
                422,
            ),
        )
    return None


def _invalid_filtered_cursor(request: Request) -> JSONResponse:
    return _error(
        request,
        BacktestServiceError(
            "INVALID_FILTERED_CURSOR",
            "The cursor does not belong to this run and filter selection.",
            422,
        ),
    )


def _signal(value: object) -> SignalEvaluationResponse:
    return SignalEvaluationResponse(
        id=value.id,  # type: ignore[attr-defined]
        sequence=value.sequence,  # type: ignore[attr-defined]
        timestamp=value.timestamp,  # type: ignore[attr-defined]
        session_date=value.session_date,  # type: ignore[attr-defined]
        decision=value.decision,  # type: ignore[attr-defined]
        rejection_reason=value.rejection_reason,  # type: ignore[attr-defined]
        window=value.window,  # type: ignore[attr-defined]
        score=value.score,  # type: ignore[attr-defined]
        inputs=value.inputs,  # type: ignore[attr-defined]
        entry_trade_ids=[trade.id for trade in getattr(value, "entry_trades", [])],
    )


def _trade(value: object) -> DemoTradeResponse:
    entry_signal = getattr(value, "entry_signal", None)
    diagnostic = getattr(entry_signal, "diagnostic", None) if entry_signal else None
    assumptions = cast(dict[str, object], getattr(value, "assumptions", {}) or {})
    costs = cast(
        Decimal,
        getattr(
            value,
            "costs_demo_units",
            value.gross_demo_units - value.net_demo_units,  # type: ignore[attr-defined]
        ),
    )
    return DemoTradeResponse(
        id=value.id,  # type: ignore[attr-defined]
        sequence=value.sequence,  # type: ignore[attr-defined]
        entry_signal_evaluation_id=getattr(value, "entry_signal_evaluation_id", None),
        entry_timestamp=value.entry_timestamp,  # type: ignore[attr-defined]
        exit_timestamp=value.exit_timestamp,  # type: ignore[attr-defined]
        exit_reason=value.exit_reason,  # type: ignore[attr-defined]
        entry_reason_summary=_reason_summary(
            getattr(entry_signal, "decision", "UNKNOWN") if entry_signal else "UNKNOWN", diagnostic
        )
        if entry_signal
        else None,
        exit_reason_label=getattr(value, "exit_reason", "").replace("_", " ").title(),
        quantity=value.quantity,  # type: ignore[attr-defined]
        entry_reference=value.entry_reference,  # type: ignore[attr-defined]
        exit_reference=value.exit_reference,  # type: ignore[attr-defined]
        gross_demo_units=value.gross_demo_units,  # type: ignore[attr-defined]
        costs_demo_units=costs,
        net_demo_units=value.net_demo_units,  # type: ignore[attr-defined]
        gross_outcome=value.gross_demo_units,  # type: ignore[attr-defined]
        costs=costs,
        net_outcome=value.net_demo_units,  # type: ignore[attr-defined]
        pricing_quality=cast(str | None, assumptions.get("pricing_quality")),
        executable_quotes=bool(assumptions.get("executable_quotes", False)),
        estimate_limitations=display_research_limitations(assumptions),
        synthetic=getattr(value, "synthetic", True),
        unit=getattr(value, "unit", "DEMO_UNITS"),
        financial_pnl_available=getattr(value, "financial_pnl_available", False),
        assumptions=assumptions,
        legs=[
            DemoTradeLegResponse(
                id=leg.id,
                leg_number=leg.leg_number,
                name=leg.name,
                entry_reference=leg.entry_reference,
                exit_reference=leg.exit_reference,
                demo_units=leg.demo_units,
                gross_outcome=leg.demo_units,
                synthetic=getattr(leg, "synthetic", getattr(value, "synthetic", True)),
                unit=getattr(leg, "unit", getattr(value, "unit", "DEMO_UNITS")),
                financial_pnl_available=getattr(
                    leg, "financial_pnl_available", getattr(value, "financial_pnl_available", False)
                ),
            )
            for leg in value.legs  # type: ignore[attr-defined]
        ],
    )


def _signal_detail(
    value: object,
    run: BacktestRun,
    previous_signal_id: UUID | None,
    next_signal_id: UUID | None,
    execution_rejection: JournalEvent | None = None,
) -> SignalDecisionDetailResponse:
    snapshot = cast(dict[str, object], run.snapshot)
    algorithm = cast(dict[str, object], snapshot.get("algorithm") or {})
    assumptions = cast(dict[str, object], snapshot.get("assumptions") or {})
    diagnostic = getattr(value, "diagnostic", None)
    diagnostic_dict = diagnostic if isinstance(diagnostic, dict) else None
    diagnostic_response: SignalDiagnosticResponse | None = None
    if diagnostic_dict is not None and diagnostic_dict.get("schema_version") == SCHEMA_VERSION:
        try:
            diagnostic_response = SignalDiagnosticResponse.model_validate(diagnostic_dict)
        except ValidationError:
            diagnostic_response = None
    structured = diagnostic_response is not None
    trades = list(getattr(value, "entry_trades", []))
    related = trades[0] if len(trades) == 1 else None
    if diagnostic_response is not None:
        limitations = list(diagnostic_response.limitations)
    else:
        limitations = [
            "This historical signal has no persisted structured diagnostic. Raw evidence is shown without reinterpretation."
        ]
    return SignalDecisionDetailResponse(
        signal=_signal(value),
        run_identity=SignalRunIdentityResponse(
            run_id=run.id,
            dataset_version_id=run.dataset_version_id,
            algorithm_version_id=run.algorithm_version_id,
            algorithm_key=str(algorithm.get("key", "unknown")),
            code_version=str(algorithm.get("code_version", "unknown")),
            mode=run.mode,
            synthetic=bool(assumptions.get("synthetic", run.mode == "PLATFORM_DEMO")),
            unit=cast(str | None, assumptions.get("unit")),
            financial_pnl_available=bool(
                assumptions.get(
                    "financial_pnl_available",
                    run.mode == "OPTION_SPREAD_SIMULATION",
                )
            ),
        ),
        diagnostic=diagnostic_response,
        structured_explanation_available=structured,
        raw_evidence=None if structured else getattr(value, "inputs", {}),
        limitations=limitations,
        previous_signal_id=previous_signal_id,
        next_signal_id=next_signal_id,
        related_trade=(
            RelatedSignalTradeResponse(
                id=related.id,
                sequence=related.sequence,
                entry_timestamp=related.entry_timestamp,
                exit_timestamp=related.exit_timestamp,
                exit_reason=related.exit_reason,
                net_demo_units=related.net_demo_units,
                synthetic=getattr(related, "synthetic", True),
                unit=getattr(related, "unit", "DEMO_UNITS"),
                financial_pnl_available=getattr(related, "financial_pnl_available", False),
            )
            if related is not None
            else None
        ),
        execution_rejection=(
            ExecutionRejectionResponse(
                timestamp=execution_rejection.timestamp,
                reason=str(execution_rejection.payload.get("reason", "UNKNOWN")),
                details={
                    key: value
                    for key, value in execution_rejection.payload.items()
                    if key != "reason"
                },
            )
            if execution_rejection is not None
            else None
        ),
    )


def _catalog_run(value: BacktestRun) -> BacktestCatalogRunResponse:
    dataset_version = value.dataset_version
    algorithm_version = value.algorithm_version
    dataset = dataset_version.dataset
    algorithm = algorithm_version.algorithm
    metrics = value.metrics.metrics if value.metrics else {}
    safe_metrics = {
        key: metrics[key]
        for key in (
            "processed_candles",
            "accepted_signals",
            "rejected_evaluations",
            "total_trades",
            "wins",
            "losses",
        )
        if key in metrics
    }
    end = value.completed_at or (datetime.now(UTC) if value.started_at else None)
    duration = int((end - value.started_at).total_seconds()) if end and value.started_at else None
    return BacktestCatalogRunResponse(
        id=value.id,
        dataset_version_id=value.dataset_version_id,
        dataset_name=dataset.name,
        dataset_version_number=dataset_version.version_number,
        algorithm_version_id=value.algorithm_version_id,
        algorithm_key=algorithm_version.algorithm_key,
        algorithm_display_name=algorithm.display_name,
        algorithm_version_name=algorithm_version.name,
        algorithm_version_number=algorithm_version.version_number,
        code_version=algorithm_version.code_version,
        start_date=value.start_date,
        end_date=value.end_date,
        mode=value.mode,
        status=value.status,
        progress=value.progress,
        error_code=value.error_code,
        error_message=value.error_message,
        created_at=value.created_at,
        started_at=value.started_at,
        completed_at=value.completed_at,
        duration_seconds=duration,
        metrics_summary=safe_metrics,
        outcome_summary=_run_outcome_summary(value),
        synthetic=value.mode != "OPTION_SPREAD_SIMULATION",
        unit="INR" if value.mode == "OPTION_SPREAD_SIMULATION" else "DEMO_UNITS",
        financial_pnl_available=value.mode == "OPTION_SPREAD_SIMULATION",
    )


def _history_summary(values: list[BacktestRun]) -> BacktestHistorySummaryResponse:
    statuses = Counter(value.status for value in values)
    activity: dict[date, Counter[str]] = defaultdict(Counter)
    datasets: Counter[tuple[str, str]] = Counter()
    algorithms: Counter[tuple[str, str]] = Counter()
    durations: list[int] = []
    for value in values:
        activity[value.created_at.date()][value.status] += 1
        datasets[(str(value.dataset_version_id), value.dataset_version.dataset.name)] += 1
        algorithms[
            (value.algorithm_version.algorithm_key, value.algorithm_version.algorithm.display_name)
        ] += 1
        if value.started_at and value.completed_at:
            durations.append(max(0, int((value.completed_at - value.started_at).total_seconds())))
    completed = statuses["COMPLETED"]
    active = statuses["PENDING"] + statuses["RUNNING"]
    result_modes = {value.mode for value in values}
    has_estimates = "OPTION_SPREAD_SIMULATION" in result_modes
    has_demo = "PLATFORM_DEMO" in result_modes
    return BacktestHistorySummaryResponse(
        total_runs=len(values),
        completed_runs=completed,
        failed_runs=statuses["FAILED"],
        active_runs=active,
        completion_rate=round((completed / len(values) * 100) if values else 0, 1),
        median_duration_seconds=int(median(durations)) if durations else None,
        status_counts=dict(statuses),
        activity=[
            BacktestHistoryBucketResponse(
                date=key,
                total=sum(counts.values()),
                completed=counts["COMPLETED"],
                failed=counts["FAILED"],
                cancelled=counts["CANCELLED"],
                active=counts["PENDING"] + counts["RUNNING"],
            )
            for key, counts in sorted(activity.items())
        ],
        dataset_usage=[
            BacktestHistoryUsageResponse(key=key, label=label, count=count)
            for (key, label), count in datasets.most_common(8)
        ],
        algorithm_usage=[
            BacktestHistoryUsageResponse(key=key, label=label, count=count)
            for (key, label), count in algorithms.most_common(8)
        ],
        synthetic=not has_estimates,
        unit="MIXED" if has_estimates and has_demo else ("INR" if has_estimates else "DEMO_UNITS"),
        financial_pnl_available=has_estimates,
    )


def _filters(
    *,
    run_query: str | None,
    status: str | None,
    dataset_id: UUID | None,
    dataset_version_id: UUID | None,
    algorithm_key: str | None,
    algorithm_version_id: UUID | None,
    mode: str | None,
    start_date: date | None,
    end_date: date | None,
    created_from: datetime | None,
    created_to: datetime | None,
) -> BacktestRunFilters:
    normalized_query = run_query.strip().lower() if run_query else None
    if normalized_query and not re.fullmatch(r"[0-9a-f-]{4,36}", normalized_query):
        normalized_query = "!invalid!"
    return BacktestRunFilters(
        run_query=normalized_query,
        status=status,
        dataset_id=dataset_id,
        dataset_version_id=dataset_version_id,
        algorithm_key=algorithm_key,
        algorithm_version_id=algorithm_version_id,
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        created_from=created_from,
        created_to=created_to,
    )


def _not_found(request: Request) -> JSONResponse:
    return error_response(
        status_code=404,
        code="BACKTEST_NOT_FOUND",
        message="The requested backtest does not exist.",
        request=request,
    )


def _analytics_safeguards(
    timezone: str, mode: str = "PLATFORM_DEMO"
) -> AnalyticsSafeguardsResponse:
    is_inr = mode == "OPTION_SPREAD_SIMULATION"
    return AnalyticsSafeguardsResponse(
        mode=mode,
        synthetic=not is_inr,
        unit="INR" if is_inr else "DEMO_UNITS",
        financial_pnl_available=is_inr,
        realized_only=True,
        attribution="EXIT_TIMESTAMP",
        timezone=timezone,
    )


def _analytics_period(value: RunAnalyticsPeriod) -> AnalyticsPeriodResponse:
    return AnalyticsPeriodResponse(
        granularity=cast(Literal["TRADE", "DAY", "MONTH"], value.granularity),
        period_start=value.period_start,
        period_end=value.period_end,
        partial=value.partial,
        eligible_session_count=value.eligible_session_count,
        trade_count=value.trade_count,
        wins=value.wins,
        losses=value.losses,
        breakevens=value.breakevens,
        gross_outcome=value.gross_demo_units,
        costs=value.costs_demo_units,
        net_outcome=value.net_demo_units,
        cumulative_net_outcome=value.cumulative_net_demo_units,
        equity_end=value.equity_demo_units,
        local_max_drawdown=value.local_max_drawdown_demo_units,
        run_drawdown_end=value.run_drawdown_demo_units,
    )


def _trade_analytics_period(
    value: AnalyticsTradePeriod,
    trade: object,
) -> AnalyticsPeriodResponse:
    entry_signal = getattr(trade, "entry_signal", None)
    diagnostic = getattr(entry_signal, "diagnostic", None) if entry_signal else None

    return AnalyticsPeriodResponse(
        granularity="TRADE",
        period_start=value.period_start,
        period_end=value.period_start,
        partial=False,
        eligible_session_count=1,
        trade_count=1,
        wins=int(value.net > 0),
        losses=int(value.net < 0),
        breakevens=int(value.net == 0),
        gross_outcome=value.gross,
        costs=value.costs,
        net_outcome=value.net,
        cumulative_net_outcome=value.cumulative_net,
        equity_end=value.equity_end,
        local_max_drawdown=value.local_max_drawdown,
        run_drawdown_end=value.run_drawdown_end,
        sequence=value.sequence,
        entry_timestamp=value.entry_timestamp,
        exit_timestamp=value.exit_timestamp,
        exit_reason=getattr(trade, "exit_reason", ""),
        trade_id=getattr(trade, "id", None),
        entry_signal_evaluation_id=getattr(trade, "entry_signal_evaluation_id", None),
        entry_reason_summary=_reason_summary(
            getattr(entry_signal, "decision", "UNKNOWN") if entry_signal else "UNKNOWN", diagnostic
        )
        if entry_signal
        else None,
        exit_reason_summary=_reason_summary(getattr(trade, "exit_reason", ""), None),
    )


def _aggregate_analytics_periods(
    values: list[AnalyticsPeriodResponse], *, max_points: int
) -> tuple[list[AnalyticsPeriodResponse], int]:
    """Roll adjacent periods into exact totals while retaining the final equity state."""

    bucket_size = max(1, ceil(len(values) / max_points))
    if bucket_size == 1:
        return values, bucket_size
    aggregated: list[AnalyticsPeriodResponse] = []
    for offset in range(0, len(values), bucket_size):
        bucket = values[offset : offset + bucket_size]
        first, last = bucket[0], bucket[-1]
        aggregated.append(
            AnalyticsPeriodResponse(
                granularity=first.granularity,
                period_start=first.period_start,
                period_end=last.period_end,
                partial=any(item.partial for item in bucket),
                eligible_session_count=sum(item.eligible_session_count for item in bucket),
                trade_count=sum(item.trade_count for item in bucket),
                wins=sum(item.wins for item in bucket),
                losses=sum(item.losses for item in bucket),
                breakevens=sum(item.breakevens for item in bucket),
                gross_outcome=sum(
                    (item.gross_outcome or Decimal("0") for item in bucket), Decimal("0")
                ),
                costs=sum((item.costs or Decimal("0") for item in bucket), Decimal("0")),
                net_outcome=sum(
                    (item.net_outcome or Decimal("0") for item in bucket), Decimal("0")
                ),
                cumulative_net_outcome=last.cumulative_net_outcome,
                equity_end=last.equity_end,
                local_max_drawdown=min(item.local_max_drawdown for item in bucket),
                run_drawdown_end=last.run_drawdown_end,
            )
        )
    return aggregated, bucket_size
