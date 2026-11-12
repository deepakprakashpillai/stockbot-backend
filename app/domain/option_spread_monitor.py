"""Five-minute close monitoring for the retained spread research versions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.decision_checks import check
from app.domain.futures_comparison import futures_comparison_available
from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    OpenPositionDataGapError,
    OptionExecutionCancelled,
    OptionExitReason,
    OptionSpreadAssumptions,
)
from app.domain.option_spread_pricing import _normalize_ts


@dataclass(frozen=True)
class MonitoredExit:
    timestamp: datetime
    reason: OptionExitReason
    buy_candle: Any
    sell_candle: Any


def monitor_spread_exit(
    *,
    session_candles: list[Any],
    buy_candles: dict[datetime, Any],
    sell_candles: dict[datetime, Any],
    entry_timestamp: datetime,
    assumptions: OptionSpreadAssumptions,
    stop_level: Decimal,
    target_level: Decimal,
    trend_level: Decimal,
    signal_sequence: int,
    emit: Callable[..., None],
    should_cancel: Callable[[], bool] | None,
) -> MonitoredExit:
    """Return the first retained-policy exit and emit each observed checkpoint."""

    for candle in session_candles:
        if should_cancel is not None and should_cancel():
            raise OptionExecutionCancelled(
                "Backtest cancellation requested while monitoring a position."
            )
        timestamp = _normalize_ts(candle.timestamp)
        if timestamp <= entry_timestamp:
            continue
        elapsed = int((timestamp - entry_timestamp).total_seconds() // 60)
        if (
            elapsed % assumptions.monitoring_interval_minutes
            and timestamp.time() < assumptions.square_off_time
        ):
            continue
        missing: list[str] = []
        if timestamp not in buy_candles:
            missing.append("long_option_close")
        if timestamp not in sell_candles:
            missing.append("short_option_close")
        if candle.futures_vwap is None:
            missing.append("futures_vwap")
        if candle.india_vix is None:
            missing.append("india_vix")
        if assumptions.use_futures_price and not futures_comparison_available(candle):
            missing.append("synchronized_futures_close")
        if missing:
            raise OpenPositionDataGapError(timestamp, missing)
        comparison_price = candle.futures_close if assumptions.use_futures_price else candle.close
        price_label = "Futures" if assumptions.use_futures_price else "Spot"
        current_buy = buy_candles[timestamp]
        current_sell = sell_candles[timestamp]
        current_spread = (
            Decimal(str(current_buy.close)) - Decimal(str(current_sell.close))
        ).quantize(FINANCIAL_DECIMAL_QUANTUM)
        exit_reason: OptionExitReason | None = None
        if current_spread <= stop_level:
            exit_reason = OptionExitReason.STOP_LOSS
        elif current_spread >= target_level:
            exit_reason = OptionExitReason.TARGET_REACHED
        elif (
            Decimal(str(comparison_price)) < Decimal(str(candle.futures_vwap))
            and current_spread < trend_level
        ):
            exit_reason = OptionExitReason.TREND_REVERSAL
        elif timestamp.time() >= assumptions.square_off_time:
            exit_reason = OptionExitReason.SESSION_SQUARE_OFF
        emit(
            "POSITION_CHECK",
            timestamp,
            signal_sequence=signal_sequence,
            position_state="CHECKING",
            buy_reference_close=str(current_buy.close),
            sell_reference_close=str(current_sell.close),
            spread_value_points=str(current_spread),
            spot_close=str(candle.close),
            futures_vwap=str(candle.futures_vwap),
            stop_level_points=str(stop_level),
            target_level_points=str(target_level),
            trend_reversal_level_points=str(trend_level),
            hard_exit_time=assumptions.square_off_time.isoformat(),
            evidence_version="position-checks-v3"
            if assumptions.use_futures_price
            else "position-checks-v2",
            **(
                {
                    "futures_close": str(candle.futures_close),
                    "vwap_comparison_source": "futures_close",
                }
                if assumptions.use_futures_price
                else {}
            ),
            action=exit_reason.value if exit_reason is not None else "HOLD",
            checks=[
                check(
                    "stop",
                    "Spread is at or below stop",
                    str(current_spread),
                    "<=",
                    str(stop_level),
                    current_spread <= stop_level,
                ),
                check(
                    "target",
                    "Spread is at or above target",
                    str(current_spread),
                    ">=",
                    str(target_level),
                    current_spread >= target_level if current_spread > stop_level else None,
                ),
                check(
                    "trend_price",
                    f"{price_label} is below futures VWAP",
                    str(comparison_price),
                    "<",
                    str(candle.futures_vwap),
                    Decimal(str(comparison_price)) < Decimal(str(candle.futures_vwap))
                    if stop_level < current_spread < target_level
                    else None,
                ),
                check(
                    "trend_spread",
                    f"Spread is below trend threshold (AND {price_label.lower()} below VWAP)",
                    str(current_spread),
                    "<",
                    str(trend_level),
                    current_spread < trend_level
                    if stop_level < current_spread < target_level
                    and Decimal(str(comparison_price)) < Decimal(str(candle.futures_vwap))
                    else None,
                ),
                check(
                    "hard_exit",
                    "Time is at or after hard exit",
                    timestamp.time().isoformat(),
                    ">=",
                    assumptions.square_off_time.isoformat(),
                    timestamp.time() >= assumptions.square_off_time
                    if exit_reason in {None, OptionExitReason.SESSION_SQUARE_OFF}
                    else None,
                ),
            ],
        )
        if exit_reason is not None:
            return MonitoredExit(timestamp, exit_reason, current_buy, current_sell)
    raise OpenPositionDataGapError(entry_timestamp, ["hard_exit_checkpoint"])
