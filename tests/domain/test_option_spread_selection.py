"""Keep legacy entry selection explicit while partner changes await approval."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.domain.option_spread_models import OptionSpreadAssumptions
from app.domain.option_spread_selection import select_entry_spread

ENTRY = datetime(2025, 7, 21, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


class Session:
    def __init__(self, prices: dict[int, str]) -> None:
        self.prices = prices

    def get_option_contract(self, strike: int, _option_type: str) -> SimpleNamespace | None:
        return SimpleNamespace(tick_size=Decimal("0.05")) if strike in self.prices else None

    def get_option_candles(self, strike: int, _option_type: str) -> dict[datetime, SimpleNamespace]:
        return (
            {ENTRY: SimpleNamespace(close=Decimal(self.prices[strike]))}
            if strike in self.prices
            else {}
        )


def test_legacy_primary_cap_failure_selects_eligible_fallback() -> None:
    assumptions = OptionSpreadAssumptions(slippage_percent=Decimal("0"))
    result = select_entry_spread(
        Session({25000: "100", 25200: "20", 25250: "30"}),
        {},
        ENTRY,
        25000,
        assumptions,
    )
    assert result.selected is not None
    assert result.selected.width == 250
    assert result.selected.debit == Decimal("70.00")
    assert len(result.attempts) == 2
    assert result.attempts[0]["maximum_debit_points"] == "70.00"


def test_legacy_missing_primary_price_stops_before_fallback() -> None:
    assumptions = OptionSpreadAssumptions(slippage_percent=Decimal("0"))
    result = select_entry_spread(
        Session({25000: "100", 25250: "30"}), {}, ENTRY, 25000, assumptions
    )
    assert result.selected is None
    assert result.rejection_reason == "SYNCHRONIZED_OPTION_PRICE_MISSING"
    assert len(result.attempts) == 1
