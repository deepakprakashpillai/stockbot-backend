"""Deterministic platform-demo signal evaluator."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from typing import Any

from app.domain.algorithms import PlatformDemoParameters
from app.domain.signal_models import EXCHANGE_OFFSET, MarketCandle, SignalDecision


def _evaluate_platform_demo(
    candles: Iterable[MarketCandle],
    parameters: PlatformDemoParameters,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[SignalDecision]:
    """Emit deterministic synthetic decisions from completed candles only."""

    current_session: date | None = None
    session_candle_number = 0
    remaining_hold_candles = 0
    last_timestamp: datetime | None = None
    candle_iterator = iter(candles)
    while True:
        if should_cancel and should_cancel():
            return
        try:
            candle = next(candle_iterator)
        except StopIteration:
            return
        if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() != EXCHANGE_OFFSET:
            raise ValueError("Market data timestamps must use the Asia/Kolkata +05:30 offset.")
        if candle.timestamp.second or candle.timestamp.microsecond or candle.timestamp.minute % 5:
            raise ValueError("Market data timestamps must represent completed five-minute candles.")
        if last_timestamp is not None and candle.timestamp <= last_timestamp:
            raise ValueError("Market data must be strictly chronological.")
        last_timestamp = candle.timestamp

        session = candle.timestamp.date()
        if current_session != session:
            current_session = session
            session_candle_number = 0
            remaining_hold_candles = 0
        session_candle_number += 1

        inputs: dict[str, Any] = {
            "close": candle.close,
            "high": candle.high,
            "session_candle_number": session_candle_number,
            "signal_every_n_candles": parameters.signal_every_n_candles,
            "hold_candles": parameters.hold_candles,
            "remaining_hold_candles_before_decision": remaining_hold_candles,
            "completed_candle": True,
            "synthetic": True,
            "financial_pnl_available": False,
        }
        if remaining_hold_candles:
            remaining_hold_candles -= 1
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "DEMO_HOLD_ACTIVE",
                "platform_demo",
                0,
                inputs,
            )
            continue
        if session_candle_number % parameters.signal_every_n_candles:
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "DEMO_INTERVAL_NOT_REACHED",
                "platform_demo",
                0,
                inputs,
            )
            continue
        remaining_hold_candles = parameters.hold_candles
        yield SignalDecision(
            candle.timestamp,
            session,
            "ACCEPTED",
            None,
            "platform_demo",
            1,
            inputs,
        )
