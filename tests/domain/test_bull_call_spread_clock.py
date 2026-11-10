from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.domain.bull_call_spread_clock import (
    ClockSessionResult,
    compare_clock_sessions,
    replay_spread_session,
    summarize_clock_sessions,
)
from app.domain.derivatives import OptionCandle, OptionContract
from app.domain.option_spread_models import OptionSpreadAssumptions
from app.domain.signal_models import MarketCandle, SignalDecision
from app.domain.trading_calendar import NSE_FO_CALENDAR
from app.infrastructure.market_data.csv_reader import CsvMarketDataProvider
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider
from tests.market_fixture import market_bundle

DAY = date(2026, 4, 6)
TIMES = NSE_FO_CALENDAR.completed_candle_times(DAY)
ASSUMPTIONS = OptionSpreadAssumptions(slippage_percent=Decimal("0"))


class Session:
    def __init__(self) -> None:
        self.rows = {
            strike: {t: OptionCandle(t, price, price, price, price, 0, 0) for t in TIMES}
            for strike, price in ((24000, 100), (24200, 50))
        }

    def get_option_contract(self, strike: int, option_type: str) -> OptionContract | None:
        if strike not in self.rows:
            return None
        return OptionContract(
            str(strike), date(2026, 4, 7), strike, option_type, 65, Decimal("0.05"), True
        )

    def get_option_candles(self, strike: int, option_type: str) -> dict[datetime, OptionCandle]:
        return self.rows.get(strike, {})


def candles(*, missing_vix: bool = False) -> list[MarketCandle]:
    return [
        MarketCandle(
            t, 24020, 24000, 23995, None if missing_vix and i > 2 else 16, futures_close=24005
        )
        for i, t in enumerate(TIMES)
    ]


def signals(*indices: int) -> list[tuple[int, SignalDecision]]:
    return [
        (sequence, SignalDecision(TIMES[i], DAY, "ACCEPTED", None, "window_1", 5, {}))
        for sequence, i in enumerate(indices, 1)
    ]


def test_later_loss_cannot_affect_signal_until_exit_fill() -> None:
    session = Session()
    session.rows[24000][TIMES[3]] = replace(session.rows[24000][TIMES[3]], open=105, close=55)
    session.rows[24000][TIMES[4]] = replace(session.rows[24000][TIMES[4]], open=60)
    events: list[dict[str, object]] = []
    result = replay_spread_session(
        DAY,
        candles(),
        signals(2, 3, 4),
        session,
        replace(ASSUMPTIONS, max_trades_per_day=2, maximum_daily_loss=Decimal("1000")),
        observer=events.append,
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_timestamp == TIMES[2]
    assert trade.entry_net_debit_points == Decimal("55.00")
    assert trade.exit_timestamp == TIMES[3]
    assert trade.exit_net_credit_points == Decimal("10.00")
    assert trade.net_pnl_inr == Decimal("-3325.00")
    assert [event["event_type"] for event in events] == [
        "ENTRY_SIGNALLED",
        "POSITION_OPENED",
        "EXIT_SIGNALLED",
        "ENTRY_REJECTED",
        "POSITION_CLOSED",
        "ENTRY_REJECTED",
    ]
    assert events[3]["payload"] == {
        "signal_sequence": 2,
        "reason": "POSITION_OR_ENTRY_ALREADY_OPEN",
    }
    assert events[-1]["payload"] == {"signal_sequence": 3, "reason": "MAXIMUM_DAILY_LOSS_REACHED"}


def test_baseline_ignores_open_position_vix_but_variant_excludes() -> None:
    session = Session()
    baseline = replay_spread_session(
        DAY, candles(missing_vix=True), signals(2), session, ASSUMPTIONS
    )
    assert len(baseline.trades) == 1
    variant = replay_spread_session(
        DAY, candles(missing_vix=True), signals(2), session, ASSUMPTIONS, vix_spike_exit=True
    )
    assert variant.exclusion is not None
    assert variant.exclusion.missing == ("india_vix",)
    evidence = summarize_clock_sessions([variant])
    assert evidence.opened_trades == evidence.excluded_trades == 1
    assert not evidence.valid_exclusion_rate
    assert evidence.net_result_inr == 0
    assert evidence.pessimistic_net_result_inr == Decimal("-3650.00")
    comparison = compare_clock_sessions(
        {"baseline": [baseline], "spike": [variant]}, eligible_sessions={DAY}
    )
    assert not comparison.sessions
    assert not comparison.meets_minimum_coverage
    assert comparison.net_result_by_variant == {"baseline": 0, "spike": 0}


def test_exclusion_rate_flags_strictly_above_two_percent() -> None:
    session = Session()
    baseline = replay_spread_session(DAY, candles(), signals(2), session, ASSUMPTIONS)
    excluded = replay_spread_session(
        DAY, candles(missing_vix=True), signals(2), session, ASSUMPTIONS, vix_spike_exit=True
    )
    completed = [
        ClockSessionResult(DAY + timedelta(days=index), baseline.trades) for index in range(1, 50)
    ]
    assert summarize_clock_sessions([excluded, *completed]).valid_exclusion_rate
    assert not summarize_clock_sessions([excluded, *completed[:-1]]).valid_exclusion_rate


def test_missing_checkpoint_and_missing_open_after_hard_exit_exclude_session() -> None:
    session = Session()
    without_spot = [c for c in candles() if c.timestamp != TIMES[4]]
    gap = replay_spread_session(DAY, without_spot, signals(2), session, ASSUMPTIONS)
    assert gap.exclusion is not None and gap.exclusion.missing == ("spot_checkpoint",)
    next_row = next(t for t in TIMES if (t.hour, t.minute) == (15, 5))
    del session.rows[24200][next_row]
    gap = replay_spread_session(DAY, candles(), signals(2), session, ASSUMPTIONS)
    assert gap.exclusion is not None and gap.exclusion.missing == ("short_option_open",)
    assert (gap.exclusion.timestamp.hour, gap.exclusion.timestamp.minute) == (15, 0)
    assert not gap.trades


def test_replay_consumes_canonical_parquet_candles(tmp_path: Path) -> None:
    bundle = market_bundle(tmp_path)
    day = date(2025, 7, 21)
    provider = ParquetDerivativesProvider(bundle)
    market = provider.enrich_spot_candles(
        CsvMarketDataProvider(bundle / "spot/nifty_spot_5min.csv").stream(day, day)
    )
    timestamp = market[2].timestamp
    signal = SignalDecision(timestamp, day, "ACCEPTED", None, "window_1", 5, {})
    result = replay_spread_session(
        day, market, [(1, signal)], provider.load_session(day, market), ASSUMPTIONS
    )
    assert result.exclusion is None
    (trade,) = result.trades
    assert trade.entry_timestamp == timestamp
    assert trade.exit_timestamp.hour == 15 and trade.exit_timestamp.minute == 0
    assert trade.net_pnl_inr == Decimal("-400.00")
