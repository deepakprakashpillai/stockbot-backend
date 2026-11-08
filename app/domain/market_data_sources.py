"""Provider-neutral contracts for future executable-market evidence.

These records are diagnostic inputs until a reviewed trading-rule change uses
them. No adapter in this module can place broker orders.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class ExecutableOptionQuote:
    contract_symbol: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    bid_quantity: int | None
    ask_quantity: int | None
    provider: str


@dataclass(frozen=True)
class OptionLiquiditySnapshot:
    contract_symbol: str
    timestamp: datetime
    volume: int
    open_interest: int
    provider: str


@dataclass(frozen=True)
class MarketEvent:
    event_date: date
    event_type: str
    title: str
    source: str
    source_reference: str


class HistoricalQuoteProvider(Protocol):
    provider_name: str

    def quotes(
        self, contract_symbols: set[str], start: datetime, end: datetime
    ) -> Iterable[ExecutableOptionQuote]: ...


class LiquidityProvider(Protocol):
    provider_name: str

    def snapshots(
        self, contract_symbols: set[str], start: datetime, end: datetime
    ) -> Iterable[OptionLiquiditySnapshot]: ...


class EventCalendarProvider(Protocol):
    provider_name: str

    def events(self, start: date, end: date) -> Iterable[MarketEvent]: ...


def executable_quote_coverage(
    required: Iterable[tuple[str, datetime]], quotes: Iterable[ExecutableOptionQuote]
) -> dict[str, object]:
    required_keys = set(required)
    quotes = list(quotes)
    observed = {(item.contract_symbol, item.timestamp) for item in quotes}
    missing = sorted(required_keys - observed, key=lambda item: (item[1], item[0]))
    invalid = sorted(
        {
            (item.contract_symbol, item.timestamp)
            for item in quotes
            if item.bid < 0 or item.ask <= 0 or item.bid > item.ask
        },
        key=lambda item: (item[1], item[0]),
    )
    return {
        "required_count": len(required_keys),
        "observed_count": len(required_keys & observed),
        "missing_count": len(missing),
        "invalid_count": len(invalid),
        "complete": not missing and not invalid,
        "missing_examples": [
            {"contract_symbol": symbol, "timestamp": timestamp.isoformat()}
            for symbol, timestamp in missing[:20]
        ],
        "invalid_examples": [
            {"contract_symbol": symbol, "timestamp": timestamp.isoformat()}
            for symbol, timestamp in invalid[:20]
        ],
    }
