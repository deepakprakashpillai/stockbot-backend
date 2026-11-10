"""Session replay for the clarified historical research policy."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    OptionExecutionCancelled,
    OptionExitReason,
    OptionSpreadAssumptions,
    OptionSpreadTradeResult,
)
from app.domain.option_spread_monitor import MonitoredExit
from app.domain.option_spread_pricing import _normalize_ts, _round_to_increment
from app.domain.option_spread_selection import (
    SelectedSpread,
    option_reference,
    select_entry_spread,
)
from app.domain.option_spread_settlement import OpenedSpread, settle_spread
from app.domain.signal_models import MarketCandle, SignalDecision
from app.domain.trading_calendar import NSE_FO_CALENDAR, TradingSessionCalendar

BAR = timedelta(minutes=5)


@dataclass(frozen=True)
class ExcludedSpread:
    signal_sequence: int
    entry_timestamp: datetime
    maximum_loss_inr: Decimal


@dataclass(frozen=True)
class PendingEntry:
    sequence: int
    strike: int
    window: str | None
    vix: Decimal | None


@dataclass(frozen=True)
class SessionExclusion:
    session_date: date
    timestamp: datetime
    missing: tuple[str, ...]
    spreads: tuple[ExcludedSpread, ...]


@dataclass(frozen=True)
class ClockSessionResult:
    session_date: date
    trades: tuple[OptionSpreadTradeResult, ...]
    exclusion: SessionExclusion | None = None


@dataclass(frozen=True)
class ClockResearchEvidence:
    opened_trades: int
    excluded_trades: int
    valid_exclusion_rate: bool
    net_result_inr: Decimal
    pessimistic_net_result_inr: Decimal
    excluded_sessions: tuple[SessionExclusion, ...]


@dataclass(frozen=True)
class CommonSessionEvidence:
    sessions: tuple[date, ...]
    eligible_session_count: int
    meets_minimum_coverage: bool
    net_result_by_variant: dict[str, Decimal]


def compare_clock_sessions(
    variants: dict[str, Sequence[ClockSessionResult]], *, eligible_sessions: set[date]
) -> CommonSessionEvidence:
    """Compare results on the intersection after variant-specific exclusions."""
    if not variants or not eligible_sessions:
        raise ValueError("A comparison requires variants and a fixed eligible session set.")
    included = {
        name: {
            session.session_date: session
            for session in sessions
            if session.exclusion is None and session.session_date in eligible_sessions
        }
        for name, sessions in variants.items()
    }
    common = eligible_sessions.intersection(*(set(sessions) for sessions in included.values()))
    common_dates = tuple(sorted(common))
    return CommonSessionEvidence(
        common_dates,
        len(eligible_sessions),
        len(common_dates) * 100 >= len(eligible_sessions) * 90,
        {
            name: summarize_clock_sessions(sessions[day] for day in common_dates).net_result_inr
            for name, sessions in included.items()
        },
    )


def summarize_clock_sessions(sessions: Iterable[ClockSessionResult]) -> ClockResearchEvidence:
    net = Decimal("0.00")
    maximum_losses = Decimal("0.00")
    opened = excluded = 0
    exclusions: list[SessionExclusion] = []
    for session in sessions:
        net += sum((trade.net_pnl_inr for trade in session.trades), Decimal("0"))
        opened += len(session.trades)
        if session.exclusion is not None:
            exclusions.append(session.exclusion)
            excluded += len(session.exclusion.spreads)
            maximum_losses += sum(
                (spread.maximum_loss_inr for spread in session.exclusion.spreads), Decimal("0")
            )
    opened += excluded
    return ClockResearchEvidence(
        opened, excluded, excluded * 100 <= opened * 2, net, net - maximum_losses, tuple(exclusions)
    )


def _open_spread(
    selected: SelectedSpread,
    timestamp: datetime,
    signal_sequence: int,
    strike: int,
    assumptions: OptionSpreadAssumptions,
) -> OpenedSpread:
    lot_size = selected.buy_contract.lot_size
    quantity = lot_size * assumptions.lots
    debit = selected.debit
    width = Decimal(selected.width)
    quantum = FINANCIAL_DECIMAL_QUANTUM
    return OpenedSpread(
        selected=selected,
        entry_timestamp=timestamp,
        signal_sequence=signal_sequence,
        atm_strike=strike,
        lot_size=lot_size,
        quantity=quantity,
        maximum_loss_inr=(debit * quantity).quantize(quantum),
        maximum_profit_inr=((width - debit) * quantity).quantize(quantum),
        stop_level=(debit * (1 - assumptions.stop_loss_percent / 100)).quantize(quantum),
        target_level=(
            debit + (width - debit) * assumptions.target_max_profit_percent / 100
        ).quantize(quantum),
        trend_level=(debit * (1 - assumptions.trend_spread_decline_percent / 100)).quantize(
            quantum
        ),
    )


def _monitor(
    position: OpenedSpread,
    candle: MarketCandle | None,
    timestamp: datetime,
    square_off: bool,
    vix_at_entry: Decimal | None,
) -> tuple[OptionExitReason | None, tuple[str, ...]]:
    buy = option_reference(position.selected.buy_candles.get(timestamp))
    sell = option_reference(position.selected.sell_candles.get(timestamp))
    missing = []
    if buy is None:
        missing.append("long_option_close")
    if sell is None:
        missing.append("short_option_close")
    if candle is None:
        missing.append("spot_checkpoint")
    else:
        if candle.futures_close is None:
            missing.append("futures_close")
        if candle.futures_vwap is None:
            missing.append("futures_vwap")
        if vix_at_entry is not None and candle.india_vix is None:
            missing.append("india_vix")
    if missing:
        return None, tuple(missing)
    assert buy is not None and sell is not None and candle is not None
    assert candle.futures_close is not None and candle.futures_vwap is not None
    spread = (buy - sell).quantize(FINANCIAL_DECIMAL_QUANTUM)
    if spread <= position.stop_level:
        return OptionExitReason.STOP_LOSS, ()
    if spread >= position.target_level:
        return OptionExitReason.TARGET_REACHED, ()
    if candle.futures_close < candle.futures_vwap and spread < position.trend_level:
        return OptionExitReason.TREND_REVERSAL, ()
    if (
        vix_at_entry is not None
        and candle.india_vix is not None
        and Decimal(str(candle.india_vix)) >= vix_at_entry * Decimal("1.15")
    ):
        return OptionExitReason.VIX_SPIKE, ()
    return (OptionExitReason.SESSION_SQUARE_OFF if square_off else None), ()


def replay_spread_session(
    session_date: date,
    candles: Sequence[MarketCandle],
    signals: Sequence[tuple[int, SignalDecision]],
    session_data: Any,
    assumptions: OptionSpreadAssumptions = OptionSpreadAssumptions(),
    *,
    vix_spike_exit: bool = False,
    observer: Callable[[dict[str, object]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    calendar: TradingSessionCalendar = NSE_FO_CALENDAR,
) -> ClockSessionResult:
    """Consume opens before closes, with one pending order or open spread at a time.

    Option rows are indexed by their canonical close. The next row's open occurs
    at the signal's wall-clock time but after its decision in the event sequence.
    """
    checkpoints = calendar.completed_candle_times(session_date)
    if assumptions.monitoring_interval_minutes != 5:
        raise ValueError("This policy requires five-minute monitoring.")
    if assumptions.square_off_time not in {timestamp.time() for timestamp in checkpoints[:-1]}:
        raise ValueError("The session grid must include the candle after the hard exit.")
    market = {_normalize_ts(candle.timestamp): candle for candle in candles}
    accepted: dict[datetime, list[tuple[int, SignalDecision]]] = {}
    for sequence, signal in signals:
        if signal.decision in {"BUY", "ACCEPT", "ACCEPTED"}:
            accepted.setdefault(_normalize_ts(signal.timestamp), []).append((sequence, signal))

    option_cache: dict[tuple[int, str], dict[datetime, Any]] = {}
    trades: list[OptionSpreadTradeResult] = []
    opened: list[ExcludedSpread] = []
    daily_net = Decimal("0.00")
    pending_entry: PendingEntry | None = None
    pending_exit: OptionExitReason | None = None
    position: OpenedSpread | None = None
    vix_at_entry: Decimal | None = None
    capped_windows: set[str | None] = set()

    def emit(event_type: str, timestamp: datetime, **payload: object) -> None:
        if observer is not None:
            observer({"event_type": event_type, "timestamp": timestamp, "payload": payload})

    def exclude(timestamp: datetime, missing: tuple[str, ...]) -> ClockSessionResult:
        emit("SESSION_EXCLUDED", timestamp, missing=missing)
        return ClockSessionResult(
            session_date, (), SessionExclusion(session_date, timestamp, missing, tuple(opened))
        )

    for timestamp in checkpoints:
        if should_cancel is not None and should_cancel():
            raise OptionExecutionCancelled("Backtest cancellation requested during spread replay.")
        open_time = timestamp - BAR
        if pending_exit is not None:
            assert position is not None
            buy = position.selected.buy_candles.get(timestamp)
            sell = position.selected.sell_candles.get(timestamp)
            missing = tuple(
                label
                for label, row in (("long_option_open", buy), ("short_option_open", sell))
                if option_reference(row, use_open=True) is None
            )
            if missing:
                return exclude(open_time, missing)
            settled = settle_spread(
                position,
                MonitoredExit(open_time, pending_exit, buy, sell),
                assumptions,
                session_data,
                use_open=True,
            )
            trades.append(settled.trade)
            daily_net += settled.trade.net_pnl_inr
            emit(
                "POSITION_CLOSED", open_time, daily_net_inr=str(daily_net), **settled.close_payload
            )
            position = None
            pending_exit = None
        elif pending_entry is not None:
            entry = pending_entry
            pending_entry = None
            sequence, strike, vix_at_entry = entry.sequence, entry.strike, entry.vix
            selection = select_entry_spread(
                session_data, option_cache, timestamp, strike, assumptions, use_open=True
            )
            selected = selection.selected
            if selected is None:
                if (
                    len(selection.attempts) == len(selection.attempted_widths)
                    and all(
                        Decimal(str(attempt["net_debit_points"]))
                        > Decimal(str(attempt["maximum_debit_points"]))
                        for attempt in selection.attempts
                        if "net_debit_points" in attempt
                    )
                    and all("net_debit_points" in attempt for attempt in selection.attempts)
                ):
                    capped_windows.add(entry.window)
                emit(
                    "ENTRY_REJECTED",
                    open_time,
                    signal_sequence=sequence,
                    reason=selection.rejection_reason,
                )
            elif selected.buy_contract.lot_size != selected.sell_contract.lot_size:
                emit(
                    "ENTRY_REJECTED",
                    open_time,
                    signal_sequence=sequence,
                    reason="OPTION_LOT_SIZE_MISMATCH",
                )
            else:
                position = _open_spread(selected, open_time, sequence, strike, assumptions)
                opened.append(
                    ExcludedSpread(
                        sequence,
                        open_time,
                        position.maximum_loss_inr
                        + assumptions.flat_round_trip_cost.quantize(FINANCIAL_DECIMAL_QUANTUM),
                    )
                )
                emit(
                    "POSITION_OPENED",
                    open_time,
                    signal_sequence=sequence,
                    pricing_reference="option_open",
                )

        candle = market.get(timestamp)
        if position is not None:
            pending_exit, missing = _monitor(
                position,
                candle,
                timestamp,
                timestamp.time() >= assumptions.square_off_time,
                vix_at_entry,
            )
            if missing:
                return exclude(timestamp, missing)
            if pending_exit is not None:
                emit(
                    "EXIT_SIGNALLED",
                    timestamp,
                    signal_sequence=position.signal_sequence,
                    reason=pending_exit.value,
                )

        for sequence, signal in accepted.get(timestamp, []):
            reason = None
            if position is not None or pending_entry is not None:
                reason = "POSITION_OR_ENTRY_ALREADY_OPEN"
            elif timestamp.time() >= assumptions.square_off_time:
                reason = "ENTRY_AFTER_HARD_EXIT"
            elif len(opened) >= assumptions.max_trades_per_day:
                reason = "MAX_TRADES_PER_DAY_REACHED"
            elif daily_net <= -assumptions.maximum_daily_loss:
                reason = "MAXIMUM_DAILY_LOSS_REACHED"
            elif signal.window in capped_windows:
                reason = "ENTRY_WINDOW_DEBIT_CAP_REACHED"
            elif candle is None:
                reason = "SYNCHRONIZED_SPOT_PRICE_MISSING"
            elif vix_spike_exit and candle.india_vix is None:
                reason = "ENTRY_VIX_MISSING"
            if reason is not None:
                emit("ENTRY_REJECTED", timestamp, signal_sequence=sequence, reason=reason)
                continue
            assert candle is not None
            strike = _round_to_increment(Decimal(str(candle.close)), assumptions.atm_increment)
            entry_vix = (
                Decimal(str(candle.india_vix))
                if vix_spike_exit and candle.india_vix is not None
                else None
            )
            pending_entry = PendingEntry(sequence, strike, signal.window, entry_vix)
            emit("ENTRY_SIGNALLED", timestamp, signal_sequence=sequence)

    return ClockSessionResult(session_date, tuple(trades))
