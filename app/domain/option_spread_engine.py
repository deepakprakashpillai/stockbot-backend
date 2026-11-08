"""Pure historical option-spread simulation; no database or worker dependencies."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.option_spread_equity import build_equity_points
from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    OptionExecutionCancelled,
    OptionSpreadAssumptions,
    OptionSpreadExecutionRejection,
    OptionSpreadExecutionResult,
    OptionSpreadTradeResult,
)
from app.domain.option_spread_monitor import monitor_spread_exit
from app.domain.option_spread_pricing import _normalize_ts, _rejection, _round_to_increment
from app.domain.option_spread_selection import select_entry_spread
from app.domain.option_spread_settlement import OpenedSpread, settle_spread


def simulate_option_spread_backtest(
    signals: list[Any],
    derivatives_provider: Any,
    spot_candles: list[Any],
    assumptions: OptionSpreadAssumptions = OptionSpreadAssumptions(),
    strike_width: int | None = None,
    observer: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    *,
    signal_sequence_start: int = 1,
    trade_sequence_start: int = 1,
    emit_all_rejections: bool = False,
) -> OptionSpreadExecutionResult:
    """Run the approved V1 spread state machine using estimated historical fills."""

    if strike_width is not None:
        assumptions = OptionSpreadAssumptions(
            **{
                **assumptions.__dict__,
                "primary_spread_width": strike_width,
            }
        )

    normalized_spot = sorted(
        [
            candle.__class__(
                timestamp=_normalize_ts(candle.timestamp),
                high=candle.high,
                close=candle.close,
                futures_vwap=candle.futures_vwap,
                india_vix=candle.india_vix,
                previous_india_vix=getattr(candle, "previous_india_vix", None),
                futures_close=getattr(candle, "futures_close", None),
                futures_contract_id=getattr(candle, "futures_contract_id", None),
                futures_open_interest=getattr(candle, "futures_open_interest", None),
                futures_observed_at=getattr(candle, "futures_observed_at", None),
                india_vix_observed_at=getattr(candle, "india_vix_observed_at", None),
            )
            for candle in spot_candles
        ],
        key=lambda candle: candle.timestamp,
    )
    candles_by_date: dict[Any, list[Any]] = {}
    for candle in normalized_spot:
        candles_by_date.setdefault(candle.timestamp.date(), []).append(candle)
    signals_by_date: dict[Any, list[tuple[int, Any]]] = {}
    for sequence, signal in enumerate(signals, start=signal_sequence_start):
        signals_by_date.setdefault(signal.session_date, []).append((sequence, signal))

    trades: list[OptionSpreadTradeResult] = []
    rejections: list[OptionSpreadExecutionRejection] = []
    daily_net: dict[Any, Decimal] = {}

    def emit(event_type: str, timestamp: datetime, **payload: object) -> None:
        if observer is not None:
            observer({"event_type": event_type, "timestamp": timestamp, "payload": payload})

    for session_date in sorted(candles_by_date):
        session_candles = candles_by_date[session_date]
        spot_by_timestamp = {candle.timestamp: candle for candle in session_candles}
        accepted_signals = [
            (sequence, signal)
            for sequence, signal in signals_by_date.get(session_date, [])
            if str(getattr(signal, "decision", "")) in {"BUY", "ACCEPT", "ACCEPTED"}
        ]
        if not accepted_signals:
            continue
        try:
            session_data = derivatives_provider.load_session(
                session_date,
                session_candles,
                allow_same_day=assumptions.same_day_weekly_expiry,
            )
        except TypeError:
            session_data = derivatives_provider.load_session(session_date, session_candles)
        option_candles_by_strike: dict[tuple[int, str], dict[datetime, Any]] = {}
        trades_today = 0
        traded_in_window_1 = False

        for signal_sequence, signal in accepted_signals:
            if should_cancel is not None and should_cancel():
                raise OptionExecutionCancelled(
                    "Backtest cancellation requested during option execution."
                )
            entry_timestamp = _normalize_ts(signal.timestamp)

            def reject(
                reason: str,
                *,
                force_event: bool = False,
                sequence: int = signal_sequence,
                timestamp: datetime = entry_timestamp,
                **details: object,
            ) -> None:
                rejection = _rejection(sequence, timestamp, reason, **details)
                rejections.append(rejection)
                if emit_all_rejections or force_event:
                    emit(
                        "ENTRY_REJECTED",
                        timestamp,
                        signal_sequence=sequence,
                        reason=reason,
                        **rejection.details,
                    )

            emit(
                "ENTRY_CANDIDATE",
                entry_timestamp,
                signal_sequence=signal_sequence,
                window=signal.window,
                score=signal.score,
            )
            if trades_today >= assumptions.max_trades_per_day:
                reject(
                    "MAX_TRADES_PER_DAY_REACHED",
                    trades_today=trades_today,
                    maximum_trades_per_day=assumptions.max_trades_per_day,
                )
                continue
            if signal.window == "window_2" and traded_in_window_1:
                reject("WINDOW_2_INELIGIBLE", traded_in_window_1=traded_in_window_1)
                continue
            if daily_net.get(session_date, Decimal("0")) <= -assumptions.maximum_daily_loss:
                reject(
                    "MAXIMUM_DAILY_LOSS_REACHED",
                    daily_net=str(daily_net.get(session_date, Decimal("0"))),
                    loss_boundary=str(-assumptions.maximum_daily_loss),
                )
                continue

            entry_spot = spot_by_timestamp.get(entry_timestamp)
            if entry_spot is None:
                reject("SYNCHRONIZED_SPOT_PRICE_MISSING")
                continue
            atm_strike = _round_to_increment(
                Decimal(str(entry_spot.close)), assumptions.atm_increment
            )

            selection = select_entry_spread(
                session_data,
                option_candles_by_strike,
                entry_timestamp,
                atm_strike,
                assumptions,
            )
            if selection.selected is None:
                reject(
                    selection.rejection_reason,
                    force_event=True,
                    atm_strike=atm_strike,
                    attempted_widths=selection.attempted_widths,
                    attempts=selection.attempts,
                )
                continue

            selected = selection.selected
            buy_contract = selected.buy_contract
            sell_contract = selected.sell_contract
            buy_candles = selected.buy_candles
            sell_candles = selected.sell_candles
            width = selected.width
            entry_buy_fill = selected.buy_fill
            entry_sell_fill = selected.sell_fill
            entry_debit = selected.debit
            lot_size = int(
                getattr(buy_contract, "lot_size", 0)
                or getattr(sell_contract, "lot_size", 0)
                or derivatives_provider.get_lot_size(session_date)
            )
            sell_lot_size = int(getattr(sell_contract, "lot_size", lot_size))
            if sell_lot_size != lot_size:
                reject(
                    "OPTION_LOT_SIZE_MISMATCH",
                    buy_lot_size=lot_size,
                    sell_lot_size=sell_lot_size,
                )
                continue
            quantity = lot_size * assumptions.lots
            width_decimal = Decimal(width)
            maximum_loss_inr = (entry_debit * quantity).quantize(FINANCIAL_DECIMAL_QUANTUM)
            maximum_profit_inr = ((width_decimal - entry_debit) * quantity).quantize(
                FINANCIAL_DECIMAL_QUANTUM
            )
            stop_level = (
                entry_debit * (Decimal("1") - assumptions.stop_loss_percent / Decimal("100"))
            ).quantize(FINANCIAL_DECIMAL_QUANTUM)
            target_level = (
                entry_debit
                + (width_decimal - entry_debit)
                * assumptions.target_max_profit_percent
                / Decimal("100")
            ).quantize(FINANCIAL_DECIMAL_QUANTUM)
            trend_level = (
                entry_debit
                * (Decimal("1") - assumptions.trend_spread_decline_percent / Decimal("100"))
            ).quantize(FINANCIAL_DECIMAL_QUANTUM)
            emit(
                "POSITION_OPENED",
                entry_timestamp,
                signal_sequence=signal_sequence,
                position_state="OPEN",
                expiry=str(getattr(buy_contract, "expiry", "")),
                buy_strike=atm_strike,
                sell_strike=atm_strike + width,
                lot_size=lot_size,
                lot_size_source="selected_option_contract_metadata",
                lots=assumptions.lots,
                quantity=quantity,
                entry_buy_reference=str(buy_candles[entry_timestamp].close),
                entry_sell_reference=str(sell_candles[entry_timestamp].close),
                entry_buy_fill=str(entry_buy_fill),
                entry_sell_fill=str(entry_sell_fill),
                entry_debit_points=str(entry_debit),
                stop_level_points=str(stop_level),
                target_level_points=str(target_level),
                trend_reversal_level_points=str(trend_level),
                assumptions=assumptions.snapshot(),
                spread_attempts=selection.attempts,
            )

            monitored = monitor_spread_exit(
                session_candles=session_candles,
                buy_candles=buy_candles,
                sell_candles=sell_candles,
                entry_timestamp=entry_timestamp,
                assumptions=assumptions,
                stop_level=stop_level,
                target_level=target_level,
                trend_level=trend_level,
                signal_sequence=signal_sequence,
                emit=emit,
                should_cancel=should_cancel,
            )
            opened = OpenedSpread(
                selected=selected,
                entry_timestamp=entry_timestamp,
                signal_sequence=signal_sequence,
                atm_strike=atm_strike,
                lot_size=lot_size,
                quantity=quantity,
                maximum_loss_inr=maximum_loss_inr,
                maximum_profit_inr=maximum_profit_inr,
                stop_level=stop_level,
                target_level=target_level,
                trend_level=trend_level,
            )
            settled = settle_spread(opened, monitored, assumptions, session_data)
            trades.append(settled.trade)
            emit(
                "POSITION_CLOSED",
                monitored.timestamp,
                trade_sequence=trade_sequence_start + len(trades) - 1,
                **settled.close_payload,
            )
            net_pnl = settled.trade.net_pnl_inr
            daily_net[session_date] = daily_net.get(session_date, Decimal("0")) + net_pnl
            trades_today += 1
            if signal.window == "window_1":
                traded_in_window_1 = True

    equity_points = build_equity_points(
        trades,
        starting_capital=assumptions.starting_capital,
        first_market_timestamp=normalized_spot[0].timestamp if normalized_spot else None,
    )
    return OptionSpreadExecutionResult(
        trades=tuple(trades),
        equity_points=equity_points,
        rejections=tuple(rejections),
    )
