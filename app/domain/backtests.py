"""Backtest status and compatibility entry point for versioned signal evaluators."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from enum import StrEnum

from app.domain.algorithms import ConfiguredAlgorithm, PlatformDemoParameters
from app.domain.bull_call_spread_signals import _window as _window
from app.domain.bull_call_spread_signals import evaluate_bull_call_spread
from app.domain.indicators import WilderRsi as WilderRsi  # Compatibility for historical imports.
from app.domain.indicators import _rsi as _rsi
from app.domain.platform_demo_signals import _evaluate_platform_demo
from app.domain.signal_models import EXCHANGE_OFFSET as EXCHANGE_OFFSET
from app.domain.signal_models import MarketCandle as MarketCandle
from app.domain.signal_models import MarketDataProvider as MarketDataProvider
from app.domain.signal_models import SignalDecision as SignalDecision

ENGINE_VERSION = "1.2.3-market-journal"


class BacktestStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_BACKTEST_STATUSES = {
    BacktestStatus.COMPLETED,
    BacktestStatus.FAILED,
    BacktestStatus.CANCELLED,
}


def evaluate_chronologically(
    candles: Iterable[MarketCandle],
    algorithm: ConfiguredAlgorithm,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[SignalDecision]:
    """Evaluate one completed candle at a time without retaining future records."""
    parameters = algorithm.parameters
    if algorithm.key == "platform_demo":
        if not isinstance(parameters, PlatformDemoParameters):
            raise TypeError("Platform demo requires PlatformDemoParameters.")
        yield from _evaluate_platform_demo(candles, parameters, should_cancel=should_cancel)
        return
    yield from evaluate_bull_call_spread(candles, algorithm, should_cancel=should_cancel)
