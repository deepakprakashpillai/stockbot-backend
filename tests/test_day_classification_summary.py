"""Per-session evidence must survive checkpointing without retaining all signals."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.application.backtests import BacktestService
from app.application.day_classification import DayDecisionSummary, record_day_decision


def test_summary_preserves_counts_and_first_seen_reason_tie() -> None:
    day = date(2025, 7, 21)
    summaries: dict[date, DayDecisionSummary] = {}
    for decision, reason in [
        ("REJECTED", "RSI_GATE_FAILED"),
        ("REJECTED", "VIX_GATE_FAILED"),
        ("ACCEPTED", None),
        ("REJECTED", "RSI_GATE_FAILED"),
        ("REJECTED", "VIX_GATE_FAILED"),
    ]:
        record_day_decision(
            summaries,
            SimpleNamespace(session_date=day, decision=decision, rejection_reason=reason),
        )
    summary = summaries[day]
    assert summary.evaluation_count == 5
    assert summary.accepted_count == 1
    assert dict(summary.rejection_reasons) == {
        "RSI_GATE_FAILED": 2,
        "VIX_GATE_FAILED": 2,
    }
    assert summary.rejection_reasons.most_common(1)[0][0] == "RSI_GATE_FAILED"


def test_day_classification_uses_summary_and_coverage_without_signal_list() -> None:
    day = date(2025, 7, 21)
    excluded_day = date(2025, 7, 22)
    run = SimpleNamespace(
        id=uuid4(),
        snapshot={
            "coverage": {
                "included_sessions": [day.isoformat()],
                "excluded_sessions": [
                    {
                        "session_date": excluded_day.isoformat(),
                        "classification": "DATA_GAP",
                        "reason_code": "OPTION_CANDLE_MISSING",
                    }
                ],
            }
        },
    )
    service = object.__new__(BacktestService)
    service.session = MagicMock()
    service.session.scalars.return_value = []
    service.runs = MagicMock()
    summary = DayDecisionSummary()
    summary.record(
        SimpleNamespace(
            session_date=day,
            timestamp=datetime(2025, 7, 21, 9, 20),
            decision="ACCEPTED",
            rejection_reason=None,
        )
    )
    BacktestService._persist_day_classifications(service, run, uuid4(), {day: summary})
    values = service.runs.replace_day_classifications.call_args.args[2]
    assert values[0]["session_date"] == excluded_day
    assert values[0]["included"] is False
    assert values[1]["classification"] == "SIGNAL_REJECTED"
    assert values[1]["evidence"] == {
        "evaluation_count": 1,
        "accepted_candidate_count": 1,
        "rejections_by_reason": {},
    }
