"""Backtest worker orchestration and fenced result persistence."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from app.application.analytics import persist_completed_run_analytics
from app.application.backtest_preparation import prepare_run_inputs
from app.application.checkpoint_writer import persist_demo_trades, persist_signal_checkpoint
from app.application.day_classification import DayDecisionSummary, record_day_decision
from app.application.journal_writer import MarketJournalWriter
from app.application.lease_heartbeat import preparation_heartbeat
from app.application.option_result_writer import persist_option_results
from app.db.models.backtests import (
    BacktestRun,
    EquityPoint,
    RunMetrics,
    SignalEvaluation,
    Trade,
)
from app.domain.algorithms import BullCallSpreadSignalParameters
from app.domain.backtests import (
    ENGINE_VERSION,
    TERMINAL_BACKTEST_STATUSES,
    BacktestStatus,
    SignalDecision,
    evaluate_chronologically,
)
from app.domain.demo_execution import (
    DemoExecutionAssumptions,
    PlatformDemoExecutionState,
)
from app.domain.option_spread_execution import (
    OptionExecutionCancelled,
    OptionSpreadAssumptions,
)
from app.domain.research_pipeline import run_chronological_option_research

if TYPE_CHECKING:
    from app.application.backtests import BacktestService

logger = logging.getLogger(__name__)


def execute_backtest(self: BacktestService, run_id: UUID) -> dict[str, object]:
    owner = str(uuid4())
    run = self.runs.claim_lease(run_id, owner)
    if run is None:
        return {"run_id": str(run_id), "status": "NOT_CLAIMED"}
    if BacktestStatus(run.status) in TERMINAL_BACKTEST_STATUSES:
        self.session.commit()
        return {"run_id": str(run_id), "status": run.status}
    if run.cancellation_requested:
        run.status, run.progress, run.completed_at = (
            BacktestStatus.CANCELLED.value,
            100,
            datetime.now(UTC),
        )
        run.lease_owner = None
        run.lease_expires_at = None
        cancelled = self.runs.append_event(
            run.id, "BACKTEST_CANCELLED", {"mode": getattr(run, "mode", "SIGNAL_ONLY")}
        )
        self._commit_and_publish(cancelled)
        return {"run_id": str(run_id), "status": run.status}
    attempts_supported = hasattr(self.runs, "start_attempt")
    if attempts_supported:
        attempt = self.runs.start_attempt(run.id, owner)
        attempt_id = attempt.id
        attempt_number = attempt.attempt_number
    else:  # Compatibility for lightweight repository adapters used by old deployments/tests.
        attempt_id = uuid4()
        attempt_number = 1
    run.status, run.started_at = BacktestStatus.RUNNING.value, datetime.now(UTC)
    started = self.runs.append_event(
        run.id,
        "BACKTEST_STARTED",
        {
            "engine_version": (
                run.snapshot.get("engine_version")
                if isinstance(run.snapshot, dict)
                else ENGINE_VERSION
            ),
            "mode": getattr(run, "mode", "SIGNAL_ONLY"),
            "attempt_id": str(attempt_id),
            "attempt_number": attempt_number,
        },
    )
    self._commit_and_publish(started)
    try:
        with preparation_heartbeat(self.settings, run.id, owner):
            prepared = prepare_run_inputs(
                run, self.datasets.get_version(run.dataset_version_id), self.storage
            )
        dataset = prepared.dataset
        algorithm = prepared.algorithm
        parameters = prepared.parameters
        snapshot_algorithm = prepared.snapshot_algorithm
        included_sessions = prepared.included_sessions
        signal_candles = prepared.signal_candles
        derivatives_provider = prepared.derivatives_provider
        checkpoint_run: BacktestRun = run
        market_ordered_journal = (
            run.mode == "OPTION_SPREAD_SIMULATION"
            and run.snapshot.get("engine_version") == ENGINE_VERSION
        )
        day_summaries: dict[date, DayDecisionSummary] = {}
        pending_decisions: list[SignalDecision] = []
        session_journal_signals: list[tuple[int, SignalDecision]] = []
        persisted_trade_count = 0
        demo_state: PlatformDemoExecutionState | None = None
        if checkpoint_run.mode == "PLATFORM_DEMO":
            hold_candles = parameters.get("hold_candles")
            if not isinstance(hold_candles, int):
                raise ValueError("The demo holding interval snapshot is invalid.")
            if (
                run.snapshot.get("assumptions", {}).get("demo_execution")
                != DemoExecutionAssumptions().snapshot()
            ):
                raise ValueError("The demo execution assumptions snapshot is invalid.")
            demo_state = PlatformDemoExecutionState(
                DemoExecutionAssumptions(), hold_candles=hold_candles
            )
        # A lease-recovered worker restarts from immutable inputs.  Remove
        # incomplete checkpoint rows before it writes a deterministic replay.
        if attempts_supported and not self.runs.attempt_is_active(run.id, attempt_id, owner):
            raise RuntimeError(
                "The backtest worker lease or active attempt was lost during preparation."
            )
        # Completed checkpoint trades reference their entry signal. Remove
        # dependants before replacing deterministic signal rows on replay.
        self.session.query(Trade).filter(Trade.run_id == checkpoint_run.id).delete()
        self.session.query(EquityPoint).filter(EquityPoint.run_id == checkpoint_run.id).delete()
        self.session.query(SignalEvaluation).filter(
            SignalEvaluation.run_id == checkpoint_run.id
        ).delete()
        active_run_id = checkpoint_run.id
        processed = 0
        accepted_count = 0
        rejection_reasons: Counter[str] = Counter()
        last_market_timestamp: datetime | None = None

        def partial_metrics() -> dict[str, object]:
            metrics: dict[str, object] = {
                "mode": checkpoint_run.mode,
                "partial": True,
                "synthetic": checkpoint_run.mode == "PLATFORM_DEMO",
                "unit": "INR"
                if checkpoint_run.mode == "OPTION_SPREAD_SIMULATION"
                else ("DEMO_UNITS" if checkpoint_run.mode == "PLATFORM_DEMO" else None),
                "financial_pnl_available": (checkpoint_run.mode == "OPTION_SPREAD_SIMULATION"),
                "pricing_quality": "estimated_historical_ohlc"
                if checkpoint_run.mode == "OPTION_SPREAD_SIMULATION"
                else None,
                "executable_quotes": False,
                "pnl_available": False,
                "processed_candles": processed,
                "estimated_candles": dataset.row_count or 0,
                "last_market_timestamp": (
                    last_market_timestamp.isoformat() if last_market_timestamp else None
                ),
                "accepted_signals": accepted_count,
                "rejected_evaluations": processed - accepted_count,
                "model": parameters.get("score_model", algorithm.key),
            }
            if demo_state:
                metrics.update(demo_state.result().metrics())
            return metrics

        def persist_checkpoint() -> None:
            nonlocal pending_decisions, persisted_trade_count
            if not pending_decisions:
                return
            pending_start_sequence = processed - len(pending_decisions) + 1
            persist_signal_checkpoint(
                self.session,
                run_id=checkpoint_run.id,
                first_sequence=pending_start_sequence,
                decisions=pending_decisions,
                algorithm=algorithm,
                parameter_version_id=str(snapshot_algorithm["version_id"]),
                parameters=parameters,
            )
            events: list[object] = []
            if attempts_supported and not market_ordered_journal:
                for offset, item in enumerate(pending_decisions):
                    self.runs.append_journal(
                        checkpoint_run.id,
                        attempt_id,
                        timestamp=item.timestamp,
                        event_type="SIGNAL_EVALUATED",
                        position_state=None,
                        signal_sequence=pending_start_sequence + offset,
                        payload={
                            "decision": item.decision,
                            "rejection_reason": item.rejection_reason,
                            "window": item.window,
                            "score": item.score,
                            "inputs": item.inputs,
                        },
                    )
            if demo_state:
                persisted_trade_count, trade_events = persist_demo_trades(
                    self.session,
                    self.runs,
                    run_id=checkpoint_run.id,
                    state=demo_state,
                    persisted_count=persisted_trade_count,
                )
                events.extend(trade_events)
            total_rows = max(dataset.row_count or 0, 1)
            checkpoint_run.progress = min(99, int((processed / total_rows) * 100))
            metrics = partial_metrics()
            self.session.merge(RunMetrics(run_id=checkpoint_run.id, metrics=metrics))
            events.append(
                self.runs.append_event(
                    checkpoint_run.id,
                    "BACKTEST_PROGRESS",
                    {
                        "processed_candles": processed,
                        "estimated_candles": total_rows,
                        "percent": checkpoint_run.progress,
                        "last_market_timestamp": pending_decisions[-1].timestamp.isoformat(),
                        "accepted_signals": metrics["accepted_signals"],
                        "rejected_evaluations": metrics["rejected_evaluations"],
                        "completed_trades": metrics.get("total_trades", 0),
                        "wins": metrics.get("wins", 0),
                        "losses": metrics.get("losses", 0),
                        "equity_demo_units": (
                            str(demo_state.result().equity_points[-1].equity_demo_units)
                            if demo_state and demo_state.result().equity_points
                            else "0"
                        ),
                        "drawdown_demo_units": (
                            str(demo_state.result().equity_points[-1].drawdown_demo_units)
                            if demo_state and demo_state.result().equity_points
                            else "0"
                        ),
                        "mode": checkpoint_run.mode,
                        "synthetic": checkpoint_run.mode == "PLATFORM_DEMO",
                        "unit": "DEMO_UNITS" if checkpoint_run.mode == "PLATFORM_DEMO" else None,
                        "financial_pnl_available": False,
                    },
                )
            )
            self._commit_and_publish(*events)
            pending_decisions = []

        def cancellation_requested() -> bool:
            if processed % 100:
                return False
            current = self.runs.get_run(active_run_id, lock=True)
            if current is None or current.cancellation_requested:
                return True
            renewed = self.runs.renew_lease(active_run_id, owner)
            if renewed is None:
                raise RuntimeError("The backtest worker lease was lost.")
            return False

        def record_decision(decision: SignalDecision) -> None:
            nonlocal processed, accepted_count, last_market_timestamp
            if not checkpoint_run.start_date <= decision.session_date <= checkpoint_run.end_date:
                return
            if included_sessions is not None and decision.session_date not in included_sessions:
                return
            record_day_decision(day_summaries, decision)
            pending_decisions.append(decision)
            processed += 1
            if market_ordered_journal:
                session_journal_signals.append((processed, decision))
            last_market_timestamp = decision.timestamp
            if decision.decision == "ACCEPTED":
                accepted_count += 1
            if decision.rejection_reason:
                rejection_reasons[decision.rejection_reason] += 1
            if demo_state:
                demo_state.consume(decision)
            if processed % 100 == 0:
                persist_checkpoint()

        configured_algorithm = algorithm.configure(parameters)
        option_execution = None
        if run.mode == "OPTION_SPREAD_SIMULATION":
            if derivatives_provider is None:
                raise ValueError("The derivatives bundle was not initialized.")
            starting_capital = Decimal(str(run.starting_capital or "500000.00"))
            option_parameters = BullCallSpreadSignalParameters.model_validate(parameters)
            option_assumptions = OptionSpreadAssumptions.from_parameters(
                option_parameters, starting_capital=starting_capital
            )

            def option_execution_checkpoint() -> bool:
                current = self.runs.get_run(active_run_id, lock=True)
                if current is None:
                    raise RuntimeError("The backtest run disappeared during execution.")
                if current.cancellation_requested:
                    return True
                renewed = self.runs.renew_lease(active_run_id, owner)
                if renewed is None or (
                    attempts_supported
                    and not self.runs.attempt_is_active(active_run_id, attempt_id, owner)
                ):
                    raise RuntimeError("The backtest worker lease or active attempt was lost.")
                return False

            journal_writer = MarketJournalWriter(
                self.runs,
                active_run_id,
                attempt_id,
                self._commit_and_publish,
            )

            def persist_session_journal(raw_events: list[dict[str, Any]]) -> None:
                journal_writer.persist_session(raw_events, session_journal_signals)

            option_execution = run_chronological_option_research(
                signal_candles,
                configured_algorithm,
                derivatives_provider,
                option_assumptions,
                requested_start=run.start_date,
                requested_end=run.end_date,
                included_sessions=included_sessions,
                on_signal=record_decision,
                on_session_signals_complete=persist_checkpoint,
                on_session_events_complete=(
                    persist_session_journal if market_ordered_journal else None
                ),
                observer=None if market_ordered_journal else journal_writer.append_legacy_option,
                signal_should_cancel=cancellation_requested,
                execution_should_cancel=option_execution_checkpoint,
            )
        else:
            for decision in evaluate_chronologically(
                signal_candles,
                configured_algorithm,
                should_cancel=cancellation_requested,
            ):
                record_decision(decision)
        persist_checkpoint()
        run = self.runs.get_run(run.id, lock=True)
        assert run is not None
        if attempts_supported and not self.runs.attempt_is_active(run.id, attempt_id, owner):
            raise RuntimeError("The backtest worker lease or active attempt was lost.")
        if run.cancellation_requested:
            run.status = BacktestStatus.CANCELLED.value
            if attempts_supported:
                self.runs.finish_attempt(attempt_id, "CANCELLED")
            terminal_events = [
                self.runs.append_event(run.id, "BACKTEST_CANCELLED", {"mode": run.mode})
            ]
        else:
            terminal_events = []
            # Checkpoint signal identities remain stable through completion.
            metrics = {
                "mode": run.mode,
                "synthetic": run.mode == "PLATFORM_DEMO",
                "unit": "INR"
                if run.mode == "OPTION_SPREAD_SIMULATION"
                else ("DEMO_UNITS" if run.mode == "PLATFORM_DEMO" else None),
                "financial_pnl_available": run.mode == "OPTION_SPREAD_SIMULATION",
                "pricing_quality": "estimated_historical_ohlc"
                if run.mode == "OPTION_SPREAD_SIMULATION"
                else None,
                "executable_quotes": False,
                "processed_candles": processed,
                "last_market_timestamp": (
                    last_market_timestamp.isoformat() if last_market_timestamp else None
                ),
                "accepted_signals": accepted_count,
                "rejected_evaluations": processed - accepted_count,
                "rejections_by_reason": dict(rejection_reasons),
                "model": parameters.get("score_model", algorithm.key),
                "pnl_available": False,
            }
            if run.mode == "OPTION_SPREAD_SIMULATION":
                assert option_execution is not None
                persist_option_results(
                    self,
                    run,
                    option_execution,
                    option_assumptions,
                    algorithm,
                    snapshot_algorithm,
                    parameters,
                    market_ordered_journal,
                    accepted_count,
                    processed,
                    starting_capital,
                    metrics,
                    terminal_events,
                )
            if run.mode == "PLATFORM_DEMO" and demo_state is not None:
                assumptions = DemoExecutionAssumptions()
                snapshot_assumptions = cast(dict[str, object], run.snapshot.get("assumptions"))
                if snapshot_assumptions.get("demo_execution") != assumptions.snapshot():
                    raise ValueError("The demo execution assumptions snapshot is invalid.")
                metrics.update(demo_state.result().metrics())
            if attempts_supported and not self.runs.attempt_is_active(run.id, attempt_id, owner):
                raise RuntimeError("The backtest worker lease or active attempt was lost.")
            # Completed runs expose analytics only after their immutable trade
            # records, aggregates, and legacy metrics reconcile in one commit.
            flush = getattr(self.session, "flush", None)
            if callable(flush):
                flush()
                analytics_started = monotonic()
                try:
                    analytics = persist_completed_run_analytics(self.session, run, dataset)
                except Exception:
                    logger.exception(
                        "completed-run analytics calculation failed",
                        extra={"run_id": str(run.id)},
                    )
                    raise
                logger.info(
                    "completed-run analytics calculated",
                    extra={
                        "run_id": str(run.id),
                        "duration_ms": round((monotonic() - analytics_started) * 1000, 3),
                    },
                )
                metrics["analytics_schema_version"] = analytics.schema_version
            self.session.merge(RunMetrics(run_id=run.id, metrics=metrics))
            run.status = BacktestStatus.COMPLETED.value
            if attempts_supported:
                self._persist_day_classifications(run, attempt_id, day_summaries)
                self.runs.finish_attempt(attempt_id, "COMPLETED")
            terminal_events.append(self.runs.append_event(run.id, "BACKTEST_COMPLETED", metrics))
        run.progress, run.completed_at, run.lease_owner, run.lease_expires_at = (
            100,
            datetime.now(UTC),
            None,
            None,
        )
        self._commit_and_publish(*terminal_events)
        return {"run_id": str(run_id), "status": run.status}
    except OptionExecutionCancelled:
        self.session.rollback()
        reset_journal_sequences = getattr(self.runs, "reset_journal_sequence_cache", None)
        if callable(reset_journal_sequences):
            reset_journal_sequences()
        run = self.runs.get_run(run_id, lock=True)
        if (
            run
            and run.cancellation_requested
            and getattr(run, "active_attempt_id", attempt_id) in {None, attempt_id}
            and run.lease_owner == owner
        ):
            if attempts_supported:
                self.runs.finish_attempt(attempt_id, "CANCELLED")
            run.status = BacktestStatus.CANCELLED.value
            run.progress = min(run.progress, 99)
            run.completed_at = datetime.now(UTC)
            run.lease_owner = None
            run.lease_expires_at = None
            cancelled = self.runs.append_event(run.id, "BACKTEST_CANCELLED", {"mode": run.mode})
            self._commit_and_publish(cancelled)
        return {"run_id": str(run_id), "status": BacktestStatus.CANCELLED.value}
    except Exception as exc:
        self.session.rollback()
        reset_journal_sequences = getattr(self.runs, "reset_journal_sequence_cache", None)
        if callable(reset_journal_sequences):
            reset_journal_sequences()
        run = self.runs.get_run(run_id, lock=True)
        if (
            run
            and getattr(run, "active_attempt_id", attempt_id) in {None, attempt_id}
            and run.lease_owner == owner
        ):
            if attempts_supported:
                self.runs.finish_attempt(attempt_id, "FAILED", str(exc))
            (
                run.status,
                run.error_code,
                run.error_message,
                run.completed_at,
                run.lease_owner,
                run.lease_expires_at,
            ) = (
                BacktestStatus.FAILED.value,
                "BACKTEST_EXECUTION_FAILED",
                str(exc),
                datetime.now(UTC),
                None,
                None,
            )
            failed = self.runs.append_event(
                run.id,
                "BACKTEST_FAILED",
                {"code": run.error_code, "mode": getattr(run, "mode", "SIGNAL_ONLY")},
            )
            self._commit_and_publish(failed)
        raise
