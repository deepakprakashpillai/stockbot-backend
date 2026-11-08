"""Canonical completed-candle timestamp handling.

Provider timestamps are explicit input metadata. Domain code consumes only
interval-end timestamps so a 09:15 five-minute bar becomes usable at 09:20.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

CandleTimestampSemantics = Literal["interval_start", "interval_end"]
CANONICAL_TIMESTAMP_SEMANTICS: CandleTimestampSemantics = "interval_end"


def completed_candle_timestamp(
    timestamp: datetime,
    *,
    source_semantics: CandleTimestampSemantics,
    interval_minutes: int,
) -> datetime:
    if interval_minutes <= 0:
        raise ValueError("Candle interval must be positive.")
    if source_semantics == "interval_start":
        return timestamp + timedelta(minutes=interval_minutes)
    if source_semantics == "interval_end":
        return timestamp
    raise ValueError(f"Unsupported candle timestamp semantics: {source_semantics!r}.")
