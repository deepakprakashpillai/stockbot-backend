"""Closed-trade equity projection for historical option-spread results."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    OptionSpreadEquityPointResult,
    OptionSpreadTradeResult,
)


def build_equity_points(
    trades: list[OptionSpreadTradeResult],
    *,
    starting_capital: Decimal,
    first_market_timestamp: datetime | None,
) -> tuple[OptionSpreadEquityPointResult, ...]:
    equity = starting_capital.quantize(FINANCIAL_DECIMAL_QUANTUM)
    peak = equity
    points: list[OptionSpreadEquityPointResult] = []
    if first_market_timestamp is not None:
        points.append(
            OptionSpreadEquityPointResult(
                timestamp=first_market_timestamp,
                equity_inr=equity,
                drawdown_inr=Decimal("0.00"),
                drawdown_pct=Decimal("0.00"),
            )
        )
    for trade in trades:
        equity = (equity + trade.net_pnl_inr).quantize(FINANCIAL_DECIMAL_QUANTUM)
        peak = max(peak, equity)
        drawdown = (equity - peak).quantize(FINANCIAL_DECIMAL_QUANTUM)
        drawdown_pct = (
            (drawdown / peak * Decimal("100")).quantize(Decimal("0.01"))
            if peak
            else Decimal("0.00")
        )
        points.append(
            OptionSpreadEquityPointResult(
                timestamp=trade.exit_timestamp,
                equity_inr=equity,
                drawdown_inr=drawdown,
                drawdown_pct=drawdown_pct,
            )
        )
    return tuple(points)
