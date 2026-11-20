"""One chronological clock: completed fields, decisions, then actual opening fills."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal
from typing import Literal

from app.domain.model2.assessment import assess_reliability
from app.domain.model2.calendar import Model2Calendar, WeeklyCycle
from app.domain.model2.execution import (
    POLICIES,
    ClockEvent,
    CycleResult,
    ExecutionFeed,
    ExitTrigger,
    ReplayRequest,
    RunSummary,
    ScenarioPolicy,
    market_at,
)
from app.domain.model2.market import (
    BAR_INTERVAL,
    CompletedObservation,
    HaltObservation,
    IndexName,
    ObservationNotAvailable,
)
from app.domain.model2.models import CloseValuation, EntryEstimate
from app.domain.model2.pricing import (
    collect_opening_quotes,
    evaluate_entry,
    invalid_opening_contracts,
    value_liquidation,
    value_settlement,
)
from app.domain.model2.risk import (
    RiskState,
    prepare_cycle,
    record_excluded_result,
    record_priced_result,
)
from app.domain.model2.selection import select_condor


def _index(
    feed: ExecutionFeed, name: IndexName, end: datetime, at: datetime
) -> CompletedObservation | None:
    if name not in {"spot", "india_vix"}:
        raise ValueError("Unknown index.")
    observed = feed.index_close(name, end, as_of=at)
    if observed is not None and (
        observed.available_at > at
        or observed.interval_end != end
        or observed.interval_start != end - BAR_INTERVAL
    ):
        raise ObservationNotAvailable("Index fields do not belong to this completed clock slot.")
    return (
        observed
        if observed is not None and observed.close.is_finite() and observed.close > 0
        else None
    )


def _previous_vix(
    feed: ExecutionFeed, calendar: Model2Calendar, cycle: WeeklyCycle, at: datetime
) -> Decimal | None:
    previous = calendar.previous_session(cycle.entry_session)
    if previous.kind != "NORMAL":
        return None
    end = market_at(previous.session_date, 15, 30)
    while end >= market_at(previous.session_date, 15, 5):
        observed = _index(feed, "india_vix", end, at)
        if observed is not None:
            return observed.close
        end -= BAR_INTERVAL
    return None


def _near_vix(feed: ExecutionFeed, desired: datetime, at: datetime) -> CompletedObservation | None:
    # Exact, then nearest earlier, then later only if already completed. Ties
    # prefer the earlier observation; never cross a session boundary.
    for end in (desired, desired - BAR_INTERVAL, desired + BAR_INTERVAL):
        if end.date() != at.date() or end.time() < time(9, 20) or end > at:
            continue
        observed = _index(feed, "india_vix", end, at)
        if observed is not None:
            return observed
    return None


def _vix_emergency(
    feed: ExecutionFeed, at: datetime
) -> tuple[bool, bool, tuple[CompletedObservation, ...]]:
    reference_at = at - 6 * BAR_INTERVAL
    if reference_at.time() < time(9, 20):
        return False, False, ()
    current = _near_vix(feed, at, at)
    previous = _near_vix(feed, reference_at, at)
    observations = tuple(item for item in (previous, current) if item is not None)
    if current is None or previous is None:
        return False, True, observations
    return current.close >= previous.close * Decimal("1.15"), False, observations


def _halts(feed: ExecutionFeed, at: datetime) -> tuple[HaltObservation, ...]:
    records = feed.halts(as_of=at)
    if any(item.available_at > at or item.starts_at > at for item in records):
        raise ObservationNotAvailable("Halt evidence is unavailable at this clock time.")
    return records


def choose_exit_reason(reasons: tuple[str, ...]) -> str | None:
    return next(
        (
            reason
            for reason in ("EMERGENCY", "STOP_GAP", "STOP", "TIME", "TARGET")
            if reason in reasons
        ),
        None,
    )


def _trigger(pending: ExitTrigger | None, reason: str | None, at: datetime) -> ExitTrigger | None:
    if reason is None:
        return pending
    if pending is None:
        return ExitTrigger(reason, at, reason, at)
    chosen = choose_exit_reason((pending.reason, reason))
    assert chosen is not None
    if chosen == pending.reason:
        return pending
    tags = pending.tags
    if chosen == "STOP_GAP" and pending.original_reason == "TARGET":
        tags = tuple(dict.fromkeys((*tags, "PENDING_TARGET")))
    return ExitTrigger(chosen, at, pending.original_reason, pending.original_at, tags)


def _completed_valuation(
    feed: ExecutionFeed, entry: EntryEstimate, at: datetime
) -> tuple[CloseValuation | None, tuple[CompletedObservation, ...], tuple[str, ...]]:
    observations = []
    missing = []
    for leg in entry.legs:
        observed = feed.option_close(leg.contract, at, as_of=at)
        if observed is not None and (
            observed.available_at > at
            or observed.interval_end != at
            or observed.interval_start != at - BAR_INTERVAL
        ):
            raise ObservationNotAvailable(
                "Option fields do not belong to this completed clock slot."
            )
        if (
            observed is None
            or not observed.close.is_finite()
            or observed.close <= 0
            or observed.volume is None
            or observed.volume <= 0
        ):
            missing.append(leg.contract.contract_id)
        else:
            observations.append(observed)
    valuation = (
        None
        if len(observations) != 4
        else value_liquidation(entry, tuple(item.close for item in observations))
    )
    for name in ("spot", "india_vix"):
        index = _index(feed, name, at, at)
        if index is None:
            missing.append(name)
        else:
            observations.append(index)
    return valuation, tuple(observations), tuple(missing)


def _cycle_events(
    request: ReplayRequest,
    feed: ExecutionFeed,
    calendar: Model2Calendar,
    cycle: WeeklyCycle,
    risk: RiskState,
    policy: ScenarioPolicy,
    cancelled: Callable[[], bool],
) -> Iterator[ClockEvent]:
    decision_at = market_at(cycle.entry_session, 15, 20)
    if cancelled():
        yield ClockEvent(0, decision_at, cycle, "CANCELLED", "ENTRY_PENDING", risk_state=risk)
        return
    spot = _index(feed, "spot", decision_at, decision_at)
    vix = _index(feed, "india_vix", decision_at, decision_at)
    if spot is None or vix is None:
        result = CycleResult(cycle, "SKIPPED", "NO_VALID_ENTRY_INPUT", decision_at)
        yield ClockEvent(
            0,
            decision_at,
            cycle,
            "CYCLE_FINALIZED",
            "SKIPPED",
            result.reason,
            result=result,
            risk_state=risk,
        )
        return
    selected = select_condor(cycle, spot.close, vix.close, request.parameters, feed)
    if selected.selection is None:
        result = CycleResult(cycle, "SKIPPED", selected.reason or "MISSING_CONTRACT", decision_at)
        missing = tuple(
            f"{item.role}:{item.strike}:{item.option_type}" for item in selected.missing
        )
        yield ClockEvent(
            0,
            decision_at,
            cycle,
            "CYCLE_FINALIZED",
            "SKIPPED",
            result.reason,
            missing=missing,
            result=result,
            risk_state=risk,
        )
        return
    selection = selected.selection
    previous_vix = (
        _previous_vix(feed, calendar, cycle, decision_at)
        if request.parameters.vix_state_filter_enabled
        else None
    )
    entry = None
    for entry_at in (decision_at, decision_at + BAR_INTERVAL):
        if cancelled():
            yield ClockEvent(0, entry_at, cycle, "CANCELLED", "ENTRY_PENDING", risk_state=risk)
            return
        quotes = collect_opening_quotes(feed, calendar, selection, entry_at)
        admission = evaluate_entry(
            selection,
            quotes,
            request.parameters,
            request.starting_capital,
            previous_vix=previous_vix,
        )
        yield ClockEvent(
            0,
            entry_at,
            cycle,
            "ENTRY_DECISION",
            "ENTRY_PENDING",
            admission.reason,
            entry=admission.estimate,
            quotes=quotes,
            missing=admission.invalid_contract_ids,
            observations=(spot, vix),
        )
        if admission.accepted:
            entry = admission.estimate
            assert entry is not None
            yield ClockEvent(
                0, entry_at, cycle, "ENTRY_OPENED", "OPEN", entry=entry, risk_state=risk
            )
            break
        if not admission.retryable or entry_at.time() == time(15, 25):
            result = CycleResult(cycle, "SKIPPED", admission.reason or "NO_VALID_ENTRY", entry_at)
            yield ClockEvent(
                0,
                entry_at,
                cycle,
                "CYCLE_FINALIZED",
                "SKIPPED",
                result.reason,
                result=result,
                risk_state=risk,
            )
            return
    assert entry is not None
    pending: ExitTrigger | None = None
    attempts = 0
    missing_count = 0
    flags: set[str] = set()
    gap_checked = False
    for day in (cycle.entry_session, cycle.expiry):
        session = calendar.session(day)
        assert session is not None
        openings = session.opening_times()
        for at in (*openings, market_at(day, 15, 30)):
            if at <= entry.execution_at:
                continue
            state = "EXIT_PENDING" if pending else "OPEN"
            if cancelled():
                yield ClockEvent(
                    0,
                    at,
                    cycle,
                    "CANCELLED",
                    state,
                    entry=entry,
                    trigger=pending,
                    risk_state=risk,
                    attempt=attempts,
                    consecutive_missing=missing_count,
                )
                return
            reasons = []
            vix_observed: tuple[CompletedObservation, ...] = ()
            if at.time() == time(9, 15):
                yield ClockEvent(
                    0,
                    at,
                    cycle,
                    "SESSION_STARTED",
                    state,
                    entry=entry,
                    trigger=pending,
                    consecutive_missing=missing_count,
                    attempt=attempts,
                )
            else:
                valuation, observed, missing = _completed_valuation(feed, entry, at)
                missing_count = missing_count + 1 if missing else 0
                if missing:
                    flags.add("MONITORING_GAP")
                spike, vix_gap, vix_observed = _vix_emergency(feed, at)
                if vix_gap:
                    yield ClockEvent(
                        0, at, cycle, "VIX_GAP", state, entry=entry, observations=vix_observed
                    )
                yield ClockEvent(
                    0,
                    at,
                    cycle,
                    "COMPLETED_CANDLE",
                    state,
                    entry=entry,
                    valuation=valuation,
                    observations=observed,
                    missing=missing,
                    consecutive_missing=missing_count,
                )
                if spike or missing_count > 2:
                    reasons.append("EMERGENCY")
                if valuation is not None:
                    if valuation.stop_reached:
                        reasons.append("STOP")
                    if valuation.target_reached:
                        reasons.append("TARGET")
            halts = _halts(feed, at)
            if halts:
                reasons.append("EMERGENCY")
            if "EMERGENCY" in reasons:
                yield ClockEvent(
                    0,
                    at,
                    cycle,
                    "EMERGENCY_EVIDENCE",
                    state,
                    entry=entry,
                    observations=vix_observed,
                    halt_observations=halts,
                    consecutive_missing=missing_count,
                )
            if day == cycle.expiry and at.time() >= time(14, 30):
                reasons.append("TIME")
            updated = _trigger(pending, choose_exit_reason(tuple(reasons)), at)
            if updated != pending:
                pending = updated
                yield ClockEvent(
                    0,
                    at,
                    cycle,
                    "EXIT_TRIGGERED",
                    "EXIT_PENDING",
                    pending.reason if pending else None,
                    entry=entry,
                    trigger=pending,
                )
            # 15:30 is a completed close, never an executable open.
            if at.time() == time(15, 30):
                if day == cycle.entry_session and pending is not None:
                    yield ClockEvent(
                        0,
                        at,
                        cycle,
                        "OVERNIGHT_PENDING",
                        "EXIT_PENDING",
                        pending.reason,
                        entry=entry,
                        trigger=pending,
                        attempt=attempts,
                        consecutive_missing=missing_count,
                    )
                continue
            # At the first usable expiry opening, value the gap before the
            # candle closes, even when a different exit was already pending.
            if day == cycle.expiry and (not gap_checked or pending is not None):
                quotes = collect_opening_quotes(feed, calendar, selection, at)
                invalid = invalid_opening_contracts(selection, quotes)
                if not invalid and not gap_checked:
                    gap_checked = True
                    opening_value = value_liquidation(
                        entry,
                        tuple(
                            item.opening.price for item in quotes.legs if item.opening is not None
                        ),
                    )
                    if opening_value.stop_reached:
                        updated = _trigger(pending, "STOP_GAP", at)
                        if updated != pending:
                            pending = updated
                            yield ClockEvent(
                                0,
                                at,
                                cycle,
                                "EXIT_TRIGGERED",
                                "EXIT_PENDING",
                                "STOP_GAP",
                                entry=entry,
                                trigger=pending,
                                quotes=quotes,
                                valuation=opening_value,
                            )
            if pending is None:
                continue
            quotes = collect_opening_quotes(feed, calendar, selection, at)
            invalid = invalid_opening_contracts(selection, quotes)
            attempts += 1
            yield ClockEvent(
                0,
                at,
                cycle,
                "EXIT_ATTEMPT",
                "EXIT_PENDING",
                pending.reason,
                entry=entry,
                trigger=pending,
                quotes=quotes,
                missing=invalid,
                attempt=attempts,
            )
            if invalid:
                flags.add("EXIT_DATA_GAP")
                continue
            if attempts > 3:
                flags.add("LATE_FILL")
            value = value_liquidation(
                entry, tuple(item.opening.price for item in quotes.legs if item.opening is not None)
            )
            risk, ledger = record_priced_result(risk, at, value.net_pnl_inr)
            result = CycleResult(
                cycle,
                "CLOSED",
                pending.reason,
                at,
                entry,
                pending,
                value,
                ledger,
                tuple(sorted(flags)),
                attempts,
                exit_at=at,
            )
            yield ClockEvent(
                0,
                at,
                cycle,
                "CYCLE_FINALIZED",
                "CLOSED",
                result.reason,
                entry=entry,
                trigger=pending,
                quotes=quotes,
                valuation=value,
                result=result,
                risk_state=risk,
            )
            return
    at = request.settlement_check(cycle.expiry)
    if cancelled():
        yield ClockEvent(
            0,
            at,
            cycle,
            "CANCELLED",
            "EXIT_PENDING",
            entry=entry,
            trigger=pending,
            risk_state=risk,
            attempt=attempts,
            consecutive_missing=missing_count,
        )
        return
    observed_settlement = feed.settlement(cycle.expiry, as_of=at)
    flags.add("DATA_INCOMPLETE")
    if observed_settlement is not None:
        value = value_settlement(entry, observed_settlement, as_of=at)
        risk, ledger = record_priced_result(
            risk, market_at(cycle.expiry, 15, 30), value.net_pnl_inr
        )
        result = CycleResult(
            cycle,
            "SETTLED",
            "OFFICIAL_SETTLEMENT",
            at,
            entry,
            pending,
            value,
            ledger,
            tuple(sorted(flags)),
            attempts,
            settlement=observed_settlement,
            exit_at=market_at(cycle.expiry, 15, 30),
        )
    else:
        assert entry.maximum_loss_inr is not None
        risk, ledger = record_excluded_result(risk, entry.maximum_loss_inr, policy)
        result = CycleResult(
            cycle,
            "EXCLUDED",
            "NO_OFFICIAL_SETTLEMENT",
            at,
            entry,
            pending,
            None,
            ledger,
            tuple(sorted(flags)),
            attempts,
        )
    yield ClockEvent(
        0,
        at,
        cycle,
        "CYCLE_FINALIZED",
        result.state,
        result.reason,
        entry=entry,
        trigger=pending,
        valuation=result.valuation,
        settlement_observation=observed_settlement,
        result=result,
        risk_state=risk,
    )


def iter_replay(
    request: ReplayRequest,
    feed: ExecutionFeed,
    calendar: Model2Calendar,
    policy: ScenarioPolicy,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> Iterator[ClockEvent]:
    """Stream evidence in market order. Cancellation retains open state without a fill."""
    if policy not in POLICIES:
        raise ValueError("Unknown scenario policy.")
    cycles = request.validate_calendar(calendar)
    risk = RiskState(request.starting_capital)
    visited = skipped = opened = priced = settled = excluded = sequence = 0
    priced_net = assumed_net = Decimal("0")
    scope: Literal["FULL_REQUESTED_RANGE", "HALTED_AT_EXCLUSION", "CANCELLED"] = (
        "FULL_REQUESTED_RANGE"
    )
    previous_at = effective_end = None
    for cycle in cycles:
        decision_at = market_at(cycle.entry_session, 15, 20)
        if cancelled():
            sequence += 1
            effective_end = decision_at
            yield ClockEvent(
                sequence, decision_at, cycle, "CANCELLED", "SCHEDULED", risk_state=risk
            )
            scope = "CANCELLED"
            break
        # Boundary cycles can precede the requested start but do not trade.
        decision = prepare_cycle(risk, cycle)
        risk = decision.state
        visited += 1
        if decision.allowed:
            events = _cycle_events(request, feed, calendar, cycle, risk, policy, cancelled)
        else:
            result = CycleResult(cycle, "SKIPPED", decision.reason or "SKIPPED", decision_at)
            events = iter(
                (
                    ClockEvent(
                        0,
                        decision_at,
                        cycle,
                        "CYCLE_FINALIZED",
                        "SKIPPED",
                        result.reason,
                        result=result,
                        risk_state=risk,
                    ),
                )
            )
        for event in events:
            if previous_at is not None and event.at < previous_at:
                raise ValueError("Clock evidence must be chronological.")
            sequence += 1
            previous_at = effective_end = event.at
            if event.kind == "ENTRY_OPENED":
                opened += 1
            if event.risk_state is not None:
                risk = event.risk_state
            if event.result is not None:
                result = event.result
                skipped += result.state == "SKIPPED"
                excluded += result.state == "EXCLUDED"
                settled += result.state == "SETTLED"
                if result.valuation is not None:
                    priced += 1
                    priced_net += result.valuation.net_pnl_inr
                if result.risk_entry is not None and result.risk_entry.source.startswith("ASSUMED"):
                    assert result.risk_entry.risk_amount is not None
                    assumed_net += result.risk_entry.risk_amount
            yield replace(event, sequence=sequence)
            if event.kind == "CANCELLED":
                scope = "CANCELLED"
        if scope == "CANCELLED":
            break
        if risk.halted:
            scope = "HALTED_AT_EXCLUSION"
            break
    summary = RunSummary(
        len(cycles),
        visited,
        skipped,
        opened,
        priced,
        settled,
        excluded,
        priced_net,
        assumed_net,
        assess_reliability(opened, excluded),
        scope,
        effective_end,
        risk,
    )
    yield ClockEvent(
        sequence + 1,
        effective_end or market_at(request.start, 9, 15),
        None,
        "RUN_FINISHED",
        scope,
        risk_state=risk,
        summary=summary,
    )
