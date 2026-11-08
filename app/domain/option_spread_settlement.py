"""Pure, retained-policy fill and cost arithmetic for a closed spread."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    OptionSpreadAssumptions,
    OptionSpreadLegResult,
    OptionSpreadTradeResult,
    StatutoryCharges,
)
from app.domain.option_spread_monitor import MonitoredExit
from app.domain.option_spread_pricing import _fill
from app.domain.option_spread_selection import SelectedSpread


@dataclass(frozen=True)
class OpenedSpread:
    selected: SelectedSpread
    entry_timestamp: datetime
    signal_sequence: int
    atm_strike: int
    lot_size: int
    quantity: int
    maximum_loss_inr: Decimal
    maximum_profit_inr: Decimal
    stop_level: Decimal
    target_level: Decimal
    trend_level: Decimal


@dataclass(frozen=True)
class SettledSpread:
    trade: OptionSpreadTradeResult
    close_payload: dict[str, object]


def settle_spread(
    opened: OpenedSpread,
    monitored: MonitoredExit,
    assumptions: OptionSpreadAssumptions,
    session_data: Any,
) -> SettledSpread:
    """Apply the existing exit fill and flat-cost convention exactly once."""
    selected = opened.selected
    buy_contract = selected.buy_contract
    sell_contract = selected.sell_contract
    exit_buy_reference = Decimal(str(monitored.buy_candle.close))
    exit_sell_reference = Decimal(str(monitored.sell_candle.close))
    exit_long_sell = _fill(
        exit_buy_reference,
        assumptions,
        buy=False,
        tick=getattr(buy_contract, "tick_size", Decimal("0.05")),
    )
    exit_short_buy = _fill(
        exit_sell_reference,
        assumptions,
        buy=True,
        tick=getattr(sell_contract, "tick_size", Decimal("0.05")),
    )
    exit_credit = (exit_long_sell - exit_short_buy).quantize(FINANCIAL_DECIMAL_QUANTUM)
    gross_pnl = ((exit_credit - selected.debit) * opened.quantity).quantize(
        FINANCIAL_DECIMAL_QUANTUM
    )
    flat_cost = assumptions.flat_round_trip_cost.quantize(FINANCIAL_DECIMAL_QUANTUM)
    costs = StatutoryCharges(brokerage=flat_cost, total_charges=flat_cost)
    net_pnl = (gross_pnl - flat_cost).quantize(FINANCIAL_DECIMAL_QUANTUM)
    return_on_risk = (
        (net_pnl / opened.maximum_loss_inr * Decimal("100")).quantize(Decimal("0.01"))
        if opened.maximum_loss_inr
        else Decimal("0.00")
    )
    expiry = str(
        getattr(buy_contract, "expiry", None) or getattr(session_data, "active_expiry", "")
    )
    sell_strike = opened.atm_strike + selected.width
    buy_symbol = str(getattr(buy_contract, "trading_symbol", f"NIFTY {opened.atm_strike} CE"))
    sell_symbol = str(getattr(sell_contract, "trading_symbol", f"NIFTY {sell_strike} CE"))
    buy_leg = OptionSpreadLegResult(
        contract_symbol=buy_symbol,
        expiry=expiry,
        strike_price=Decimal(opened.atm_strike),
        option_type="CE",
        action="BUY",
        entry_reference_close=Decimal(str(selected.buy_candles[opened.entry_timestamp].close)),
        exit_reference_close=exit_buy_reference,
        entry_price=selected.buy_fill,
        exit_price=exit_long_sell,
        lot_size=opened.lot_size,
        quantity=opened.quantity,
        gross_pnl_inr=((exit_long_sell - selected.buy_fill) * opened.quantity).quantize(
            FINANCIAL_DECIMAL_QUANTUM
        ),
    )
    sell_leg = OptionSpreadLegResult(
        contract_symbol=sell_symbol,
        expiry=expiry,
        strike_price=Decimal(sell_strike),
        option_type="CE",
        action="SELL",
        entry_reference_close=Decimal(str(selected.sell_candles[opened.entry_timestamp].close)),
        exit_reference_close=exit_sell_reference,
        entry_price=selected.sell_fill,
        exit_price=exit_short_buy,
        lot_size=opened.lot_size,
        quantity=opened.quantity,
        gross_pnl_inr=((selected.sell_fill - exit_short_buy) * opened.quantity).quantize(
            FINANCIAL_DECIMAL_QUANTUM
        ),
    )
    trade = OptionSpreadTradeResult(
        entry_signal_sequence=opened.signal_sequence,
        entry_timestamp=opened.entry_timestamp,
        exit_timestamp=monitored.timestamp,
        expiry=expiry,
        spread_width_points=Decimal(selected.width),
        buy_leg=buy_leg,
        sell_leg=sell_leg,
        lot_size=opened.lot_size,
        lots_traded=assumptions.lots,
        total_quantity=opened.quantity,
        entry_net_debit_points=selected.debit,
        exit_net_credit_points=exit_credit,
        entry_net_debit_inr=opened.maximum_loss_inr,
        exit_net_credit_inr=(exit_credit * opened.quantity).quantize(FINANCIAL_DECIMAL_QUANTUM),
        maximum_loss_inr=opened.maximum_loss_inr,
        maximum_profit_inr=opened.maximum_profit_inr,
        stop_level_points=opened.stop_level,
        target_level_points=opened.target_level,
        trend_reversal_level_points=opened.trend_level,
        gross_pnl_inr=gross_pnl,
        statutory_charges=costs,
        net_pnl_inr=net_pnl,
        return_on_risk_pct=return_on_risk,
        exit_reason=monitored.reason.value,
    )
    return SettledSpread(
        trade=trade,
        close_payload={
            "signal_sequence": opened.signal_sequence,
            "position_state": "CLOSED",
            "exit_reason": monitored.reason.value,
            "exit_buy_reference": str(exit_buy_reference),
            "exit_sell_reference": str(exit_sell_reference),
            "exit_buy_fill": str(exit_long_sell),
            "exit_sell_fill": str(exit_short_buy),
            "exit_credit_points": str(exit_credit),
            "gross_pnl_inr": str(gross_pnl),
            "costs_inr": str(flat_cost),
            "net_pnl_inr": str(net_pnl),
        },
    )
