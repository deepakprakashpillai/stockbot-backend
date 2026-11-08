"""Persist linked signal and option events in stable market-time order."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from app.db.repositories.backtests import BacktestRepository
from app.domain.backtests import SignalDecision


class MarketJournalWriter:
    def __init__(
        self,
        runs: BacktestRepository,
        run_id: UUID,
        attempt_id: UUID,
        commit_and_publish: Callable[..., None],
    ) -> None:
        self.runs = runs
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.commit_and_publish = commit_and_publish

    def _append_option(self, raw_event: dict[str, Any], *, flush: bool = True) -> Any:
        raw_payload = raw_event.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        signal_sequence = payload.pop("signal_sequence", None)
        trade_sequence = payload.pop("trade_sequence", None)
        position_state = payload.pop("position_state", None)
        return self.runs.append_journal(
            self.run_id,
            self.attempt_id,
            timestamp=cast(datetime, raw_event["timestamp"]),
            event_type=str(raw_event["event_type"]),
            position_state=str(position_state) if position_state else None,
            signal_sequence=int(signal_sequence) if signal_sequence is not None else None,
            trade_sequence=int(trade_sequence) if trade_sequence is not None else None,
            payload=payload,
            flush=flush,
        )

    def _update_event(self, journal: Any) -> Any:
        return self.runs.append_event(
            self.run_id,
            "BACKTEST_JOURNAL_UPDATED",
            {
                "attempt_id": str(self.attempt_id),
                "journal_sequence": journal.sequence,
                "journal_event_type": journal.event_type,
                "position_state": journal.position_state,
            },
        )

    def append_legacy_option(self, raw_event: dict[str, Any]) -> None:
        journal = self._append_option(raw_event)
        self.commit_and_publish(self._update_event(journal))

    def persist_session(
        self,
        raw_events: list[dict[str, Any]],
        signals: list[tuple[int, SignalDecision]],
    ) -> None:
        entries: list[tuple[datetime, int, int, object]] = [
            (decision.timestamp, 0, offset, (sequence, decision))
            for offset, (sequence, decision) in enumerate(signals)
        ]
        entries.extend(
            (cast(datetime, event["timestamp"]), 1, offset, event)
            for offset, event in enumerate(raw_events)
        )
        updates: list[object] = []
        for timestamp, kind, _offset, value in sorted(entries, key=lambda item: item[:3]):
            if kind == 0:
                sequence, decision = cast(tuple[int, SignalDecision], value)
                self.runs.append_journal(
                    self.run_id,
                    self.attempt_id,
                    timestamp=timestamp,
                    event_type="SIGNAL_EVALUATED",
                    position_state=None,
                    signal_sequence=sequence,
                    payload={
                        "decision": decision.decision,
                        "rejection_reason": decision.rejection_reason,
                        "window": decision.window,
                        "score": decision.score,
                        "inputs": decision.inputs,
                    },
                    flush=False,
                )
                continue
            updates.append(
                self._update_event(self._append_option(cast(dict[str, Any], value), flush=False))
            )
        signals.clear()
        self.commit_and_publish(*updates)
