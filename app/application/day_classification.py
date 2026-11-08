"""Small per-session summaries for final backtest classifications."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from app.domain.backtests import SignalDecision


@dataclass
class DayDecisionSummary:
    evaluation_count: int = 0
    accepted_count: int = 0
    rejection_reasons: Counter[str] = field(default_factory=Counter)

    def record(self, decision: SignalDecision) -> None:
        self.evaluation_count += 1
        if decision.decision == "ACCEPTED":
            self.accepted_count += 1
        if decision.rejection_reason:
            self.rejection_reasons[decision.rejection_reason] += 1


def record_day_decision(
    summaries: dict[date, DayDecisionSummary], decision: SignalDecision
) -> None:
    summaries.setdefault(decision.session_date, DayDecisionSummary()).record(decision)
