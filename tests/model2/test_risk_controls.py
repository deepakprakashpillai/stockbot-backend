from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.domain.model2.assessment import assess_reliability
from app.domain.model2.calendar import WeeklyCycle
from app.domain.model2.market import EXCHANGE_ZONE
from app.domain.model2.risk import (
    RiskState,
    capital_limits,
    prepare_cycle,
    record_excluded_result,
    record_priced_result,
)

D = Decimal


def cycle(index=0):
    entry = date(2026, 1, 5) + timedelta(days=7 * index)
    return WeeklyCycle(entry + timedelta(days=1), entry, "READY")


def priced(state, index, net):
    selected = cycle(index)
    decision = prepare_cycle(state, selected)
    assert decision.allowed
    return record_priced_result(
        decision.state, datetime.combine(selected.expiry, time(15, 30), EXCHANGE_ZONE), D(net)
    )


def test_fixed_capital_limits_do_not_compound_with_prior_results():
    state = RiskState(D("1000000"))
    state, _ = priced(state, 0, "-100000")
    limits = capital_limits(state.starting_capital)
    assert (limits.target, limits.hard, limits.daily, limits.monthly, limits.margin) == (
        D("15000"),
        D("25000"),
        D("15000"),
        D("40000"),
        D("400000"),
    )


@pytest.mark.parametrize("net,allowed", [("-7499.99", True), ("-7500", False), ("-7500.01", False)])
def test_daily_loss_equality_blocks_new_entries_only(net, allowed):
    state = RiskState(
        D("500000"), day=cycle().entry_session, month=(2026, 1), day_net=D(net), month_net=D(net)
    )
    result = prepare_cycle(state, cycle())
    assert result.allowed is allowed
    assert result.reason == (None if allowed else "DAILY_LOSS_LIMIT")


@pytest.mark.parametrize(
    "net,allowed", [("-19999.99", True), ("-20000", False), ("-20000.01", False)]
)
def test_monthly_loss_equality_blocks_new_entries(net, allowed):
    state = RiskState(D("500000"), day=cycle().entry_session, month=(2026, 1), month_net=D(net))
    result = prepare_cycle(state, cycle())
    assert result.allowed is allowed
    assert result.reason == (None if allowed else "MONTHLY_LOSS_LIMIT")


def test_profits_offset_monthly_losses_and_daily_net_resets_on_new_day():
    state = RiskState(D("500000"))
    state, _ = priced(state, 0, "5000")
    state, _ = priced(state, 1, "-20000")
    result = prepare_cycle(state, cycle(2))
    assert result.allowed and result.state.month_net == D("-15000")
    assert result.state.day_net == 0


def test_three_losses_pause_exactly_two_scheduled_cycles_across_a_month_boundary():
    state = RiskState(D("500000"))
    for index in range(3):
        state, _ = priced(state, index, "-100")
    assert state.losing_streak == 3 and state.pause_remaining == 2
    first = prepare_cycle(state, cycle(3))
    assert first.reason == "LOSS_STREAK_PAUSE" and first.state.pause_remaining == 1
    # A special cycle still consumes a scheduled-cycle pause, regardless of quotes.
    second = prepare_cycle(first.state, replace(cycle(4), classification="SPECIAL_SESSION"))
    assert second.reason == "LOSS_STREAK_PAUSE" and second.state.pause_remaining == 0
    assert second.state.losing_streak == 0
    assert prepare_cycle(second.state, cycle(5)).allowed


def test_zero_preserves_the_streak_and_a_win_resets_it():
    state = RiskState(D("500000"))
    for index in range(2):
        state, _ = priced(state, index, "-100")
    state, _ = priced(state, 2, "0")
    assert state.losing_streak == 2 and state.pause_remaining == 0
    state, _ = priced(state, 3, "1")
    assert state.losing_streak == 0


def test_excluded_risk_is_booked_to_expiry_month_and_is_not_priced_performance():
    selected = WeeklyCycle(date(2026, 9, 1), date(2026, 8, 31), "READY")
    state = RiskState(D("500000"), day=date(2026, 8, 31), month=(2026, 8), month_net=D("5000"))
    state = prepare_cycle(state, selected).state
    state, entry = record_excluded_result(state, D("12100"), "MAX_LOSS_RISK")
    assert entry.booking_date == date(2026, 9, 1) and entry.cycle_expiry == selected.expiry
    assert entry.source == "ASSUMED_MAX_LOSS" and entry.risk_amount == D("-12100")
    assert entry.priced_performance_amount is None
    assert state.month == (2026, 9) and state.day == date(2026, 9, 1)
    assert state.month_net == state.day_net == D("-12100")  # Charges already included.
    assert state.losing_streak == 1


def test_exclusion_scenarios_change_later_eligibility_without_inventing_returns():
    state = RiskState(D("500000"))
    state, _ = priced(state, 0, "-15000")
    admitted = prepare_cycle(state, cycle(1)).state
    maximum, maximum_entry = record_excluded_result(admitted, D("12100"), "MAX_LOSS_RISK")
    zero, zero_entry = record_excluded_result(admitted, D("12100"), "ZERO_RISK")
    halted, halted_entry = record_excluded_result(admitted, D("12100"), "HALT_AT_FIRST_EXCLUSION")
    assert (
        maximum_entry.priced_performance_amount
        is zero_entry.priced_performance_amount
        is halted_entry.priced_performance_amount
        is None
    )
    assert maximum_entry.risk_amount == D("-12100") and zero_entry.risk_amount == 0
    assert halted_entry.risk_amount is None and halted.halted
    assert prepare_cycle(maximum, cycle(2)).reason == "MONTHLY_LOSS_LIMIT"
    assert prepare_cycle(zero, cycle(2)).allowed
    assert prepare_cycle(halted, cycle(2)).reason == "HALTED_AT_EXCLUSION"
    assert zero.losing_streak == 1 and maximum.losing_streak == 2


def test_recording_is_once_per_eligible_cycle_and_chronological():
    state = prepare_cycle(RiskState(D("500000")), cycle()).state
    state, entry = record_priced_result(
        state, datetime.combine(cycle().expiry, time(15, 30), EXCHANGE_ZONE), D("100")
    )
    assert entry.priced_performance_amount == 100
    with pytest.raises(ValueError, match="unfinalized"):
        record_excluded_result(state, D("1000"), "MAX_LOSS_RISK")
    with pytest.raises(ValueError, match="unique"):
        prepare_cycle(state, cycle())
    with pytest.raises(ValueError, match="chronological"):
        prepare_cycle(state, WeeklyCycle(date(2026, 1, 13), date(2026, 1, 5), "READY"))
    with pytest.raises(FrozenInstanceError):
        state.starting_capital = D("100")


def test_outside_range_does_not_consume_pause_or_change_state():
    state = RiskState(D("500000"), losing_streak=3, pause_remaining=2)
    result = prepare_cycle(state, replace(cycle(), classification="OUTSIDE_COMPLETE_CYCLE_RANGE"))
    assert result.state is state and result.state.pause_remaining == 2


@pytest.mark.parametrize(
    "opened,excluded,status,passed",
    [
        (100, 2, "RELIABLE_DATA", True),
        (100, 3, "UNRELIABLE_DATA", False),
        (40, 2, "UNRELIABLE_DATA", False),
        (50, 1, "RELIABLE_DATA", True),
        (49, 1, "UNRELIABLE_DATA", False),
        (51, 1, "RELIABLE_DATA", True),
        (40, 0, "RELIABLE_DATA", True),
        (0, 0, "INSUFFICIENT_SAMPLE", False),
    ],
)
def test_approved_reliability_uses_opened_cycles_with_a_strict_two_percent_boundary(
    opened, excluded, status, passed
):
    value = assess_reliability(opened, excluded)
    assert value.status == status and value.data_gate_passed is passed
    assert value.opened_cycles == opened and value.excluded_cycles == excluded
    assert (value.exclusion_fraction is None) == (opened == 0)


@pytest.mark.parametrize(
    "opened,excluded", [(True, 0), (10, False), (10.0, 1), (10, 1.0), (-1, 0), (1, -1), (1, 2)]
)
def test_invalid_reliability_counts_cannot_dilute_or_hide_exclusions(opened, excluded):
    with pytest.raises(ValueError):
        assess_reliability(opened, excluded)


@pytest.mark.parametrize("capital", [D("0"), D("-1"), D("NaN"), 500000])
def test_invalid_capital_is_not_implicitly_coerced(capital):
    with pytest.raises(ValueError):
        RiskState(capital)
