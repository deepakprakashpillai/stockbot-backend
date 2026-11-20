"""Fixed-capital admission and bounded immutable risk counters for one replay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from app.domain.model2.approved_specification import decimal_rule
from app.domain.model2.calendar import WeeklyCycle
from app.domain.model2.market import exchange_time
from app.domain.model2.numeric import finite_decimal


@dataclass(frozen=True)
class CapitalLimits:
    target: Decimal
    hard: Decimal
    daily: Decimal
    monthly: Decimal
    margin: Decimal


def capital_limits(starting_capital: Decimal) -> CapitalLimits:
    finite_decimal(starting_capital, "Starting capital", positive=True)
    return CapitalLimits(
        *(
            starting_capital * decimal_rule(name)
            for name in (
                "target_trade_risk_fraction",
                "hard_trade_risk_fraction",
                "daily_loss_fraction",
                "monthly_loss_fraction",
                "margin_capital_fraction",
            )
        )
    )


def round_trip_charges() -> Decimal:
    return decimal_rule("cost_per_leg_per_lot_round_trip_inr") * 4


@dataclass(frozen=True)
class RiskBudget:
    maximum_loss: Decimal | None
    assumed_margin: Decimal | None
    limits: CapitalLimits
    reason: str | None
    target_exceeded: bool


def margin_allowed(maximum_loss_including_charges: Decimal, starting_capital: Decimal) -> bool:
    finite_decimal(maximum_loss_including_charges, "Maximum loss", positive=True)
    return (
        maximum_loss_including_charges * decimal_rule("margin_multiplier")
        <= capital_limits(starting_capital).margin
    )


def risk_budget(loss_points: Decimal, quantity: int, starting_capital: Decimal) -> RiskBudget:
    finite_decimal(loss_points, "Maximum-loss points")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("Historical contract quantity must be a positive integer.")
    limits = capital_limits(starting_capital)
    if loss_points <= 0:
        return RiskBudget(None, None, limits, "NONPOSITIVE_MAX_LOSS_POINTS", False)
    maximum_loss = loss_points * quantity + round_trip_charges()
    margin = maximum_loss * decimal_rule("margin_multiplier")
    reason = (
        "RISK_CAP"
        if maximum_loss > limits.hard
        else "MARGIN_CAP"
        if margin > limits.margin
        else None
    )
    return RiskBudget(maximum_loss, margin, limits, reason, maximum_loss > limits.target)


@dataclass(frozen=True)
class RiskState:
    starting_capital: Decimal
    day: date | None = None
    month: tuple[int, int] | None = None
    day_net: Decimal = Decimal("0")
    month_net: Decimal = Decimal("0")
    losing_streak: int = 0
    pause_remaining: int = 0
    current_cycle: WeeklyCycle | None = None
    current_cycle_allowed: bool = False
    last_finalized_expiry: date | None = None
    halted: bool = False

    def __post_init__(self) -> None:
        capital_limits(self.starting_capital)
        finite_decimal(self.day_net, "Day net")
        finite_decimal(self.month_net, "Month net")
        if (
            type(self.losing_streak) is not int
            or type(self.pause_remaining) is not int
            or self.losing_streak < 0
            or not 0 <= self.pause_remaining <= 2
        ):
            raise ValueError("Invalid loss-streak/pause counters.")


@dataclass(frozen=True)
class CycleRiskDecision:
    state: RiskState
    allowed: bool
    reason: str | None


def _roll(state: RiskState, day: date) -> RiskState:
    if state.day is not None and day < state.day:
        raise ValueError("Risk events must be chronological.")
    month = day.year, day.month
    return replace(
        state,
        day=day,
        month=month,
        day_net=state.day_net if state.day == day else Decimal("0"),
        month_net=state.month_net if state.month == month else Decimal("0"),
    )


def prepare_cycle(state: RiskState, cycle: WeeklyCycle) -> CycleRiskDecision:
    """Visit each scheduled in-range cycle once, even if an entry will be skipped."""
    if state.halted:
        return CycleRiskDecision(state, False, "HALTED_AT_EXCLUSION")
    if cycle.classification == "OUTSIDE_COMPLETE_CYCLE_RANGE":
        return CycleRiskDecision(state, False, cycle.classification)
    if cycle.entry_session >= cycle.expiry:
        raise ValueError("An overnight cycle must enter before expiry day.")
    if state.current_cycle is not None and cycle.expiry <= state.current_cycle.expiry:
        raise ValueError("Scheduled cycles must be strictly ordered and unique.")
    state = replace(
        _roll(state, cycle.entry_session), current_cycle=cycle, current_cycle_allowed=False
    )
    if state.pause_remaining:
        remaining = state.pause_remaining - 1
        state = replace(
            state, pause_remaining=remaining, losing_streak=state.losing_streak if remaining else 0
        )
        return CycleRiskDecision(state, False, "LOSS_STREAK_PAUSE")
    limits = capital_limits(state.starting_capital)
    reason = (
        "SPECIAL_SESSION"
        if cycle.classification == "SPECIAL_SESSION"
        else "DAILY_LOSS_LIMIT"
        if state.day_net <= -limits.daily
        else "MONTHLY_LOSS_LIMIT"
        if state.month_net <= -limits.monthly
        else None
    )
    return CycleRiskDecision(
        replace(state, current_cycle_allowed=reason is None), reason is None, reason
    )


@dataclass(frozen=True)
class RiskEntry:
    cycle_expiry: date
    booking_date: date
    source: Literal["PRICED_RESULT", "ASSUMED_MAX_LOSS", "ASSUMED_ZERO", "HALTED_AT_EXCLUSION"]
    risk_amount: Decimal | None
    priced_performance_amount: Decimal | None


def _require_opened_cycle(state: RiskState) -> WeeklyCycle:
    # The market-clock engine additionally proves that a position actually opened.
    cycle = state.current_cycle
    if (
        state.halted
        or cycle is None
        or not state.current_cycle_allowed
        or state.last_finalized_expiry == cycle.expiry
    ):
        raise ValueError("A result needs one unfinalized, risk-eligible current cycle.")
    return cycle


def _record(
    state: RiskState,
    day: date,
    amount: Decimal,
    source: Literal["PRICED_RESULT", "ASSUMED_MAX_LOSS", "ASSUMED_ZERO"],
) -> tuple[RiskState, RiskEntry]:
    cycle = _require_opened_cycle(state)
    finite_decimal(amount, "Risk contribution")
    if day not in {cycle.entry_session, cycle.expiry}:
        raise ValueError("Outcome booking must belong to the position's two sessions.")
    state = _roll(state, day)
    streak = state.losing_streak + 1 if amount < 0 else 0 if amount > 0 else state.losing_streak
    updated = replace(
        state,
        day_net=state.day_net + amount,
        month_net=state.month_net + amount,
        losing_streak=streak,
        pause_remaining=2 if streak >= 3 else state.pause_remaining,
        last_finalized_expiry=cycle.expiry,
        current_cycle_allowed=False,
    )
    return updated, RiskEntry(
        cycle.expiry, day, source, amount, amount if source == "PRICED_RESULT" else None
    )


def record_priced_result(
    state: RiskState, closed_at: datetime, net_pnl: Decimal
) -> tuple[RiskState, RiskEntry]:
    return _record(state, exchange_time(closed_at).date(), net_pnl, "PRICED_RESULT")


def record_excluded_result(
    state: RiskState,
    maximum_loss_including_charges: Decimal,
    policy: Literal["MAX_LOSS_RISK", "ZERO_RISK", "HALT_AT_FIRST_EXCLUSION"],
) -> tuple[RiskState, RiskEntry]:
    cycle = _require_opened_cycle(state)
    finite_decimal(maximum_loss_including_charges, "Excluded-cycle maximum loss", positive=True)
    if policy == "MAX_LOSS_RISK":
        return _record(state, cycle.expiry, -maximum_loss_including_charges, "ASSUMED_MAX_LOSS")
    if policy == "ZERO_RISK":
        return _record(state, cycle.expiry, Decimal("0"), "ASSUMED_ZERO")
    if policy != "HALT_AT_FIRST_EXCLUSION":
        raise ValueError("Unknown excluded-cycle scenario policy.")
    updated = replace(
        _roll(state, cycle.expiry),
        halted=True,
        current_cycle_allowed=False,
        last_finalized_expiry=cycle.expiry,
    )
    return updated, RiskEntry(cycle.expiry, cycle.expiry, "HALTED_AT_EXCLUSION", None, None)
