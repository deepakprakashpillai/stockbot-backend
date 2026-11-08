"""Contract and entry-price selection for the retained spread research versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.decision_checks import check
from app.domain.option_spread_models import FINANCIAL_DECIMAL_QUANTUM, OptionSpreadAssumptions
from app.domain.option_spread_pricing import _fill, _normalize_ts


@dataclass(frozen=True)
class SelectedSpread:
    buy_contract: Any
    sell_contract: Any
    buy_candles: dict[datetime, Any]
    sell_candles: dict[datetime, Any]
    width: int
    buy_fill: Decimal
    sell_fill: Decimal
    debit: Decimal


@dataclass(frozen=True)
class EntrySpreadSelection:
    selected: SelectedSpread | None
    attempted_widths: list[int]
    attempts: list[dict[str, object]]
    rejection_reason: str


def _session_option_candles(
    session_data: Any,
    cache: dict[tuple[int, str], dict[datetime, Any]],
    strike: int,
    option_type: str,
) -> dict[datetime, Any]:
    key = (strike, option_type)
    if key not in cache:
        cache[key] = {
            _normalize_ts(timestamp): candle
            for timestamp, candle in session_data.get_option_candles(strike, option_type).items()
        }
    return cache[key]


def select_entry_spread(
    session_data: Any,
    option_candles_by_strike: dict[tuple[int, str], dict[datetime, Any]],
    entry_timestamp: datetime,
    atm_strike: int,
    assumptions: OptionSpreadAssumptions,
) -> EntrySpreadSelection:
    """Apply the existing primary/fallback entry policy without changing its order."""

    attempted_width_details: list[dict[str, object]] = []
    widths = [assumptions.primary_spread_width]
    if assumptions.fallback_enabled:
        widths.append(assumptions.fallback_spread_width)
    last_reason = "OPTION_CONTRACT_MISSING"
    for width_index, width in enumerate(widths):
        buy_strike = atm_strike
        sell_strike = atm_strike + width
        buy_contract = getattr(session_data, "get_option_contract", lambda *_: None)(
            buy_strike, "CE"
        )
        sell_contract = getattr(session_data, "get_option_contract", lambda *_: None)(
            sell_strike, "CE"
        )
        buy_candles = _session_option_candles(
            session_data, option_candles_by_strike, buy_strike, "CE"
        )
        sell_candles = _session_option_candles(
            session_data, option_candles_by_strike, sell_strike, "CE"
        )
        if entry_timestamp not in buy_candles or entry_timestamp not in sell_candles:
            attempted_width_details.append(
                {
                    "width": width,
                    "buy_strike": buy_strike,
                    "sell_strike": sell_strike,
                    "buy_contract_available": buy_contract is not None,
                    "sell_contract_available": sell_contract is not None,
                    "buy_price_available": entry_timestamp in buy_candles,
                    "sell_price_available": entry_timestamp in sell_candles,
                }
            )
            last_reason = "SYNCHRONIZED_OPTION_PRICE_MISSING"
            if width_index == 0:
                break
            continue
        buy_tick = getattr(buy_contract, "tick_size", Decimal("0.05"))
        sell_tick = getattr(sell_contract, "tick_size", Decimal("0.05"))
        buy_reference = Decimal(str(buy_candles[entry_timestamp].close))
        sell_reference = Decimal(str(sell_candles[entry_timestamp].close))
        buy_fill = _fill(buy_reference, assumptions, buy=True, tick=buy_tick)
        sell_fill = _fill(sell_reference, assumptions, buy=False, tick=sell_tick)
        debit = (buy_fill - sell_fill).quantize(FINANCIAL_DECIMAL_QUANTUM)
        cap = (Decimal(width) * assumptions.maximum_debit_percent / Decimal("100")).quantize(
            FINANCIAL_DECIMAL_QUANTUM
        )
        attempted_width_details.append(
            {
                "width": width,
                "buy_strike": buy_strike,
                "sell_strike": sell_strike,
                "buy_reference_close": str(buy_reference),
                "sell_reference_close": str(sell_reference),
                "buy_fill": str(buy_fill),
                "sell_fill": str(sell_fill),
                "net_debit_points": str(debit),
                "maximum_debit_points": str(cap),
                "checks": [
                    check(
                        "positive_debit", "Net debit is positive", str(debit), ">", "0", debit > 0
                    ),
                    check(
                        "debit_cap",
                        "Net debit is at or below width × cap percentage",
                        str(debit),
                        "<=",
                        str(cap),
                        debit <= cap if debit > 0 else None,
                        evidence={
                            "formula": "buy fill - sell fill <= width * maximum debit percent / 100",
                            "buy_fill": str(buy_fill),
                            "sell_fill": str(sell_fill),
                            "width": width,
                            "maximum_debit_percent": str(assumptions.maximum_debit_percent),
                        },
                    ),
                ],
            }
        )
        if debit <= 0:
            last_reason = "NON_POSITIVE_NET_DEBIT"
            break
        if debit > cap:
            last_reason = "NET_DEBIT_CAP_EXCEEDED"
            continue
        return EntrySpreadSelection(
            selected=SelectedSpread(
                buy_contract=buy_contract,
                sell_contract=sell_contract,
                buy_candles=buy_candles,
                sell_candles=sell_candles,
                width=width,
                buy_fill=buy_fill,
                sell_fill=sell_fill,
                debit=debit,
            ),
            attempted_widths=widths,
            attempts=attempted_width_details,
            rejection_reason=last_reason,
        )
    return EntrySpreadSelection(None, widths, attempted_width_details, last_reason)
