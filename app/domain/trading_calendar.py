from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

NSE_FO_CALENDAR_VERSION = "nse-fo-2025-2026.v1"
NSE_FO_CALENDAR_SOURCES = (
    "NSE/CMTR/65587 (2025 capital-market holidays)",
    "NSE/FAOP/71777 (2026 futures-and-options holidays)",
    "NSE/CMTR/72260 (15 January 2026 special closure)",
)

_HOLIDAYS = {
    2025: {
        date(2025, 2, 26),
        date(2025, 3, 14),
        date(2025, 3, 31),
        date(2025, 4, 10),
        date(2025, 4, 14),
        date(2025, 4, 18),
        date(2025, 5, 1),
        date(2025, 8, 15),
        date(2025, 8, 27),
        date(2025, 10, 2),
        date(2025, 10, 21),
        date(2025, 10, 22),
        date(2025, 11, 5),
        date(2025, 12, 25),
    },
    2026: {
        date(2026, 1, 15),
        date(2026, 1, 26),
        date(2026, 3, 3),
        date(2026, 3, 26),
        date(2026, 3, 31),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 5, 28),
        date(2026, 6, 26),
        date(2026, 9, 14),
        date(2026, 10, 2),
        date(2026, 10, 20),
        date(2026, 11, 10),
        date(2026, 11, 24),
        date(2026, 12, 25),
    },
}

# These sessions use exchange-announced special hours. They are kept in the
# calendar so coverage can report them explicitly; the strategy's own entry
# windows decide whether any candle is eligible.
_SPECIAL_SESSIONS = {date(2025, 10, 21), date(2026, 2, 1)}


@dataclass(frozen=True)
class TradingSessionCalendar:
    version: str = NSE_FO_CALENDAR_VERSION

    def _validate_year(self, value: date) -> None:
        if value.year not in _HOLIDAYS:
            raise ValueError(f"Trading calendar {self.version} does not cover {value.year}.")

    def is_session(self, value: date) -> bool:
        self._validate_year(value)
        if value in _SPECIAL_SESSIONS:
            return True
        return value.weekday() < 5 and value not in _HOLIDAYS[value.year]

    def sessions(self, start: date, end: date) -> list[date]:
        if end < start:
            raise ValueError("Trading calendar range is invalid.")
        result: list[date] = []
        current = start
        while current <= end:
            if self.is_session(current):
                result.append(current)
            current += timedelta(days=1)
        return result

    def previous_session(self, value: date) -> date:
        current = value - timedelta(days=1)
        for _ in range(14):
            if self.is_session(current):
                return current
            current -= timedelta(days=1)
        raise ValueError(f"No previous trading session found before {value.isoformat()}.")

    def completed_candle_times(self, value: date) -> list[datetime]:
        """Required normal-session grid; never infer completeness from observed rows."""
        if not self.is_session(value):
            return []
        if value in _SPECIAL_SESSIONS:
            raise ValueError(
                f"Verified intraday schedule is unavailable for special session {value}."
            )
        first = datetime.combine(value, time(9, 20), timezone(timedelta(hours=5, minutes=30)))
        return [first + timedelta(minutes=5 * index) for index in range(75)]


NSE_FO_CALENDAR = TradingSessionCalendar()
