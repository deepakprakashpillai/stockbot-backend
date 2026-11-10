"""Proposed RSI vectors are separate from the saved historical algorithm."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.proposed_indicators import GapAwareWilderRsi, ProposedWilderRsi
from app.domain.trading_calendar import NSE_FO_CALENDAR


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([Decimal("100")] * 28, Decimal("50")),
        ([Decimal(100 + index) for index in range(28)], Decimal("100")),
        ([Decimal(100 - index) for index in range(28)], Decimal("0")),
    ],
)
def test_warmup_and_flat_rising_falling_values(closes: list[Decimal], expected: Decimal) -> None:
    indicator = ProposedWilderRsi()
    values = [indicator.update_contiguous(close) for close in closes]
    assert values[:27] == [None] * 27
    assert values[27] == expected


def test_indicator_state_continues_across_call_batches() -> None:
    closes = [Decimal(100 + (index % 5) - (index % 3)) for index in range(40)]
    full = ProposedWilderRsi()
    one_pass = [full.update_contiguous(close) for close in closes]
    batched = ProposedWilderRsi()
    first = [batched.update_contiguous(close) for close in closes[:20]]
    second = [batched.update_contiguous(close) for close in closes[20:]]
    assert first + second == one_pass


def test_nonfinite_close_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        ProposedWilderRsi().update_contiguous(Decimal("NaN"))


def test_gap_restarts_after_missing_bar_and_missing_session_but_not_holiday() -> None:
    rsi = GapAwareWilderRsi()
    thursday = NSE_FO_CALENDAR.completed_candle_times(date(2026, 4, 2))
    monday = NSE_FO_CALENDAR.completed_candle_times(date(2026, 4, 6))
    wednesday = NSE_FO_CALENDAR.completed_candle_times(date(2026, 4, 8))
    for timestamp in thursday:
        rsi.update(timestamp, Decimal("100"))
    assert rsi.update(monday[0], Decimal("100")) == 50  # Friday was an exchange holiday.
    after_gap = [rsi.update(t, Decimal(100 + i)) for i, t in enumerate(monday[2:])]
    assert after_gap[:27] == [None] * 27
    assert after_gap[27] == 100
    assert rsi.update(wednesday[0], Decimal("100")) is None  # Tuesday was a trading day.
    assert rsi.restarts == [monday[2], wednesday[0]]
    assert rsi.affected_sessions == {monday[0].date(), date(2026, 4, 7), wednesday[0].date()}
