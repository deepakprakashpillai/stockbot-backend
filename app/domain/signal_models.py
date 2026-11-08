"""Completed market observations and strategy decisions shared by pure evaluators."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

EXCHANGE_OFFSET = timedelta(hours=5, minutes=30)


@dataclass(frozen=True)
class MarketCandle:
    timestamp: datetime
    high: float
    close: float
    futures_vwap: float | None
    india_vix: float | None
    previous_india_vix: float | None = None
    futures_close: float | None = None
    futures_contract_id: str | None = None
    futures_open_interest: int | None = None
    futures_observed_at: datetime | None = None
    india_vix_observed_at: datetime | None = None


class MarketDataProvider(Protocol):
    def stream(self, start_date: date, end_date: date) -> Iterator[MarketCandle]: ...


@dataclass(frozen=True)
class SignalDecision:
    timestamp: datetime
    session_date: date
    decision: str
    rejection_reason: str | None
    window: str | None
    score: int | None
    inputs: dict[str, Any]
