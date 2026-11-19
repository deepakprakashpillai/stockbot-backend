"""Immutable clock inputs, evidence and outputs; no storage or worker dependency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Protocol

from app.domain.model2.approved_specification import approved_specification_digest
from app.domain.model2.assessment import ReliabilityAssessment
from app.domain.model2.calendar import Model2Calendar, WeeklyCycle
from app.domain.model2.market import (
    EXCHANGE_ZONE,
    CompletedObservation,
    HaltObservation,
    Model2PriceFeed,
    OpenObservation,
    SettlementObservation,
    exchange_time,
)
from app.domain.model2.models import CloseValuation, EntryEstimate, OpeningQuotes
from app.domain.model2.parameters import Model2Parameters
from app.domain.model2.risk import RiskEntry, RiskState, capital_limits

ENGINE_VERSION = "1.0.0-model2-overnight-clock"
ALGORITHM_KEY = "model2_overnight_iron_condor"
ALGORITHM_CODE_VERSION = "1.0.0-model2-iron-condor"
ScenarioPolicy = Literal["MAX_LOSS_RISK", "ZERO_RISK", "HALT_AT_FIRST_EXCLUSION"]
POLICIES: tuple[ScenarioPolicy, ...] = ("MAX_LOSS_RISK", "ZERO_RISK", "HALT_AT_FIRST_EXCLUSION")


class ExecutionFeed(Model2PriceFeed, Protocol):
    def halts(self, *, as_of: datetime) -> tuple[HaltObservation, ...]: ...


@dataclass(frozen=True)
class ReplayRequest:
    dataset_identity: str
    start: date
    end: date
    starting_capital: Decimal
    parameters: Model2Parameters
    # Explicit input-data checkpoints, not a configurable trading threshold.
    # Never invent an official publication time, or query settlement at infinity.
    settlement_checks: tuple[tuple[date, datetime], ...]

    def __post_init__(self) -> None:
        capital_limits(self.starting_capital)
        if not self.dataset_identity.strip() or self.end < self.start:
            raise ValueError("An immutable dataset identity and ordered range are required.")
        normalized = tuple((day, exchange_time(at)) for day, at in self.settlement_checks)
        object.__setattr__(self, "settlement_checks", normalized)
        if len({day for day, _ in normalized}) != len(normalized):
            raise ValueError("Settlement checkpoints must be unique per expiry.")
        for day, at in normalized:
            if at < market_at(day, 15, 30):
                raise ValueError("Settlement checkpoints cannot precede the expiry market close.")

    def settlement_check(self, expiry: date) -> datetime:
        for day, at in self.settlement_checks:
            if day == expiry:
                return at
        raise ValueError("Each ready cycle needs an explicit settlement-data checkpoint.")

    def validate_calendar(self, calendar: Model2Calendar) -> tuple[WeeklyCycle, ...]:
        cycles = calendar.cycles(self.start, self.end)
        for index, cycle in enumerate(cycles):
            if cycle.classification == "READY":
                checkpoint = self.settlement_check(cycle.expiry)
                if index + 1 < len(cycles) and checkpoint > market_at(
                    cycles[index + 1].entry_session, 15, 20
                ):
                    raise ValueError(
                        "Settlement knowledge must be resolved before the next cycle; later evidence needs a new input snapshot/replay."
                    )
        return cycles


@dataclass(frozen=True)
class ExitTrigger:
    reason: str
    at: datetime
    original_reason: str
    original_at: datetime
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CycleResult:
    cycle: WeeklyCycle
    state: Literal["SKIPPED", "CLOSED", "SETTLED", "EXCLUDED"]
    reason: str
    finalized_at: datetime
    entry: EntryEstimate | None = None
    trigger: ExitTrigger | None = None
    valuation: CloseValuation | None = None
    risk_entry: RiskEntry | None = None
    flags: tuple[str, ...] = ()
    exit_attempts: int = 0
    settlement: SettlementObservation | None = None
    # Fill / intrinsic-expiry time; finalized_at is when the outcome was known.
    exit_at: datetime | None = None


@dataclass(frozen=True)
class RunSummary:
    scheduled_cycles: int
    visited_cycles: int
    skipped_cycles: int
    opened_cycles: int
    priced_cycles: int
    settled_cycles: int
    excluded_cycles: int
    priced_net_pnl: Decimal
    assumed_risk_net: Decimal
    reliability: ReliabilityAssessment
    scope: Literal["FULL_REQUESTED_RANGE", "HALTED_AT_EXCLUSION", "CANCELLED"]
    effective_end: datetime | None
    final_risk_state: RiskState

    @property
    def full_period_data_gate_passed(self) -> bool:
        return self.scope == "FULL_REQUESTED_RANGE" and self.reliability.data_gate_passed


@dataclass(frozen=True)
class ClockEvent:
    sequence: int
    at: datetime
    cycle: WeeklyCycle | None
    kind: str
    state: str
    reason: str | None = None
    entry: EntryEstimate | None = None
    trigger: ExitTrigger | None = None
    quotes: OpeningQuotes | None = None
    valuation: CloseValuation | None = None
    observations: tuple[CompletedObservation | OpenObservation, ...] = ()
    halt_observations: tuple[HaltObservation, ...] = ()
    settlement_observation: SettlementObservation | None = None
    missing: tuple[str, ...] = ()
    consecutive_missing: int = 0
    attempt: int = 0
    result: CycleResult | None = None
    risk_state: RiskState | None = None
    summary: RunSummary | None = None


@dataclass(frozen=True)
class ScenarioResult:
    request: ReplayRequest
    policy: ScenarioPolicy
    calendar_version: str
    calendar_sha256: str
    cycles: tuple[CycleResult, ...]
    summary: RunSummary
    events: tuple[ClockEvent, ...] = ()
    engine_version: str = ENGINE_VERSION
    specification_sha256: str = approved_specification_digest()
    algorithm_key: str = ALGORITHM_KEY
    algorithm_code_version: str = ALGORITHM_CODE_VERSION


def market_at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), EXCHANGE_ZONE)
