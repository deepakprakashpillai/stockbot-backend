"""Wilder RSI and calendar-aware continuity for Bull Call Spread research."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.domain.trading_calendar import NSE_FO_CALENDAR, TradingSessionCalendar


class ProposedWilderRsi:
    """Wilder RSI(14), neutral flat series, valid after 28 completed closes.

    A caller must establish candle continuity.
    """

    def __init__(self) -> None:
        self.previous: Decimal | None = None
        self.closes = 0
        self.changes = 0
        self.gain = Decimal("0")
        self.loss = Decimal("0")

    def update_contiguous(self, close: Decimal) -> Decimal | None:
        if not close.is_finite() or close <= 0:
            raise ValueError("A completed close must be positive and finite.")
        self.closes += 1
        if self.previous is None:
            self.previous = close
            return None
        change = close - self.previous
        self.previous = close
        self.changes += 1
        if self.changes <= 14:
            self.gain += max(change, Decimal("0"))
            self.loss += max(-change, Decimal("0"))
            if self.changes == 14:
                self.gain /= Decimal("14")
                self.loss /= Decimal("14")
        else:
            self.gain = (self.gain * Decimal("13") + max(change, Decimal("0"))) / Decimal("14")
            self.loss = (self.loss * Decimal("13") + max(-change, Decimal("0"))) / Decimal("14")
        if self.closes < 28:
            return None
        if self.gain == 0 and self.loss == 0:
            return Decimal("50")
        if self.loss == 0:
            return Decimal("100")
        if self.gain == 0:
            return Decimal("0")
        return Decimal("100") - Decimal("100") / (Decimal("1") + self.gain / self.loss)


class GapAwareWilderRsi:
    """Stream the complete dataset once, before slicing requested run dates."""

    def __init__(self, calendar: TradingSessionCalendar = NSE_FO_CALENDAR) -> None:
        self.calendar = calendar
        self.restarts: list[datetime] = []
        self.affected_sessions: set[date] = set()
        self._indicator = ProposedWilderRsi()
        self._previous: datetime | None = None
        self._session: date | None = None
        self._last_checkpoint: datetime | None = None
        self._checkpoints: set[datetime] = set()

    def update(self, timestamp: datetime, close: Decimal) -> Decimal | None:
        previous = self._previous
        if previous is not None and timestamp <= previous:
            raise ValueError("RSI observations must be in strictly increasing market time.")
        if not close.is_finite() or close <= 0:
            raise ValueError("A completed close must be positive and finite.")
        session = timestamp.date()
        if session != self._session:
            checkpoints = self.calendar.completed_candle_times(session)
            if not checkpoints or timestamp not in checkpoints:
                raise ValueError("RSI observation is outside the verified session grid.")
            gap = False
            if previous is not None:
                if previous != self._last_checkpoint:
                    self.affected_sessions.add(previous.date())
                    gap = True
                if self.calendar.previous_session(session) != previous.date():
                    self.affected_sessions.update(
                        self.calendar.sessions(
                            previous.date() + timedelta(days=1), session - timedelta(days=1)
                        )
                    )
                    gap = True
                gap = gap or timestamp != checkpoints[0]
            self._session = session
            self._last_checkpoint = checkpoints[-1]
            self._checkpoints = set(checkpoints)
        else:
            if timestamp not in self._checkpoints:
                raise ValueError("RSI observation is outside the verified session grid.")
            gap = previous is not None and timestamp - previous != timedelta(minutes=5)
        if gap:
            self._indicator = ProposedWilderRsi()
            self.restarts.append(timestamp)
        self._previous = timestamp
        result = self._indicator.update_contiguous(close)
        if self.restarts and result is None:
            self.affected_sessions.add(session)
        return result
