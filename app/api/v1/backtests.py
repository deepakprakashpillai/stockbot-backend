from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.backtests import (
    BacktestCreateRequest,
    BacktestHistorySummaryResponse,
    BacktestPreflightRequest,
    BacktestPreflightResponse,
    BacktestRunListResponse,
    BacktestRunResponse,
    DemoEquityPointListResponse,
    DemoEquityPointResponse,
    DemoTradeListResponse,
    DemoTradeResponse,
    ExecutionAttemptListResponse,
    ExecutionAttemptResponse,
    JournalEventListResponse,
    JournalEventResponse,
    JsonExportResponse,
    LiveRunSnapshotResponse,
    RunDayClassificationListResponse,
    RunDayClassificationResponse,
    RunEventListResponse,
    RunEventResponse,
    RunMetricsResponse,
    SignalDecisionDetailResponse,
    SignalEvaluationListResponse,
    SignalEvaluationResponse,
)
from app.api.v1 import backtest_analytics, backtest_comparisons, backtest_exports
from app.api.v1.backtest_helpers import (
    _catalog_run,
    _error,
    _filters,
    _history_summary,
    _invalid_filtered_cursor,
    _not_found,
    _run,
    _signal,
    _signal_detail,
    _trade,
    _validate_timestamp_range,
)
from app.application.backtests import BacktestService, BacktestServiceError
from app.application.results import ResultService
from app.core.config import Settings
from app.db.models.backtests import (
    RunEvent,
    SignalEvaluation,
)
from app.db.repositories.backtests import (
    BacktestRepository,
    EquityListFilters,
    EventListFilters,
    SignalListFilters,
    TradeListFilters,
)
from app.infrastructure.datasets.storage import LocalDatasetStorage
from app.infrastructure.realtime import RedisRunEventPublisher

router = APIRouter(prefix="/backtests", tags=["backtests"])


def _service(request: Request, session: Session) -> BacktestService:
    settings: Settings = request.app.state.settings
    publisher = getattr(request.app.state, "realtime_publisher", None)
    if publisher is None:
        publisher = RedisRunEventPublisher(settings)
        request.app.state.realtime_publisher = publisher
    return BacktestService(
        session,
        LocalDatasetStorage(settings.storage_root),
        settings,
        publisher,
    )


@router.post("/preflight", response_model=BacktestPreflightResponse)
def preflight(
    body: BacktestPreflightRequest, request: Request, session: Session = Depends(get_db_session)
) -> BacktestPreflightResponse | JSONResponse:
    try:
        return BacktestPreflightResponse.model_validate(
            _service(request, session).preflight(
                body.dataset_version_id,
                body.algorithm_version_id,
                body.start_date,
                body.end_date,
                body.warning_acknowledged,
                body.accepted_coverage_digest,
            )
        )
    except BacktestServiceError as exc:
        return _error(request, exc)


@router.post("", response_model=BacktestRunResponse, status_code=202)
def create_backtest(
    body: BacktestCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_db_session),
) -> BacktestRunResponse | JSONResponse:
    try:
        run, duplicate = _service(request, session).create(
            dataset_version_id=body.dataset_version_id,
            algorithm_version_id=body.algorithm_version_id,
            start_date=body.start_date,
            end_date=body.end_date,
            starting_capital=body.starting_capital,
            warning_acknowledged=body.warning_acknowledged,
            accepted_coverage_digest=body.accepted_coverage_digest,
            idempotency_key=idempotency_key,
        )
    except BacktestServiceError as exc:
        return _error(request, exc)
    return _run(run)


@router.get("", response_model=BacktestRunListResponse)
def list_backtests(
    run_query: str | None = None,
    status: str | None = None,
    dataset_id: UUID | None = None,
    dataset_version_id: UUID | None = None,
    algorithm_key: str | None = None,
    algorithm_version_id: UUID | None = None,
    mode: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    cursor: UUID | None = None,
    limit: int = 20,
    session: Session = Depends(get_db_session),
) -> BacktestRunListResponse:
    filters = _filters(
        run_query=run_query,
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
    repository = BacktestRepository(session)
    values, next_cursor = repository.list_runs(
        filters,
        cursor,
        min(max(limit, 1), 100),
    )
    total = repository.count_runs(filters)
    return BacktestRunListResponse(
        items=[_catalog_run(value) for value in values], next_cursor=next_cursor, total_count=total
    )


@router.get("/history-summary", response_model=BacktestHistorySummaryResponse)
def history_summary(
    run_query: str | None = None,
    status: str | None = None,
    dataset_id: UUID | None = None,
    dataset_version_id: UUID | None = None,
    algorithm_key: str | None = None,
    algorithm_version_id: UUID | None = None,
    mode: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    session: Session = Depends(get_db_session),
) -> BacktestHistorySummaryResponse:
    filters = _filters(
        run_query=run_query,
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
    return _history_summary(BacktestRepository(session).history_runs(filters))


router.include_router(backtest_comparisons.router)


router.include_router(backtest_exports.job_router)


@router.get("/{run_id}", response_model=BacktestRunResponse)
def get_backtest(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> BacktestRunResponse | JSONResponse:
    value = BacktestRepository(session).get_run(run_id)
    if value is None:
        return _not_found(request)
    return _run(value)


router.include_router(backtest_exports.run_router)


@router.post("/{run_id}/cancel", response_model=BacktestRunResponse, status_code=202)
def cancel_backtest(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> BacktestRunResponse | JSONResponse:
    try:
        return _run(_service(request, session).cancel(run_id))
    except BacktestServiceError as exc:
        return _error(request, exc)


@router.get("/{run_id}/signals", response_model=SignalEvaluationListResponse)
def list_signals(
    run_id: UUID,
    request: Request,
    cursor: UUID | None = None,
    limit: int = 100,
    session: Session = Depends(get_db_session),
    after_sequence: int | None = Query(default=None, ge=0),
    start: datetime | None = None,
    end: datetime | None = None,
    decision: Literal["ACCEPTED", "REJECTED"] | None = None,
    rejection_reason: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    window: Annotated[str | None, Query(min_length=1, max_length=30)] = None,
) -> SignalEvaluationListResponse | JSONResponse:
    repository = BacktestRepository(session)
    if repository.get_run(run_id) is None:
        return _not_found(request)
    invalid_range = _validate_timestamp_range(request, start, end)
    if invalid_range is not None:
        return invalid_range
    if any((start, end, decision, rejection_reason, window)):
        if cursor is not None:
            cursor_value = repository.get_signal(run_id, cursor)
            if (
                cursor_value is None
                or (start is not None and cursor_value.timestamp < start)
                or (end is not None and cursor_value.timestamp > end)
                or (decision is not None and cursor_value.decision != decision)
                or (
                    rejection_reason is not None
                    and cursor_value.rejection_reason != rejection_reason
                )
                or (window is not None and cursor_value.window != window)
            ):
                return _invalid_filtered_cursor(request)
        values, next_cursor = repository.list_signals(
            run_id,
            cursor,
            min(max(limit, 1), 200),
            after_sequence=after_sequence if isinstance(after_sequence, int) else None,
            filters=SignalListFilters(
                start=start,
                end=end,
                decision=decision,
                rejection_reason=rejection_reason,
                window=window,
            ),
        )
    else:
        values, next_cursor = repository.list_signals(
            run_id,
            cursor,
            min(max(limit, 1), 200),
            after_sequence=after_sequence if isinstance(after_sequence, int) else None,
        )

    return SignalEvaluationListResponse(
        items=[_signal(value) for value in values],
        next_cursor=next_cursor,
    )


@router.get("/{run_id}/signals/{signal_id}", response_model=SignalEvaluationResponse)
def get_signal(
    run_id: UUID, signal_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> SignalEvaluationResponse | JSONResponse:
    value = BacktestRepository(session).get_signal(run_id, signal_id)
    if value is None:
        return _not_found(request)
    return _signal(value)


@router.get("/{run_id}/signals/{signal_id}/detail", response_model=SignalDecisionDetailResponse)
def get_signal_detail(
    run_id: UUID, signal_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> SignalDecisionDetailResponse | JSONResponse:
    repository = BacktestRepository(session)
    value = repository.get_signal(run_id, signal_id)
    run = repository.get_run(run_id)
    if value is None or run is None:
        return _not_found(request)
    previous_signal_id, next_signal_id = repository.signal_neighbors(run_id, value.sequence)
    attempt_id = getattr(run, "active_attempt_id", None)
    rejection = (
        repository.get_signal_execution_rejection(run_id, attempt_id, value.sequence)
        if attempt_id is not None
        else None
    )
    return _signal_detail(value, run, previous_signal_id, next_signal_id, rejection)


@router.get("/{run_id}/events/{event_id}", response_model=RunEventResponse)
def get_event(
    run_id: UUID, event_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> RunEventResponse | JSONResponse:
    value = BacktestRepository(session).get_event(run_id, event_id)
    if value is None:
        return _not_found(request)
    return RunEventResponse(
        id=value.id,
        sequence=value.sequence,
        event_type=value.event_type,
        payload=value.payload,
        created_at=value.created_at,
    )


@router.get("/{run_id}/attempts", response_model=ExecutionAttemptListResponse)
def list_execution_attempts(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> ExecutionAttemptListResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    return ExecutionAttemptListResponse(
        active_attempt_id=run.active_attempt_id,
        items=[
            ExecutionAttemptResponse.model_validate(item, from_attributes=True)
            for item in repository.list_attempts(run_id)
        ],
    )


@router.get("/{run_id}/journal", response_model=JournalEventListResponse)
def list_journal_events(
    run_id: UUID,
    request: Request,
    attempt_id: UUID | None = None,
    cursor: int | None = None,
    market_order: bool = False,
    market_cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
) -> JournalEventListResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    selected_attempt_id = attempt_id or run.active_attempt_id
    if selected_attempt_id is None:
        return JournalEventListResponse(
            run_id=run_id,
            attempt_id=None,
            items=[],
            next_cursor=None,
            legacy_detail_unavailable=True,
        )
    attempts = {item.id: item for item in repository.list_attempts(run_id)}
    attempt = attempts.get(selected_attempt_id)
    if attempt is None:
        return _not_found(request)
    next_market_cursor = None
    try:
        if market_order:
            values, next_market_cursor = repository.list_journal_chronological(
                run_id, selected_attempt_id, market_cursor, limit
            )
            next_cursor = None
        else:
            values, next_cursor = repository.list_journal(
                run_id, selected_attempt_id, cursor, limit
            )
    except ValueError:
        return _error(
            request,
            BacktestServiceError(
                "JOURNAL_CURSOR_INVALID",
                "The journal cursor is invalid or belongs to another attempt.",
                422,
            ),
        )
    return JournalEventListResponse(
        run_id=run_id,
        attempt_id=selected_attempt_id,
        items=[JournalEventResponse.model_validate(item, from_attributes=True) for item in values],
        next_cursor=next_cursor,
        next_market_cursor=next_market_cursor,
        legacy_detail_unavailable=attempt.legacy,
    )


@router.get("/{run_id}/days", response_model=RunDayClassificationListResponse)
def list_day_classifications(
    run_id: UUID,
    request: Request,
    attempt_id: UUID | None = None,
    cursor: date | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_db_session),
) -> RunDayClassificationListResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    selected_attempt_id = attempt_id or run.active_attempt_id
    if selected_attempt_id is None:
        return RunDayClassificationListResponse(
            run_id=run_id, attempt_id=None, items=[], next_cursor=None, counts={}
        )
    values, next_cursor = repository.list_day_classifications(
        run_id, selected_attempt_id, cursor, limit
    )
    all_values, _ = repository.list_day_classifications(run_id, selected_attempt_id, None, 10000)
    return RunDayClassificationListResponse(
        run_id=run_id,
        attempt_id=selected_attempt_id,
        items=[
            RunDayClassificationResponse.model_validate(item, from_attributes=True)
            for item in values
        ],
        next_cursor=next_cursor,
        counts=dict(Counter(item.classification for item in all_values)),
    )


@router.get("/{run_id}/trades/{trade_id}", response_model=DemoTradeResponse)
def get_trade(
    run_id: UUID, trade_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> DemoTradeResponse | JSONResponse:
    value = BacktestRepository(session).get_trade(run_id, trade_id)
    if value is None:
        return _not_found(request)
    return _trade(value)


@router.get("/{run_id}/equity/{point_id}", response_model=DemoEquityPointResponse)
def get_equity_point(
    run_id: UUID, point_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> DemoEquityPointResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    value = repository.get_equity_point(run_id, point_id)
    if value is None:
        return _not_found(request)
    mode = getattr(run, "mode", "PLATFORM_DEMO")
    is_inr = mode == "OPTION_SPREAD_SIMULATION"
    return DemoEquityPointResponse(
        id=value.id,
        sequence=value.sequence,
        timestamp=value.timestamp,
        equity_demo_units=value.equity_demo_units,
        drawdown_demo_units=value.drawdown_demo_units,
        equity_amount=value.equity_demo_units,
        drawdown_amount=value.drawdown_demo_units,
        synthetic=getattr(value, "synthetic", not is_inr),
        unit=getattr(value, "unit", "INR" if is_inr else "DEMO_UNITS"),
        financial_pnl_available=getattr(value, "financial_pnl_available", is_inr),
    )


@router.get("/{run_id}/live-snapshot", response_model=LiveRunSnapshotResponse)
def get_live_snapshot(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> LiveRunSnapshotResponse | JSONResponse:
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    metrics = run.metrics.metrics if run.metrics else {}
    last_timestamp = metrics.get("last_market_timestamp")
    if last_timestamp is None and hasattr(session, "scalar"):
        last_eval_timestamp = session.scalar(
            select(SignalEvaluation.timestamp)
            .where(SignalEvaluation.run_id == run_id)
            .order_by(SignalEvaluation.sequence.desc())
            .limit(1)
        )
        if last_eval_timestamp is not None:
            last_timestamp = (
                last_eval_timestamp.isoformat()
                if isinstance(last_eval_timestamp, datetime)
                else str(last_eval_timestamp)
            )
    checkpointed_at = run.completed_at
    if checkpointed_at is None:
        latest_event = repository.latest_event_sequence(run_id)
        checkpointed_at = (
            session.scalar(
                select(RunEvent.created_at).where(
                    RunEvent.run_id == run_id, RunEvent.sequence == latest_event
                )
            )
            if latest_event
            else run.started_at or run.created_at
        )
    mode = getattr(run, "mode", "PLATFORM_DEMO")
    is_inr = mode == "OPTION_SPREAD_SIMULATION"
    return LiveRunSnapshotResponse(
        run_id=run.id,
        status=run.status,
        progress=run.progress,
        processed_candles=int(metrics.get("processed_candles", 0)),
        total_candles=int(metrics.get("estimated_candles", metrics.get("processed_candles", 0))),
        last_processed_candle_at=datetime.fromisoformat(last_timestamp)
        if isinstance(last_timestamp, str)
        else None,
        latest_signal_sequence=repository.latest_signal_sequence(run_id),
        latest_trade_sequence=repository.latest_trade_sequence(run_id),
        latest_equity_sequence=repository.latest_equity_sequence(run_id),
        latest_event_sequence=repository.latest_event_sequence(run_id),
        checkpointed_at=checkpointed_at,
        synthetic=not is_inr,
        unit="INR" if is_inr else "DEMO_UNITS",
        financial_pnl_available=is_inr,
    )


@router.get("/{run_id}/trades", response_model=DemoTradeListResponse)
def list_trades(
    run_id: UUID,
    request: Request,
    cursor: int | None = None,
    limit: int = 100,
    session: Session = Depends(get_db_session),
    after_sequence: int | None = Query(default=None, ge=0),
    start: datetime | None = None,
    end: datetime | None = None,
    exit_date_from: date | None = None,
    exit_date_to: date | None = None,
    outcome: Literal["WIN", "LOSS", "BREAKEVEN"] | None = None,
    exit_reason: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    sort: Literal[
        "SEQUENCE_ASC", "SEQUENCE_DESC", "EXIT_TIMESTAMP_ASC", "EXIT_TIMESTAMP_DESC"
    ] = "SEQUENCE_ASC",
) -> DemoTradeListResponse | JSONResponse:
    repository = BacktestRepository(session)
    if repository.get_run(run_id) is None:
        return _not_found(request)
    invalid_range = _validate_timestamp_range(request, start, end)
    if invalid_range is not None:
        return invalid_range
    if exit_date_from is not None and exit_date_to is not None and exit_date_from > exit_date_to:
        return _error(
            request,
            BacktestServiceError(
                "INVALID_TRADE_DATE_RANGE",
                "exit_date_from must be on or before exit_date_to.",
                422,
            ),
        )
    cursor = after_sequence if isinstance(after_sequence, int) else cursor
    advanced = (
        any((start, end, exit_date_from, exit_date_to, outcome, exit_reason))
        or sort != "SEQUENCE_ASC"
    )
    if not advanced:
        # Preserve the phase-5 cursor/order contract for older clients.
        values, next_cursor = repository.list_trades(run_id, cursor, min(max(limit, 1), 200))
    else:
        if cursor is not None:
            cursor_value = repository.get_trade_by_sequence(run_id, cursor)
            if (
                cursor_value is None
                or (start is not None and cursor_value.exit_timestamp < start)
                or (end is not None and cursor_value.entry_timestamp > end)
                or (outcome == "WIN" and cursor_value.net_demo_units <= 0)
                or (outcome == "LOSS" and cursor_value.net_demo_units >= 0)
                or (outcome == "BREAKEVEN" and cursor_value.net_demo_units != 0)
                or (exit_reason is not None and cursor_value.exit_reason != exit_reason)
            ):
                return _invalid_filtered_cursor(request)
        exchange_timezone = "Asia/Kolkata"
        analytics = repository.get_analytics(run_id)
        if analytics is not None:
            exchange_timezone = analytics.timezone
        zone = ZoneInfo(exchange_timezone)
        filters = TradeListFilters(
            start=start,
            end=end,
            exit_from=(
                datetime.combine(exit_date_from, time.min, tzinfo=zone).astimezone(UTC)
                if exit_date_from is not None
                else None
            ),
            exit_to=(
                datetime.combine(
                    exit_date_to + timedelta(days=1), time.min, tzinfo=zone
                ).astimezone(UTC)
                if exit_date_to is not None
                else None
            ),
            outcome=outcome,
            exit_reason=exit_reason,
        )
        values, next_cursor = repository.list_trades(
            run_id, cursor, min(max(limit, 1), 200), filters=filters, sort=sort
        )
    return DemoTradeListResponse(
        items=[_trade(value) for value in values],
        next_cursor=next_cursor,
    )


@router.get("/{run_id}/equity", response_model=DemoEquityPointListResponse)
def list_equity(
    run_id: UUID,
    request: Request,
    cursor: int | None = None,
    limit: int = 500,
    session: Session = Depends(get_db_session),
    after_sequence: int | None = Query(default=None, ge=0),
    start: datetime | None = None,
    end: datetime | None = None,
) -> DemoEquityPointListResponse | JSONResponse:
    repository = BacktestRepository(session)
    if repository.get_run(run_id) is None:
        return _not_found(request)
    invalid_range = _validate_timestamp_range(request, start, end)
    if invalid_range is not None:
        return invalid_range
    if start is not None or end is not None:
        effective_cursor = after_sequence if isinstance(after_sequence, int) else cursor
        if effective_cursor is not None:
            cursor_value = repository.get_equity_point_by_sequence(run_id, effective_cursor)
            if (
                cursor_value is None
                or (start is not None and cursor_value.timestamp < start)
                or (end is not None and cursor_value.timestamp > end)
            ):
                return _invalid_filtered_cursor(request)
        values, next_cursor = repository.list_equity_points(
            run_id,
            after_sequence if isinstance(after_sequence, int) else cursor,
            min(max(limit, 1), 1000),
            filters=EquityListFilters(start=start, end=end),
        )
    else:
        values, next_cursor = repository.list_equity_points(
            run_id,
            after_sequence if isinstance(after_sequence, int) else cursor,
            min(max(limit, 1), 1000),
        )
    run = repository.get_run(run_id)
    if run is None:
        return _not_found(request)
    mode = getattr(run, "mode", "PLATFORM_DEMO")
    is_inr = mode == "OPTION_SPREAD_SIMULATION"
    return DemoEquityPointListResponse(
        items=[
            DemoEquityPointResponse(
                id=value.id,
                sequence=value.sequence,
                timestamp=value.timestamp,
                equity_demo_units=value.equity_demo_units,
                drawdown_demo_units=value.drawdown_demo_units,
                equity_amount=value.equity_demo_units,
                drawdown_amount=value.drawdown_demo_units,
                synthetic=getattr(value, "synthetic", not is_inr),
                unit=getattr(value, "unit", "INR" if is_inr else "DEMO_UNITS"),
                financial_pnl_available=getattr(value, "financial_pnl_available", is_inr),
            )
            for value in values
        ],
        next_cursor=next_cursor,
    )


@router.get("/{run_id}/events", response_model=RunEventListResponse)
def list_events(
    run_id: UUID,
    request: Request,
    cursor: int | None = None,
    limit: int = 100,
    session: Session = Depends(get_db_session),
    after_sequence: int | None = Query(default=None, ge=0),
    start: datetime | None = None,
    end: datetime | None = None,
    event_type: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> RunEventListResponse | JSONResponse:
    repository = BacktestRepository(session)
    if repository.get_run(run_id) is None:
        return _not_found(request)
    invalid_range = _validate_timestamp_range(request, start, end)
    if invalid_range is not None:
        return invalid_range
    if start is not None or end is not None or event_type is not None:
        effective_cursor = after_sequence if isinstance(after_sequence, int) else cursor
        if effective_cursor is not None:
            cursor_value = repository.get_event_by_sequence(run_id, effective_cursor)
            if (
                cursor_value is None
                or (start is not None and cursor_value.created_at < start)
                or (end is not None and cursor_value.created_at > end)
                or (event_type is not None and cursor_value.event_type != event_type)
            ):
                return _invalid_filtered_cursor(request)
        values, next_cursor = repository.list_events(
            run_id,
            after_sequence if isinstance(after_sequence, int) else cursor,
            min(max(limit, 1), 200),
            filters=EventListFilters(start=start, end=end, event_type=event_type),
        )
    else:
        values, next_cursor = repository.list_events(
            run_id,
            after_sequence if isinstance(after_sequence, int) else cursor,
            min(max(limit, 1), 200),
        )
    return RunEventListResponse(
        items=[
            RunEventResponse(
                id=value.id,
                sequence=value.sequence,
                event_type=value.event_type,
                payload=value.payload,
                created_at=value.created_at,
            )
            for value in values
        ],
        next_cursor=next_cursor,
    )


@router.get("/{run_id}/metrics", response_model=RunMetricsResponse)
def get_metrics(
    run_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> RunMetricsResponse | JSONResponse:
    run = BacktestRepository(session).get_run(run_id)
    if run is None:
        return _not_found(request)
    return RunMetricsResponse(
        run_id=run.id,
        metrics=(
            run.metrics.metrics
            if run.metrics
            else {
                "mode": run.mode,
                "available": False,
                "synthetic": run.mode == "PLATFORM_DEMO",
                "unit": "DEMO_UNITS" if run.mode == "PLATFORM_DEMO" else None,
                "financial_pnl_available": False,
            }
        ),
    )


router.include_router(backtest_analytics.router)


@router.get(
    "/{run_id}/exports/{kind}",
    response_model=JsonExportResponse,
    responses={200: {"content": {"text/csv": {}}}},
)
def export_backtest(
    run_id: UUID,
    kind: Literal["summary", "trades", "signals", "analytics"],
    request: Request,
    format: Literal["json", "csv"] = "json",
    session: Session = Depends(get_db_session),
    granularity: Literal["TRADE", "DAY", "MONTH"] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    include_zero_periods: bool = True,
    sort: Literal["PERIOD_ASC", "PERIOD_DESC"] = "PERIOD_ASC",
) -> JsonExportResponse | Response:
    started = monotonic()
    metrics = getattr(getattr(request.scope.get("app"), "state", None), "metrics", None)
    try:
        if kind == "analytics" and granularity is not None:
            payload = ResultService(session).export(
                run_id,
                kind,
                granularity=granularity,
                start_date=start_date,
                end_date=end_date,
                include_zero_periods=include_zero_periods,
                sort=sort,
            )
        else:
            payload = ResultService(session).export(run_id, kind)
    except BacktestServiceError as exc:
        if metrics is not None and kind == "analytics" and granularity is not None:
            metrics.increment(
                "stockbot_analytics_export_failures_total",
                {"granularity": granularity, "code": exc.code},
            )
        return _error(request, exc)
    finally:
        if metrics is not None and kind == "analytics" and granularity is not None:
            metrics.observe(
                "stockbot_analytics_export_duration_seconds",
                monotonic() - started,
                {"granularity": granularity, "format": format},
            )
    filename = f"stockbot-{run_id}-{kind}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if format == "json":
        model = JsonExportResponse.model_validate(payload)
        return JSONResponse(content=jsonable_encoder(model), headers=headers)
    output = StringIO()
    rows = payload["rows"] or [{"run_id": str(run_id), "record_type": "EXPORT_METADATA_ONLY"}]
    metadata = json.dumps(jsonable_encoder(payload["metadata"]), sort_keys=True)
    fieldnames = ["export_metadata"] + sorted(
        {key for row in rows for key in row} if rows else {"run_id"}
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        serialized = {
            key: json.dumps(jsonable_encoder(value), sort_keys=True)
            if isinstance(value, (dict, list))
            else value
            for key, value in row.items()
        }
        writer.writerow({"export_metadata": metadata} | serialized)
    return Response(
        content=output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers
    )
