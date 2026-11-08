"""Session-ordered research pipeline with bounded market-observation buffering."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from itertools import groupby, tee
from typing import Any

from app.domain.algorithms import ConfiguredAlgorithm
from app.domain.backtests import MarketCandle, SignalDecision, evaluate_chronologically
from app.domain.option_spread_engine import simulate_option_spread_backtest
from app.domain.option_spread_equity import build_equity_points
from app.domain.option_spread_models import (
    OptionExecutionCancelled,
    OptionSpreadAssumptions,
    OptionSpreadExecutionRejection,
    OptionSpreadExecutionResult,
    OptionSpreadTradeResult,
)


def run_chronological_option_research(
    observations: Iterable[MarketCandle],
    algorithm: ConfiguredAlgorithm,
    derivatives_provider: Any,
    assumptions: OptionSpreadAssumptions,
    *,
    requested_start: date,
    requested_end: date,
    included_sessions: set[date] | None = None,
    on_signal: Callable[[SignalDecision], None] | None = None,
    on_session_signals_complete: Callable[[], None] | None = None,
    on_session_events_complete: Callable[[list[dict[str, Any]]], None] | None = None,
    observer: Callable[[dict[str, Any]], None] | None = None,
    signal_should_cancel: Callable[[], bool] | None = None,
    execution_should_cancel: Callable[[], bool] | None = None,
) -> OptionSpreadExecutionResult:
    """Evaluate and execute each session before reading the next session.

    The two iterators stay within one session of each other. The function retains
    only the day's candles/decisions plus final trades and execution rejections.
    """
    for_signals, for_execution = tee(observations)
    market_days = groupby(for_execution, key=lambda candle: candle.timestamp.date())
    decisions_by_day = groupby(
        evaluate_chronologically(for_signals, algorithm, should_cancel=signal_should_cancel),
        key=lambda decision: decision.session_date,
    )
    trades: list[OptionSpreadTradeResult] = []
    rejections: list[OptionSpreadExecutionRejection] = []
    signal_sequence = 1
    first_timestamp = None

    for session_date, decision_group in decisions_by_day:
        market_date, candle_group = next(market_days)
        if market_date != session_date:
            raise ValueError("Market observations and signal decisions lost session alignment.")
        candles = list(candle_group)
        decisions = list(decision_group)
        if not requested_start <= session_date <= requested_end or (
            included_sessions is not None and session_date not in included_sessions
        ):
            continue
        if first_timestamp is None and candles:
            first_timestamp = candles[0].timestamp
        for decision in decisions:
            if on_signal is not None:
                on_signal(decision)
        if on_session_signals_complete is not None:
            on_session_signals_complete()
        session_events: list[dict[str, Any]] = []
        try:
            session_result = simulate_option_spread_backtest(
                signals=decisions,
                derivatives_provider=derivatives_provider,
                spot_candles=candles,
                assumptions=assumptions,
                observer=session_events.append
                if on_session_events_complete is not None
                else observer,
                should_cancel=execution_should_cancel,
                signal_sequence_start=signal_sequence,
                trade_sequence_start=len(trades) + 1,
                emit_all_rejections=on_session_events_complete is not None,
            )
        except OptionExecutionCancelled:
            if on_session_events_complete is not None:
                on_session_events_complete(session_events)
            raise
        if on_session_events_complete is not None:
            on_session_events_complete(session_events)
        trades.extend(session_result.trades)
        rejections.extend(session_result.rejections)
        signal_sequence += len(decisions)

    return OptionSpreadExecutionResult(
        trades=tuple(trades),
        equity_points=build_equity_points(
            trades,
            starting_capital=assumptions.starting_capital,
            first_market_timestamp=first_timestamp,
        ),
        rejections=tuple(rejections),
    )
