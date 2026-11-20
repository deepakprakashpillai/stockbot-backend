from __future__ import annotations

from typing import Any, cast

from pydantic import TypeAdapter

from app.domain.model2.execution import ClockEvent, CycleResult, ReplayRequest, RunSummary

REQUEST = TypeAdapter(ReplayRequest)
EVENT = TypeAdapter(ClockEvent)
CYCLE = TypeAdapter(CycleResult)
SUMMARY = TypeAdapter(RunSummary)


def request_json(value: ReplayRequest) -> dict[str, Any]:
    return cast(dict[str, Any], REQUEST.dump_python(value, mode="json"))


def event_json(value: ClockEvent) -> dict[str, Any]:
    return cast(dict[str, Any], EVENT.dump_python(value, mode="json"))
