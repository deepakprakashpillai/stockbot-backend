"""Reader for the immutable spot-candle CSV profile."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

from app.domain.backtests import MarketCandle


class CsvMarketDataProvider:
    """The candle-only profile deliberately cannot masquerade as signal-complete data."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def stream(self, start_date: date, end_date: date) -> Iterator[MarketCandle]:
        with self.path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                timestamp = datetime.fromisoformat(row["timestamp"])
                if start_date <= timestamp.date() <= end_date:
                    futures_vwap = row.get("futures_vwap")
                    india_vix = row.get("india_vix")
                    previous_india_vix = row.get("previous_india_vix")
                    yield MarketCandle(
                        timestamp,
                        float(row["high"]),
                        float(row["close"]),
                        float(futures_vwap) if futures_vwap else None,
                        float(india_vix) if india_vix else None,
                        float(previous_india_vix) if previous_india_vix else None,
                    )
