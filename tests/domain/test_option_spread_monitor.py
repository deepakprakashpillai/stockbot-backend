"""Exit checks preserve precedence and fail on missing monitoring data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.option_spread_models import (
    OpenPositionDataGapError,
    OptionExitReason,
    OptionSpreadAssumptions,
)
from app.domain.option_spread_monitor import monitor_spread_exit

IST = timezone(timedelta(hours=5, minutes=30))
ENTRY = datetime(2025, 7, 21, 10, 0, tzinfo=IST)
CHECK = ENTRY + timedelta(minutes=5)


def test_stop_precedes_trend_reversal_and_emits_evidence() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    result = monitor_spread_exit(
        session_candles=[
            SimpleNamespace(timestamp=CHECK, close=100, futures_vwap=110, india_vix=15)
        ],
        buy_candles={CHECK: SimpleNamespace(close=60)},
        sell_candles={CHECK: SimpleNamespace(close=20)},
        entry_timestamp=ENTRY,
        assumptions=OptionSpreadAssumptions(),
        stop_level=Decimal("40"),
        target_level=Decimal("80"),
        trend_level=Decimal("50"),
        signal_sequence=7,
        emit=lambda name, _timestamp, **payload: events.append((name, payload)),
        should_cancel=None,
    )
    assert result.reason == OptionExitReason.STOP_LOSS
    assert events[0][0] == "POSITION_CHECK"
    assert events[0][1]["action"] == "STOP_LOSS"
    assert events[0][1]["signal_sequence"] == 7


def test_missing_option_checkpoint_fails_closed() -> None:
    with pytest.raises(OpenPositionDataGapError):
        monitor_spread_exit(
            session_candles=[
                SimpleNamespace(timestamp=CHECK, close=100, futures_vwap=110, india_vix=15)
            ],
            buy_candles={CHECK: SimpleNamespace(close=60)},
            sell_candles={},
            entry_timestamp=ENTRY,
            assumptions=OptionSpreadAssumptions(),
            stop_level=Decimal("40"),
            target_level=Decimal("80"),
            trend_level=Decimal("50"),
            signal_sequence=7,
            emit=lambda *_args, **_payload: None,
            should_cancel=None,
        )
