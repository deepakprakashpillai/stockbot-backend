"""Deterministic timestamp, strike and adverse-fill calculations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    OptionSpreadAssumptions,
    OptionSpreadExecutionRejection,
)


def _normalize_ts(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return timestamp


def _round_to_increment(value: Decimal, increment: int) -> int:
    return int(
        (value / Decimal(increment)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        * Decimal(increment)
    )


def _round_fill(value: Decimal, tick: Decimal, *, buy: bool) -> Decimal:
    if tick <= 0:
        tick = Decimal("0.05")
    rounding = ROUND_CEILING if buy else ROUND_FLOOR
    return ((value / tick).quantize(Decimal("1"), rounding=rounding) * tick).quantize(
        FINANCIAL_DECIMAL_QUANTUM
    )


def _fill(
    reference: Decimal, assumptions: OptionSpreadAssumptions, *, buy: bool, tick: Decimal
) -> Decimal:
    rate = assumptions.slippage_percent / Decimal("100")
    adjusted = reference * (Decimal("1") + rate if buy else Decimal("1") - rate)
    return _round_fill(adjusted, tick, buy=buy)


def _rejection(
    signal_sequence: int,
    timestamp: datetime,
    reason: str,
    **details: object,
) -> OptionSpreadExecutionRejection:
    return OptionSpreadExecutionRejection(signal_sequence, timestamp, reason, details)
