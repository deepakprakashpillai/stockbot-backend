from dataclasses import FrozenInstanceError, replace
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from clock_fixture import SOURCE, fixture

from app.domain.model2.clock import choose_exit_reason, iter_replay
from app.domain.model2.execution import POLICIES, market_at
from app.domain.model2.market import BAR_INTERVAL, HaltObservation, SettlementObservation
from app.domain.model2.scenarios import run_scenario, run_scenarios

EXPIRY = date(2026, 1, 6)
ENTRY = date(2026, 1, 5)


def replay(request, feed, calendar, policy="MAX_LOSS_RISK"):
    return run_scenario(request, feed, calendar, policy, keep_events=True)


def events(result, kind):
    return [event for event in result.events if event.kind == kind]


def test_complete_golden_time_exit_has_one_entry_four_legs_one_fill_and_exact_money():
    request, feed, calendar = fixture()
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert (cycle.state, cycle.reason, cycle.finalized_at) == (
        "CLOSED",
        "TIME",
        market_at(EXPIRY, 14, 30),
    )
    assert cycle.entry.execution_at == market_at(ENTRY, 15, 20)
    assert cycle.entry.credit_points == 40
    assert cycle.entry.maximum_loss_inr == 10800
    assert (
        cycle.valuation.gross_pnl_inr,
        cycle.valuation.charges_inr,
        cycle.valuation.net_pnl_inr,
    ) == (0, 400, -400)
    assert cycle.risk_entry.risk_amount == -400
    assert cycle.exit_at == cycle.finalized_at
    assert result.summary.priced_net_pnl == -400
    assert result.summary.opened_cycles == result.summary.priced_cycles == 1
    assert result.summary.reliability.data_gate_passed
    assert len(events(result, "ENTRY_OPENED")) == len(events(result, "EXIT_ATTEMPT")) == 1
    assert len(cycle.entry.legs) == 4
    assert all(leg.contract.expiry == EXPIRY for leg in cycle.entry.legs)
    assert [event.sequence for event in result.events] == list(range(1, len(result.events) + 1))
    assert [event.at for event in result.events] == sorted(event.at for event in result.events)
    assert all(when <= at for _, when, at in feed.calls)
    at_exit = [event.kind for event in result.events if event.at == cycle.finalized_at]
    assert at_exit == [
        "COMPLETED_CANDLE",
        "EXIT_TRIGGERED",
        "EXIT_ATTEMPT",
        "CYCLE_FINALIZED",
        "RUN_FINISHED",
    ]


@pytest.mark.parametrize(
    "width,slip,credit,loss",
    [(150, "0", "40", "7550"), (200, "1", "39.2", "10852"), (200, "2", "38.4", "10904")],
)
def test_both_widths_and_slippage_scenarios_survive_whole_clock(width, slip, credit, loss):
    request, feed, calendar = fixture(width=width, slip=slip)
    (cycle,) = replay(request, feed, calendar).cycles
    assert cycle.entry.credit_points == Decimal(credit)
    assert cycle.entry.maximum_loss_inr == Decimal(loss)
    assert cycle.valuation.net_pnl_inr == Decimal({"0": "-400", "1": "-504", "2": "-608"}[slip])


def test_entry_missing_open_retries_once_with_original_decision_strikes():
    request, feed, calendar = fixture()
    key = f"{EXPIRY}:SHORT_CALL", market_at(ENTRY, 15, 20)
    feed.prices[key] = ("0", "30", 100)  # Valid at close for the retry's preceding check.
    result = replay(request, feed, calendar)
    assert [event.at.time() for event in events(result, "ENTRY_DECISION")] == [
        time(15, 20),
        time(15, 25),
    ]
    assert result.cycles[0].entry.execution_at.time() == time(15, 25)
    assert result.cycles[0].entry.selection.spot == 25000


@pytest.mark.parametrize("bad_previous", [False, True])
def test_entry_data_failure_stops_after_1525_and_never_opens(bad_previous):
    request, feed, calendar = fixture()
    for minute in (15, 20, 25):
        if bad_previous:
            feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(ENTRY, 15, minute))] = ("30", "30", 0)
        else:
            feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(ENTRY, 15, minute))] = None
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == "NO_VALID_ENTRY"
    assert result.summary.opened_cycles == 0
    assert result.summary.reliability.status == "INSUFFICIENT_SAMPLE"
    assert len(events(result, "ENTRY_DECISION")) == 2
    assert not events(result, "ENTRY_OPENED")


def test_risk_cap_rejection_is_not_retried_even_if_later_price_would_pass():
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 20), short_open="20", long_open="14")
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == "RISK_CAP"
    assert len(events(result, "ENTRY_DECISION")) == 1
    assert events(result, "ENTRY_DECISION")[0].entry.maximum_loss_inr == 12620
    assert not any(
        "open:" in kind and when == market_at(ENTRY, 15, 25) for kind, when, _ in feed.calls
    )


@pytest.mark.parametrize("name", ["spot", "india_vix"])
def test_missing_entry_input_skips_without_inventing_a_price(name):
    request, feed, calendar = fixture()
    feed.indexes[(name, market_at(ENTRY, 15, 15))] = None
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == "NO_VALID_ENTRY_INPUT"
    assert not events(result, "ENTRY_DECISION")


def test_missing_contract_is_diagnostic_and_does_not_read_a_replacement():
    request, feed, calendar = fixture()
    del feed.contracts[(EXPIRY, 24550, "PE")]
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == "MISSING_CONTRACT"
    assert result.events[0].missing == ("LONG_PUT:24550:PE",)


@pytest.mark.parametrize(
    "trigger,short_close,open_short,reason,net",
    [("target", "15", "12.5", "TARGET", "1225"), ("stop", "55", "65", "STOP", "-4950")],
)
def test_completed_close_triggers_then_actual_same_timestamp_next_open_fills(
    trigger, short_close, open_short, reason, net
):
    request, feed, calendar = fixture()
    feed.set_prices(
        EXPIRY,
        market_at(EXPIRY, 10, 0),
        short_close=short_close,
        long_close="5" if trigger == "target" else "10",
    )
    feed.set_prices(
        EXPIRY,
        market_at(EXPIRY, 10, 5),
        short_open=open_short,
        long_open="5" if trigger == "target" else "10",
    )
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.reason == reason
    assert cycle.trigger.at == cycle.finalized_at == market_at(EXPIRY, 10, 5)
    assert cycle.valuation.net_pnl_inr == Decimal(net)
    assert cycle.entry.execution_at < cycle.trigger.at
    assert events(result, "EXIT_ATTEMPT")[0].quotes.execution_at == cycle.finalized_at


@pytest.mark.parametrize(
    "reason,short_close,long_close", [("TARGET", "15", "5"), ("STOP", "55", "10")]
)
def test_1530_trigger_stays_pending_overnight_and_fills_actual_open(
    reason, short_close, long_close
):
    request, feed, calendar = fixture()
    feed.set_prices(
        EXPIRY, market_at(ENTRY, 15, 25), short_close=short_close, long_close=long_close
    )
    feed.set_prices(EXPIRY, market_at(EXPIRY, 9, 15), short_open="35")
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.trigger.original_reason == reason
    assert cycle.trigger.original_at == market_at(ENTRY, 15, 30)
    assert cycle.finalized_at == market_at(EXPIRY, 9, 15)
    assert cycle.valuation.exit_debit_points == 50
    assert cycle.valuation.net_pnl_inr == -1050
    assert [event.at for event in events(result, "OVERNIGHT_PENDING")] == [market_at(ENTRY, 15, 30)]
    assert not any(event.at.time() == time(15, 30) for event in events(result, "EXIT_ATTEMPT"))


def test_pending_target_is_overtaken_by_gap_stop_with_secondary_tag_and_one_fill():
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 25), short_close="15", long_close="5")
    feed.set_prices(EXPIRY, market_at(EXPIRY, 9, 15), short_open="70")
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.reason == "STOP_GAP"
    assert cycle.trigger.original_reason == "TARGET"
    assert cycle.trigger.tags == ("PENDING_TARGET",)
    assert cycle.valuation.exit_debit_points == 120
    assert cycle.valuation.net_pnl_inr == -5600
    assert cycle.exit_attempts == 1
    assert len(events(result, "CYCLE_FINALIZED")) == 1


def test_opening_gap_is_checked_before_expiry_candle_closes_without_pending_exit():
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(EXPIRY, 9, 15), short_open="70", short_close="30")
    (cycle,) = replay(request, feed, calendar).cycles
    assert (cycle.reason, cycle.finalized_at) == ("STOP_GAP", market_at(EXPIRY, 9, 15))
    assert cycle.valuation.exit_debit_points == 120


@pytest.mark.parametrize(
    "missing_slots,fill_minute,late", [(1, 20, False), (2, 25, False), (3, 30, True)]
)
def test_overnight_retries_count_only_scheduled_opens(missing_slots, fill_minute, late):
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 25), short_close="55")
    for minute in (15, 20, 25)[:missing_slots]:
        feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(EXPIRY, 9, minute))] = ("0", "30", 100)
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.finalized_at == market_at(EXPIRY, 9, fill_minute)
    assert cycle.exit_attempts == missing_slots + 1
    assert ("LATE_FILL" in cycle.flags) is late
    assert [event.at for event in events(result, "EXIT_ATTEMPT")] == [
        market_at(EXPIRY, 9, 15) + i * BAR_INTERVAL for i in range(missing_slots + 1)
    ]


@pytest.mark.parametrize(
    "missing_slots,fill_minute,late",
    [(0, 30, False), (1, 35, False), (2, 40, False), (3, 45, True)],
)
def test_time_exit_retries_and_first_later_fill(missing_slots, fill_minute, late):
    request, feed, calendar = fixture()
    for minute in (30, 35, 40)[:missing_slots]:
        feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(EXPIRY, 14, minute))] = ("0", "30", 100)
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.finalized_at == market_at(EXPIRY, 14, fill_minute)
    assert cycle.trigger.original_at == market_at(EXPIRY, 14, 30)
    assert ("LATE_FILL" in cycle.flags) is late
    assert cycle.exit_attempts == missing_slots + 1


@pytest.mark.parametrize(
    "reasons,expected",
    [
        (tuple(), None),
        (("TARGET", "TIME", "STOP", "EMERGENCY"), "EMERGENCY"),
        (("TARGET", "TIME", "STOP"), "STOP"),
        (("TARGET", "TIME"), "TIME"),
    ],
)
def test_priority_is_explicit_and_stable(reasons, expected):
    assert choose_exit_reason(reasons) == expected


def test_halt_wins_over_pending_target_and_open_gap_stop():
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 25), short_close="15", long_close="5")
    at = market_at(EXPIRY, 9, 15)
    feed.set_prices(EXPIRY, at, short_open="70")
    feed.halt_records.append((HaltObservation(at, at, SOURCE), at + 2 * BAR_INTERVAL))
    (cycle,) = replay(request, feed, calendar).cycles
    assert cycle.reason == "EMERGENCY"
    assert cycle.trigger.original_reason == "TARGET"
    assert cycle.exit_attempts == 1
    assert cycle.valuation.net_pnl_inr == -5600


@pytest.mark.parametrize("current,expected", [("18.4", "EMERGENCY"), ("18.399", "TIME")])
def test_same_session_vix_spike_at_exact_fifteen_percent(current, expected):
    request, feed, calendar = fixture()
    feed.indexes[("india_vix", market_at(EXPIRY, 9, 45))] = current
    (cycle,) = replay(request, feed, calendar).cycles
    assert cycle.reason == expected
    if expected == "EMERGENCY":
        assert cycle.finalized_at == market_at(EXPIRY, 9, 50)


def test_vix_does_not_compare_open_to_previous_day_close():
    request, feed, calendar = fixture()
    for minute in range(15, 45, 5):
        feed.indexes[("india_vix", market_at(EXPIRY, 9, minute))] = "25"
    result = replay(request, feed, calendar)
    assert not [
        event for event in events(result, "EXIT_TRIGGERED") if event.at < market_at(EXPIRY, 9, 50)
    ]


def test_vix_fallback_is_one_completed_candle_and_never_a_future_current_bar():
    request, feed, calendar = fixture()
    feed.indexes[("india_vix", market_at(EXPIRY, 10, 0))] = None
    feed.indexes[("india_vix", market_at(EXPIRY, 10, 5))] = "25"
    result = replay(request, feed, calendar)
    assert result.cycles[0].trigger.at == market_at(EXPIRY, 10, 10)
    assert all(when <= at for _, when, at in feed.calls)


def test_vix_gap_logs_without_spurious_emergency_when_reference_is_unavailable():
    request, feed, calendar = fixture()
    for minute in (15, 20, 25):
        feed.indexes[("india_vix", market_at(EXPIRY, 9, minute))] = None
    # Three gaps cause a legitimate feed-loss emergency at 09:30. Make the
    # unavailable reference a missing *historical* context instead.
    for minute in (15, 20, 25):
        feed.indexes[("india_vix", market_at(ENTRY, 14, minute))] = None
    # At 15:20 entry, the emergency reference is irrelevant; at 15:25 the
    # 14:55 reference can be absent without affecting the live feed count.
    for minute in (45, 50, 55):
        feed.indexes[("india_vix", market_at(ENTRY, 14, minute))] = None
    result = replay(request, feed, calendar)
    assert events(result, "VIX_GAP")
    assert not [event for event in events(result, "EXIT_TRIGGERED") if event.at.date() == ENTRY]


def test_feed_loss_is_three_missing_scheduled_closes_and_closure_does_not_increment():
    request, feed, calendar = fixture()
    for day, hour, minute in ((ENTRY, 15, 20), (ENTRY, 15, 25), (EXPIRY, 9, 15)):
        feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(day, hour, minute))] = ("30", "30", 0)
    result = replay(request, feed, calendar)
    lost = [event for event in events(result, "COMPLETED_CANDLE") if event.consecutive_missing]
    assert [(event.at, event.consecutive_missing) for event in lost[:3]] == [
        (market_at(ENTRY, 15, 25), 1),
        (market_at(ENTRY, 15, 30), 2),
        (market_at(EXPIRY, 9, 20), 3),
    ]
    # 09:20 fails preceding volume, so the first actual valid opening is 09:25.
    assert result.cycles[0].trigger.original_at == market_at(EXPIRY, 9, 20)
    assert result.cycles[0].finalized_at == market_at(EXPIRY, 9, 25)
    assert result.cycles[0].reason == "EMERGENCY"


@pytest.mark.parametrize(
    "spot,debit,net",
    [
        ("25000", "0", "2200"),
        ("25300", "50", "-1050"),
        ("25600", "200", "-10800"),
        ("24400", "200", "-10800"),
    ],
)
def test_no_exit_quotes_settle_at_official_intrinsic_with_cost_once(spot, debit, net):
    request, feed, calendar = fixture()
    feed.no_quotes_after_entry(EXPIRY)
    feed.settlements[EXPIRY] = SettlementObservation(
        EXPIRY, Decimal(spot), market_at(EXPIRY, 15, 35), SOURCE
    )
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.state == "SETTLED"
    assert cycle.valuation.exit_debit_points == Decimal(debit)
    assert cycle.valuation.net_pnl_inr == Decimal(net)
    assert cycle.valuation.charges_inr == 400
    assert "DATA_INCOMPLETE" in cycle.flags
    assert result.summary.priced_cycles == result.summary.settled_cycles == 1
    assert result.summary.excluded_cycles == 0
    assert cycle.risk_entry.booking_date == EXPIRY


def test_settlement_ignores_fill_slippage_and_does_not_query_before_declared_checkpoint():
    request, feed, calendar = fixture(slip="2")
    feed.no_quotes_after_entry(EXPIRY)
    feed.settlements[EXPIRY] = SettlementObservation(
        EXPIRY, Decimal("25000"), market_at(EXPIRY, 15, 35), SOURCE
    )
    result = replay(request, feed, calendar)
    assert result.cycles[0].valuation.net_pnl_inr == 2096
    assert [(when, at) for kind, when, at in feed.calls if kind == "settlement"] == [
        (market_at(EXPIRY, 15, 35), market_at(EXPIRY, 15, 35))
    ]


@pytest.mark.parametrize(
    "policy,source,risk_amount,scope",
    [
        ("MAX_LOSS_RISK", "ASSUMED_MAX_LOSS", "-10800", "FULL_REQUESTED_RANGE"),
        ("ZERO_RISK", "ASSUMED_ZERO", "0", "FULL_REQUESTED_RANGE"),
        ("HALT_AT_FIRST_EXCLUSION", "HALTED_AT_EXCLUSION", None, "HALTED_AT_EXCLUSION"),
    ],
)
def test_no_settlement_exclusion_keeps_unknown_pnl_out_of_performance(
    policy, source, risk_amount, scope
):
    request, feed, calendar = fixture()
    feed.no_quotes_after_entry(EXPIRY)
    result = replay(request, feed, calendar, policy)
    (cycle,) = result.cycles
    assert cycle.state == "EXCLUDED"
    assert cycle.exit_at is None
    assert cycle.valuation is None
    assert cycle.risk_entry.source == source
    assert cycle.risk_entry.risk_amount == (
        Decimal(risk_amount) if risk_amount is not None else None
    )
    assert cycle.risk_entry.priced_performance_amount is None
    assert result.summary.priced_net_pnl == result.summary.priced_cycles == 0
    assert result.summary.excluded_cycles == result.summary.opened_cycles == 1
    assert result.summary.reliability.status == "UNRELIABLE_DATA"
    assert result.summary.scope == scope
    assert not result.summary.full_period_data_gate_passed


def test_independent_scenarios_replay_different_future_entries_not_just_adjusted_totals():
    expiries = tuple(date(2026, 1, day) for day in (6, 13, 20))
    request, feed, calendar = fixture(expiries=expiries)
    for expiry in expiries:
        feed.no_quotes_after_entry(expiry)
    primary, zero, halted = run_scenarios(request, feed, calendar, keep_events=True)
    assert tuple(result.policy for result in (primary, zero, halted)) == POLICIES
    assert [cycle.state for cycle in primary.cycles] == ["EXCLUDED", "EXCLUDED", "SKIPPED"]
    assert primary.cycles[-1].reason == "MONTHLY_LOSS_LIMIT"
    assert primary.summary.assumed_risk_net == -21600
    assert primary.summary.final_risk_state.losing_streak == 2
    assert [cycle.state for cycle in zero.cycles] == ["EXCLUDED"] * 3
    assert zero.summary.assumed_risk_net == zero.summary.final_risk_state.losing_streak == 0
    assert len(halted.cycles) == 1
    assert halted.summary.scheduled_cycles == 3
    assert halted.summary.visited_cycles == halted.summary.opened_cycles == 1
    assert halted.summary.scope == "HALTED_AT_EXCLUSION"
    assert all(result.summary.priced_net_pnl == 0 for result in (primary, zero, halted))
    assert primary.request is zero.request is halted.request


def test_exclusion_books_expiry_month_and_later_missing_evidence_never_mutates_prior_result():
    expiry = date(2026, 9, 1)
    request, feed, calendar = fixture(expiries=(expiry,))
    feed.no_quotes_after_entry(expiry)
    before = replay(request, feed, calendar)
    assert before.cycles[0].entry.execution_at.date() == date(2026, 8, 31)
    assert before.cycles[0].risk_entry.booking_date == expiry
    assert before.summary.final_risk_state.month == (2026, 9)
    assert before.summary.final_risk_state.month_net == -10800
    feed.settlements[expiry] = SettlementObservation(
        expiry, Decimal("25000"), market_at(expiry, 15, 35), SOURCE
    )
    new_request = replace(request, dataset_identity="synthetic-dataset@immutable-2")
    after = replay(new_request, feed, calendar)
    assert after.cycles[0].state == "SETTLED"
    assert before.cycles[0].state == "EXCLUDED"
    assert before.cycles[0].valuation is None
    with pytest.raises(FrozenInstanceError):
        before.summary.priced_net_pnl = after.summary.priced_net_pnl


def test_zero_events_collector_and_stream_have_identical_outputs():
    request, feed, calendar = fixture()
    streamed = list(iter_replay(request, feed, calendar, "MAX_LOSS_RISK"))
    collected = run_scenario(request, feed, calendar, "MAX_LOSS_RISK")
    assert collected.events == ()
    assert collected.summary == streamed[-1].summary
    assert collected.cycles == tuple(event.result for event in streamed if event.result)


def test_cancellation_with_open_position_retains_state_without_fabricated_result():
    request, feed, calendar = fixture()
    stream = iter_replay(
        request, feed, calendar, "MAX_LOSS_RISK", cancelled=lambda: len(feed.calls) > 25
    )
    emitted = list(stream)
    cancelled = [event for event in emitted if event.kind == "CANCELLED"]
    assert len(cancelled) == 1
    assert cancelled[0].entry is not None
    assert not [event for event in emitted if event.result]
    assert emitted[-1].summary.scope == "CANCELLED"
    assert emitted[-1].summary.opened_cycles == 1
    assert emitted[-1].summary.priced_cycles == 0
    assert not emitted[-1].summary.full_period_data_gate_passed


@pytest.mark.parametrize("change", ["missing", "before_close", "naive", "duplicate"])
def test_settlement_knowledge_checkpoint_is_explicit_and_validated(change):
    request, feed, calendar = fixture()
    clocks = {
        "missing": (),
        "before_close": ((EXPIRY, market_at(EXPIRY, 15, 25)),),
        "naive": ((EXPIRY, market_at(EXPIRY, 15, 35).replace(tzinfo=None)),),
        "duplicate": request.settlement_checks * 2,
    }[change]
    with pytest.raises(ValueError):
        run_scenario(replace(request, settlement_checks=clocks), feed, calendar, "MAX_LOSS_RISK")
    assert feed.calls == []


def test_invalid_policy_never_reads_market_data():
    request, feed, calendar = fixture()
    with pytest.raises(ValueError):
        run_scenario(request, feed, calendar, "WRONG")
    assert feed.calls == []


def test_three_losses_pause_exactly_two_scheduled_cycles_then_resume_in_new_month():
    expiries = tuple(date(2026, 1, day) for day in (6, 13, 20, 27)) + (
        date(2026, 2, 3),
        date(2026, 2, 10),
    )
    request, feed, calendar = fixture(expiries=expiries)
    result = replay(request, feed, calendar)
    assert [cycle.state for cycle in result.cycles] == ["CLOSED"] * 3 + ["SKIPPED"] * 2 + ["CLOSED"]
    assert [cycle.reason for cycle in result.cycles[3:5]] == ["LOSS_STREAK_PAUSE"] * 2
    assert result.summary.opened_cycles == 4
    assert result.summary.priced_net_pnl == -1600
    assert result.summary.final_risk_state.losing_streak == 1
    assert result.summary.final_risk_state.pause_remaining == 0
    assert result.summary.final_risk_state.month_net == -400
    assert result.summary.final_risk_state.starting_capital == 500000


def test_special_cycle_consumes_pause_without_reading_its_prices():
    expiries = tuple(date(2026, 1, day) for day in (6, 13, 20, 27)) + (
        date(2026, 2, 3),
        date(2026, 2, 10),
    )
    request, feed, calendar = fixture(expiries=expiries)
    calendar = replace(
        calendar,
        sessions=tuple(
            replace(day, kind="SPECIAL") if day.session_date == date(2026, 1, 27) else day
            for day in calendar.sessions
        ),
    )
    feed.calendar = calendar
    result = replay(request, feed, calendar)
    assert result.cycles[3].reason == "LOSS_STREAK_PAUSE"
    assert result.cycles[5].state == "CLOSED"
    assert not any(at.date() in {date(2026, 1, 26), date(2026, 1, 27)} for _, _, at in feed.calls)


def test_boundary_and_special_cycles_remain_visible_without_opening_positions():
    request, feed, calendar = fixture(expiries=(EXPIRY, date(2026, 1, 13)))
    boundary = replace(request, start=EXPIRY)
    calendar = replace(
        calendar,
        sessions=tuple(
            replace(day, kind="SPECIAL") if day.session_date == date(2026, 1, 13) else day
            for day in calendar.sessions
        ),
    )
    result = replay(boundary, feed, calendar)
    assert [cycle.reason for cycle in result.cycles] == [
        "OUTSIDE_COMPLETE_CYCLE_RANGE",
        "SPECIAL_SESSION",
    ]
    assert result.summary.scheduled_cycles == result.summary.skipped_cycles == 2
    assert result.summary.opened_cycles == 0
    assert feed.calls == []


def test_experimental_filter_falls_back_to_last_previous_session_bar_at_or_after_1500():
    request, feed, calendar = fixture(filter_on=True)
    previous = date(2026, 1, 2)
    for minute in (25, 20, 15, 10, 5):
        feed.indexes[("india_vix", market_at(previous, 15, minute))] = None
    feed.indexes[("india_vix", market_at(previous, 15, 0))] = "16"
    result = replay(request, feed, calendar)
    assert result.cycles[0].state == "CLOSED"
    past = [when for name, when, _ in feed.calls if name == "india_vix" and when.date() == previous]
    assert min(past).time() == time(15, 5)


@pytest.mark.parametrize("filter_on", [False, True])
def test_unknown_experimental_reference_only_skips_when_filter_is_on(filter_on):
    request, feed, calendar = fixture(filter_on=filter_on)
    for minute in range(0, 30, 5):
        feed.indexes[("india_vix", market_at(date(2026, 1, 2), 15, minute))] = None
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == ("VIX_FILTER_UNKNOWN" if filter_on else "TIME")
    if filter_on:
        assert result.summary.opened_cycles == 0


def test_poisoning_execution_candle_future_volume_cannot_prevent_entry_at_1520():
    request, feed, calendar = fixture()
    for contract in feed.contracts.values():
        opening = "30" if "SHORT" in contract.contract_id else "10"
        feed.prices[(contract.contract_id, market_at(ENTRY, 15, 20))] = (opening, opening, 0)
    result = replay(request, feed, calendar)
    assert result.cycles[0].entry.execution_at == market_at(ENTRY, 15, 20)
    assert events(result, "ENTRY_DECISION")[0].quotes.legs[0].preceding.volume == 100
    assert events(result, "COMPLETED_CANDLE")[0].consecutive_missing == 1


def test_last_allowed_1525_open_can_fill_and_no_1530_open_is_attempted():
    request, feed, calendar = fixture()
    feed.no_quotes_after_entry(EXPIRY)
    # The prior candle can authorize the 15:25 fill even though its own open
    # was unusable; all legs must be synchronized at the actual fill.
    feed.set_prices(
        EXPIRY,
        market_at(EXPIRY, 15, 20),
        short_open="0",
        long_open="0",
        short_close="30",
        long_close="10",
    )
    feed.set_prices(EXPIRY, market_at(EXPIRY, 15, 25), short_open="35", long_open="10")
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.finalized_at == market_at(EXPIRY, 15, 25)
    assert cycle.valuation.net_pnl_inr == -1050
    assert "LATE_FILL" in cycle.flags
    assert not any(
        kind.startswith("open:") and when.time() == time(15, 30) for kind, when, _ in feed.calls
    )
    assert not any(kind == "settlement" for kind, _, _ in feed.calls)


def test_settlement_after_snapshot_checkpoint_is_not_used_early():
    request, feed, calendar = fixture()
    feed.no_quotes_after_entry(EXPIRY)
    feed.settlements[EXPIRY] = SettlementObservation(
        EXPIRY, Decimal("25000"), market_at(EXPIRY, 15, 40), SOURCE
    )
    result = replay(request, feed, calendar)
    assert result.cycles[0].state == "EXCLUDED"
    later = replay(
        replace(
            request,
            dataset_identity="later-observation-snapshot",
            settlement_checks=((EXPIRY, market_at(EXPIRY, 15, 40)),),
        ),
        feed,
        calendar,
    )
    assert later.cycles[0].state == "SETTLED"
    assert later.cycles[0].settlement.provenance == SOURCE
    assert events(later, "CYCLE_FINALIZED")[0].settlement_observation == later.cycles[0].settlement


def test_winning_result_resets_streak_but_zero_net_result_leaves_it_unchanged():
    request, feed, calendar = fixture(
        expiries=(EXPIRY, date(2026, 1, 13), date(2026, 1, 20)), width=150
    )
    # Second trade earns exactly the charges: gross 400, net zero. Tick size
    # and quantity are changed in metadata, not inferred by the engine.
    second = date(2026, 1, 13)
    for key, contract in list(feed.contracts.items()):
        if contract.expiry == second:
            feed.contracts[key] = replace(contract, lot_size=100)
    feed.set_prices(second, market_at(second, 14, 30), short_open="28", long_open="10")
    # Third trade wins 900 net at the target.
    third = date(2026, 1, 20)
    feed.set_prices(third, market_at(third, 10, 0), short_close="20", long_close="10")
    feed.set_prices(third, market_at(third, 10, 5), short_open="20", long_open="10")
    result = replay(request, feed, calendar)
    assert [cycle.valuation.net_pnl_inr for cycle in result.cycles] == [-400, 0, 900]
    finalized = events(result, "CYCLE_FINALIZED")
    assert [event.risk_state.losing_streak for event in finalized] == [1, 1, 0]
    assert result.summary.priced_net_pnl == 500


def test_cancelled_group_does_not_start_later_children_or_manufacture_results():
    request, feed, calendar = fixture()
    results = run_scenarios(request, feed, calendar, cancelled=lambda: True, keep_events=True)
    assert len(results) == 1
    assert results[0].summary.scope == "CANCELLED"
    assert results[0].summary.visited_cycles == 0
    assert results[0].cycles == ()
    assert feed.calls == []


def test_empty_cycle_range_has_no_false_reliability_pass():
    request, feed, calendar = fixture()
    result = replay(
        replace(request, start=date(2026, 1, 2), end=date(2026, 1, 2), settlement_checks=()),
        feed,
        calendar,
    )
    assert result.summary.scheduled_cycles == 0
    assert result.summary.reliability.status == "INSUFFICIENT_SAMPLE"
    assert not result.summary.full_period_data_gate_passed
    assert feed.calls == []


def test_emergency_evidence_keeps_the_qualified_halt_source():
    request, feed, calendar = fixture()
    at = market_at(EXPIRY, 10, 0)
    halt = HaltObservation(at, at, SOURCE)
    feed.halt_records.append((halt, at + BAR_INTERVAL))
    result = replay(request, feed, calendar)
    assert events(result, "EMERGENCY_EVIDENCE")[0].halt_observations == (halt,)
    assert result.cycles[0].finalized_at == at


def test_gap_stop_waits_for_first_valid_open_without_using_missing_price():
    request, feed, calendar = fixture()
    feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(EXPIRY, 9, 15))] = ("0", "30", 100)
    feed.set_prices(EXPIRY, market_at(EXPIRY, 9, 20), short_open="70", short_close="30")
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == "STOP_GAP"
    assert result.cycles[0].finalized_at == market_at(EXPIRY, 9, 20)
    assert result.cycles[0].valuation.exit_debit_points == 120


def test_retry_window_can_start_before_overnight_and_resume_without_reset():
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 20), short_close="15", long_close="5")
    feed.set_prices(
        EXPIRY,
        market_at(ENTRY, 15, 25),
        short_open="0",
        long_open="0",
        short_close="15",
        long_close="5",
    )
    feed.prices[(f"{EXPIRY}:SHORT_CALL", market_at(EXPIRY, 9, 15))] = ("0", "30", 100)
    result = replay(request, feed, calendar)
    (cycle,) = result.cycles
    assert cycle.finalized_at == market_at(EXPIRY, 9, 20)
    assert cycle.exit_attempts == 3
    assert "LATE_FILL" not in cycle.flags
    assert [event.at for event in events(result, "EXIT_ATTEMPT")] == [
        market_at(ENTRY, 15, 25),
        market_at(EXPIRY, 9, 15),
        market_at(EXPIRY, 9, 20),
    ]


def test_qualified_halt_at_1530_is_carried_to_next_session_with_source():
    request, feed, calendar = fixture()
    at = market_at(ENTRY, 15, 30)
    feed.halt_records.append((HaltObservation(at, at, SOURCE), at + timedelta(minutes=5)))
    result = replay(request, feed, calendar)
    assert result.cycles[0].reason == "EMERGENCY"
    assert result.cycles[0].trigger.original_at == at
    assert result.cycles[0].finalized_at == market_at(EXPIRY, 9, 15)
    assert events(result, "EMERGENCY_EVIDENCE")[0].halt_observations[0].provenance == SOURCE


def test_next_day_settlement_availability_is_explicit_and_still_books_expiry_date():
    request, feed, calendar = fixture()
    feed.no_quotes_after_entry(EXPIRY)
    available = market_at(EXPIRY + timedelta(days=1), 8, 0)
    request = replace(request, settlement_checks=((EXPIRY, available),))
    feed.settlements[EXPIRY] = SettlementObservation(EXPIRY, Decimal("25000"), available, SOURCE)
    (cycle,) = replay(request, feed, calendar).cycles
    assert cycle.state == "SETTLED"
    assert cycle.finalized_at == available
    assert cycle.exit_at == market_at(EXPIRY, 15, 30)
    assert cycle.risk_entry.booking_date == EXPIRY
    assert cycle.settlement.available_at == available


def test_settlement_checkpoint_cannot_retroactively_change_a_later_entry():
    request, feed, calendar = fixture(expiries=(EXPIRY, date(2026, 1, 13)))
    request = replace(
        request,
        settlement_checks=(
            (EXPIRY, market_at(date(2026, 1, 12), 15, 25)),
            request.settlement_checks[1],
        ),
    )
    with pytest.raises(ValueError, match="before the next cycle"):
        replay(request, feed, calendar)
    assert feed.calls == []


def test_cancellation_while_pending_overnight_keeps_the_original_trigger():
    request, feed, calendar = fixture()
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 25), short_close="55")
    stop = False

    def cancelled():
        return stop

    stream = iter_replay(request, feed, calendar, "MAX_LOSS_RISK", cancelled=cancelled)
    before = []
    for event in stream:
        before.append(event)
        if event.kind == "OVERNIGHT_PENDING":
            stop = True
            break
    remaining = list(stream)
    cancellation = remaining[0]
    assert cancellation.kind == "CANCELLED"
    assert cancellation.entry == before[-1].entry
    assert cancellation.trigger.original_reason == "STOP"
    assert cancellation.trigger.original_at == market_at(ENTRY, 15, 30)
    assert not [event for event in remaining if event.result]
    assert remaining[-1].summary.scope == "CANCELLED"
    assert not [
        at for kind, at, _ in feed.calls if kind.startswith("open:") and at.date() == EXPIRY
    ]

    # Cancelling after the final scheduled close also retains all attempted
    # exits while leaving the price outcome unknown.
    request, feed, calendar = fixture()
    feed.no_quotes_after_entry(EXPIRY)
    emitted = list(
        iter_replay(
            request,
            feed,
            calendar,
            "MAX_LOSS_RISK",
            cancelled=lambda: any(
                kind.startswith("close:") and when == market_at(EXPIRY, 15, 30)
                for kind, when, _ in feed.calls
            ),
        )
    )
    cancelled_event = next(event for event in emitted if event.kind == "CANCELLED")
    assert cancelled_event.at == market_at(EXPIRY, 15, 35)
    assert cancelled_event.attempt == 73
    assert cancelled_event.consecutive_missing > 2
    assert not [event for event in emitted if event.result]
    assert not any(kind == "settlement" for kind, _, _ in feed.calls)


@pytest.mark.parametrize(
    "semantics,slip,expected_net", [("interval_start", "0", "-400"), ("interval_end", "2", "-660")]
)
def test_complete_clock_integrates_with_native_csv_parquet_feed(
    bundle_factory, semantics, slip, expected_net
):
    from app.infrastructure.market_data.model2_feed import Model2BundleFeed

    sample = bundle_factory(build=False, semantics=semantics)
    strikes = {"call_short": 25300, "call_wing": 25500, "put_short": 24700, "put_wing": 24500}
    for contract in sample.contracts:
        if contract["contract_id"] in strikes:
            contract["strike"] = strikes[contract["contract_id"]]
    sample.write_json("contracts", sample.contracts)
    for component in sample.components[:]:
        if component.kind not in {"spot", "india_vix", "options"}:
            continue
        if component.kind == "options" and component.contract_id not in strikes:
            continue
        rows = []
        value = (
            "25000"
            if component.kind == "spot"
            else "20"
            if component.kind == "india_vix"
            else "40"
            if component.contract_id.endswith("short")
            else "10"
        )
        for session in sample.calendar["sessions"]:
            day = date.fromisoformat(session["session_date"])
            if component.kind == "options" and day > EXPIRY:
                continue
            for offset in range(75):
                at = market_at(day, 9, 15) + offset * BAR_INTERVAL
                stamp = at if semantics == "interval_start" else at + BAR_INTERVAL
                rows.append(
                    {
                        "timestamp": stamp.isoformat(),
                        "open": value,
                        "high": value,
                        "low": value,
                        "close": value,
                        "volume": 100,
                    }
                )
        sample.write_rows(component, rows)
    manifest = sample.build()
    feed = Model2BundleFeed(sample.root)
    request, _, _ = fixture(slip=slip)
    result = replay(
        replace(request, dataset_identity=manifest.bundle_checksum), feed, feed.calendar
    )
    (cycle,) = result.cycles
    assert cycle.state == "CLOSED"
    assert cycle.reason == "TIME"
    assert cycle.finalized_at == market_at(EXPIRY, 14, 30)
    assert cycle.valuation.net_pnl_inr == Decimal(expected_net)
    assert not cycle.flags
    assert feed.cached_series_sessions <= 12
    assert all(leg.contract.expiry == EXPIRY for leg in cycle.entry.legs)


def test_approved_placeholder_golden_books_expiry_month_and_increments_existing_streak():
    first, second = date(2026, 8, 25), date(2026, 9, 1)
    request, feed, calendar = fixture(expiries=(first, second))
    feed.set_prices(second, market_at(date(2026, 8, 31), 15, 20), short_open="20", long_open="10")
    feed.no_quotes_after_entry(second)
    result = replay(request, feed, calendar)
    excluded = result.cycles[1]
    assert excluded.state == "EXCLUDED"
    assert excluded.entry.credit_points == 20
    assert excluded.entry.maximum_loss_inr == 12100
    assert excluded.risk_entry.risk_amount == -12100
    assert excluded.risk_entry.priced_performance_amount is None
    assert excluded.risk_entry.booking_date == second
    assert result.summary.final_risk_state.month_net == -12100
    assert result.summary.final_risk_state.losing_streak == 2
    assert result.summary.priced_net_pnl == -400


def test_approved_twenty_point_settlement_vector_has_900_net_even_in_two_percent_scenario():
    request, feed, calendar = fixture(slip="2")
    feed.set_prices(EXPIRY, market_at(ENTRY, 15, 20), short_open="20", long_open="9.4")
    feed.no_quotes_after_entry(EXPIRY)
    feed.settlements[EXPIRY] = SettlementObservation(
        EXPIRY, Decimal("25000"), market_at(EXPIRY, 15, 35), SOURCE
    )
    (cycle,) = replay(request, feed, calendar).cycles
    assert cycle.entry.credit_points == 20
    assert cycle.valuation.gross_pnl_inr == 1300
    assert cycle.valuation.net_pnl_inr == 900
    assert cycle.valuation.exit_debit_points == 0


@pytest.mark.parametrize("series", ["spot", "option", "halt", "settlement"])
def test_engine_rejects_adapter_that_returns_unavailable_future_evidence(series):
    from app.domain.model2.market import ObservationNotAvailable

    request, feed, calendar = fixture()
    if series == "spot":
        original = feed.index_close

        def future(name, end, *, as_of):
            observed = original(name, end, as_of=as_of)
            return replace(observed, interval_end=end + BAR_INTERVAL)

        feed.index_close = future
    elif series == "option":
        original = feed.option_close

        def future(contract, end, *, as_of):
            observed = original(contract, end, as_of=as_of)
            # Entries need the correct preceding candle; poison monitoring only.
            return (
                replace(observed, interval_end=end + BAR_INTERVAL)
                if end > market_at(ENTRY, 15, 20)
                else observed
            )

        feed.option_close = future
    elif series == "halt":
        feed.halts = lambda *, as_of: (
            HaltObservation(as_of + BAR_INTERVAL, as_of + BAR_INTERVAL, SOURCE),
        )
    else:
        feed.no_quotes_after_entry(EXPIRY)
        feed.settlement = lambda expiry, *, as_of: SettlementObservation(
            expiry, Decimal("25000"), as_of + BAR_INTERVAL, SOURCE
        )
    with pytest.raises(ObservationNotAvailable):
        replay(request, feed, calendar)


def test_full_clock_is_deterministic_and_carries_exact_run_identities():
    request, feed, calendar = fixture()
    first = replay(request, feed, calendar)
    second = replay(request, feed, calendar)
    assert first == second
    assert first.engine_version == "1.0.0-model2-overnight-clock"
    assert first.algorithm_key == "model2_overnight_iron_condor"
    assert first.algorithm_code_version == "1.0.0-model2-iron-condor"
    assert first.calendar_version == calendar.version
    assert first.calendar_sha256 == calendar.provenance.checksum
    assert (
        first.specification_sha256
        == "755096fd04fc4fda345673e8b08685cedf3f5d82db5b8f98c7dce6c222c188fa"
    )
    assert len(events(first, "COMPLETED_CANDLE")) == 65
    assert all(event.observations for event in events(first, "COMPLETED_CANDLE"))


@pytest.mark.parametrize("case_index", [0, 1])
def test_frozen_independent_golden_milestone_timelines(case_index):
    import json
    from pathlib import Path

    path = Path(__file__).parents[1] / "fixtures/model2/clock_goldens_v1.json"
    pack = json.loads(path.read_text())
    expected = pack["cases"][case_index]
    request, feed, calendar = fixture()
    if expected["id"] == "pending_target_opening_stop":
        feed.set_prices(EXPIRY, market_at(ENTRY, 15, 25), short_close="15", long_close="5")
        feed.set_prices(EXPIRY, market_at(EXPIRY, 9, 15), short_open="70")
    result = replay(request, feed, calendar)
    milestones = [
        [event.at.isoformat(), event.kind, event.reason]
        for event in result.events
        if event.kind != "COMPLETED_CANDLE"
    ]
    assert result.specification_sha256 == pack["specification_sha256"]
    assert milestones == expected["milestones"]
    assert result.cycles[0].valuation.net_pnl_inr == Decimal(expected["net_pnl"])
    assert len(events(result, "COMPLETED_CANDLE")) == expected["completed_candles"]
