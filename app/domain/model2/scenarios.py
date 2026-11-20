"""Sequential independent counterfactual replays, never adjusted finished totals."""

from __future__ import annotations

from collections.abc import Callable

from app.domain.model2.calendar import Model2Calendar
from app.domain.model2.clock import iter_replay
from app.domain.model2.execution import (
    POLICIES,
    ExecutionFeed,
    ReplayRequest,
    ScenarioPolicy,
    ScenarioResult,
)


def run_scenario(
    request: ReplayRequest,
    feed: ExecutionFeed,
    calendar: Model2Calendar,
    policy: ScenarioPolicy,
    *,
    keep_events: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
) -> ScenarioResult:
    """Convenience collector. Production writers can consume iter_replay directly."""
    cycles = []
    events = []
    summary = None
    for event in iter_replay(request, feed, calendar, policy, cancelled=cancelled):
        if keep_events:
            events.append(event)
        if event.result is not None:
            cycles.append(event.result)
        if event.summary is not None:
            summary = event.summary
    assert summary is not None
    return ScenarioResult(
        request,
        policy,
        calendar.version,
        calendar.provenance.checksum,
        tuple(cycles),
        summary,
        tuple(events),
    )


def run_scenarios(
    request: ReplayRequest,
    feed: ExecutionFeed,
    calendar: Model2Calendar,
    *,
    keep_events: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[ScenarioResult, ...]:
    """Each child starts with fresh counters and the same immutable inputs."""
    results = []
    for policy in POLICIES:
        result = run_scenario(
            request, feed, calendar, policy, keep_events=keep_events, cancelled=cancelled
        )
        results.append(result)
        if result.summary.scope == "CANCELLED":
            break
    return tuple(results)
