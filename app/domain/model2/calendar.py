"""Explicit immutable sessions/expiry schedules; no weekday or observed-row guesses."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from app.domain.model2.market import BAR_INTERVAL, EXCHANGE_ZONE, Provenance, exchange_time


@dataclass(frozen=True)
class Model2Session:
    session_date: date
    kind: Literal["NORMAL", "SPECIAL"]
    opens_at: time | None = None
    closes_at: time | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"NORMAL", "SPECIAL"}:
            raise ValueError("Unknown session kind.")
        if self.kind == "NORMAL" and (
            self.opens_at != time(9, 15) or self.closes_at != time(15, 30)
        ):
            raise ValueError("Short/changed sessions must be explicitly marked SPECIAL.")
        if (self.opens_at is None) != (self.closes_at is None):
            raise ValueError("Session boundaries must be present together.")
        if self.opens_at is not None and self.closes_at is not None:
            if (
                self.opens_at.tzinfo is not None
                or self.closes_at.tzinfo is not None
                or self.closes_at <= self.opens_at
            ):
                raise ValueError("Session times must be ordered exchange-local wall times.")

    def opening_times(self) -> tuple[datetime, ...]:
        if self.kind != "NORMAL" or self.opens_at is None or self.closes_at is None:
            raise ValueError("Model 2 does not execute special sessions.")
        start = datetime.combine(self.session_date, self.opens_at, EXCHANGE_ZONE)
        end = datetime.combine(self.session_date, self.closes_at, EXCHANGE_ZONE)
        return tuple(
            start + index * BAR_INTERVAL for index in range(int((end - start) / BAR_INTERVAL))
        )


@dataclass(frozen=True)
class WeeklyCycle:
    expiry: date
    entry_session: date
    classification: Literal["READY", "SPECIAL_SESSION", "OUTSIDE_COMPLETE_CYCLE_RANGE"]


@dataclass(frozen=True)
class Model2Calendar:
    version: str
    coverage_start: date
    coverage_end: date
    sessions: tuple[Model2Session, ...]
    weekly_expiries: tuple[date, ...]
    provenance: Provenance
    expiry_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "weekly_expiries", tuple(self.weekly_expiries))
        if (
            not self.version.strip()
            or not self.expiry_reference.strip()
            or self.coverage_end < self.coverage_start
        ):
            raise ValueError("An explicit calendar version, expiry source and range are required.")
        days = tuple(item.session_date for item in self.sessions)
        if not days or days != tuple(sorted(set(days))):
            raise ValueError("Calendar sessions must be unique and strictly ordered.")
        if days[0] < self.coverage_start or days[-1] > self.coverage_end:
            raise ValueError("Session outside declared calendar coverage.")
        if self.weekly_expiries != tuple(sorted(set(self.weekly_expiries))):
            raise ValueError("Weekly expiry dates must be unique and strictly ordered.")
        for expiry in self.weekly_expiries:
            if expiry not in days:
                raise ValueError("An expiry must be a declared trading session.")
            self.previous_session(expiry)

    def require_coverage(self, value: date) -> None:
        if not self.coverage_start <= value <= self.coverage_end:
            raise ValueError(f"Calendar {self.version} does not cover {value}.")

    def session(self, value: date) -> Model2Session | None:
        self.require_coverage(value)
        return next((item for item in self.sessions if item.session_date == value), None)

    def previous_session(self, value: date) -> Model2Session:
        self.require_coverage(value)
        days = tuple(item.session_date for item in self.sessions)
        index = bisect_left(days, value) - 1
        if index < 0:
            raise ValueError("Calendar lacks the required previous trading session.")
        return self.sessions[index]

    def previous_completed_at(self, opening: datetime) -> datetime:
        opening = exchange_time(opening)
        session = self.session(opening.date())
        if session is None or opening not in session.opening_times():
            raise ValueError("Opening time is outside the normal session schedule.")
        if opening.time() != session.opens_at:
            return opening
        previous = self.previous_session(opening.date())
        if previous.kind != "NORMAL" or previous.closes_at is None:
            raise ValueError("Previous normal-session closing time is unavailable.")
        return datetime.combine(previous.session_date, previous.closes_at, EXCHANGE_ZONE)

    def cycles(self, start: date, end: date) -> tuple[WeeklyCycle, ...]:
        self.require_coverage(start)
        self.require_coverage(end)
        if end < start:
            raise ValueError("Cycle range is reversed.")
        result = []
        for expiry in self.weekly_expiries:
            previous = self.previous_session(expiry)
            if previous.session_date > end or expiry < start:
                continue
            session = self.session(expiry)
            assert session is not None
            classification: Literal["READY", "SPECIAL_SESSION", "OUTSIDE_COMPLETE_CYCLE_RANGE"] = (
                "READY"
            )
            if not start <= previous.session_date <= expiry <= end:
                classification = "OUTSIDE_COMPLETE_CYCLE_RANGE"
            elif previous.kind == "SPECIAL" or session.kind == "SPECIAL":
                classification = "SPECIAL_SESSION"
            result.append(WeeklyCycle(expiry, previous.session_date, classification))
        return tuple(result)
