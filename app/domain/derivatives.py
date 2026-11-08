"""Market contract and option-candle values independent of file storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class OptionContract:
    trading_symbol: str
    expiry: date
    strike: int
    option_type: str
    lot_size: int
    tick_size: Decimal
    weekly: bool


@dataclass(frozen=True)
class OptionCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int
