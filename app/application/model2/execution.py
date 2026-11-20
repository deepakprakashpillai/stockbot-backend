"""Lease-fenced, bounded evidence writes and atomic four-leg finalization."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.jobs import enqueue_job
from app.application.lease_heartbeat import preparation_heartbeat
from app.application.model2.serialization import CYCLE, REQUEST, SUMMARY, event_json
from app.core.config import Settings
from app.db.models.backtests import BacktestRun, EquityPoint, RunEvent, RunMetrics, Trade, TradeLeg
from app.db.models.model2 import (
    BacktestCycle,
    BacktestRiskEntry,
    ResearchScenarioGroup,
    ResearchScenarioMember,
)
from app.db.repositories.backtests import BacktestRepository
from app.domain.model2.approved_specification import approved_specification_digest
from app.domain.model2.clock import iter_replay
from app.domain.model2.execution import (
    ALGORITHM_KEY,
    ENGINE_VERSION,
    POLICIES,
    ReplayRequest,
    RunSummary,
    ScenarioPolicy,
)
from app.domain.model2.models import LegRole
from app.infrastructure.datasets.storage import DatasetStorage
from app.infrastructure.market_data.model2_feed import Model2BundleFeed
from app.infrastructure.realtime import NullRunEventPublisher, RunEventPublisher

logger = logging.getLogger(__name__)
MODE = "OPTION_PORTFOLIO_SIMULATION"


class LostAttempt(RuntimeError):
    pass


class Model2ExecutionService:
    def __init__(
        self,
        session: Session,
        storage: DatasetStorage,
        settings: Settings,
        publisher: RunEventPublisher | None = None,
    ):
        self.session, self.storage, self.settings = session, storage, settings
        self.runs = BacktestRepository(session)
        self.publisher = publisher or NullRunEventPublisher()

    def _commit(self, event: RunEvent | None = None) -> None:
        self.session.commit()
        if event is not None:
            try:
                self.publisher.publish(event.run_id, event.sequence)
            except Exception:
                logger.warning("Model 2 notification deferred; durable evidence remains available")

    def fence(self, run_id: UUID, owner: str, attempt_id: UUID) -> BacktestRun:
        membership = self.session.get(ResearchScenarioMember, run_id)
        if membership is not None:
            self.session.scalar(
                select(ResearchScenarioGroup)
                .where(ResearchScenarioGroup.id == membership.group_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        run = self.runs.get_run(run_id, lock=True)
        if (
            run is None
            or run.lease_owner != owner
            or run.active_attempt_id != attempt_id
            or not run.lease_expires_at
            or run.lease_expires_at <= datetime.now(UTC)
            or run.status != "RUNNING"
        ):
            raise LostAttempt("This attempt no longer owns the run.")
        return run

    def execute(self, run_id: UUID) -> dict[str, object]:
        owner = str(uuid4())
        member = self.session.get(ResearchScenarioMember, run_id)
        group = (
            self.session.scalar(
                select(ResearchScenarioGroup)
                .where(ResearchScenarioGroup.id == member.group_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if member
            else None
        )
        run = self.runs.claim_lease(run_id, owner)
        if run is None:
            self.session.rollback()
            return {"run_id": str(run_id), "status": "NOT_CLAIMED"}
        member = self.session.get(ResearchScenarioMember, run_id)
        if run.mode != MODE or member is None:
            self.session.rollback()
            raise ValueError("Model 2 execution requires scenario membership.")
        if member.ordinal > 1:
            previous = self.session.scalar(
                select(BacktestRun)
                .join(ResearchScenarioMember, ResearchScenarioMember.run_id == BacktestRun.id)
                .where(
                    ResearchScenarioMember.group_id == member.group_id,
                    ResearchScenarioMember.ordinal == member.ordinal - 1,
                )
            )
            if previous is None or previous.status != "COMPLETED":
                self.session.rollback()
                return {"run_id": str(run_id), "status": "WAITING_FOR_PREDECESSOR"}
        attempt = self.runs.start_attempt(run_id, owner)
        attempt_id = attempt.id
        run.status, run.started_at, run.progress = "RUNNING", datetime.now(UTC), 0
        assert group is not None
        group.status = "RUNNING"
        run.cancellation_requested = run.cancellation_requested or group.cancellation_requested
        started = self.runs.append_event(
            run_id, "BACKTEST_STARTED", {"mode": MODE, "attempt_id": str(attempt_id)}
        )
        self._commit(started)
        try:
            if (
                run.snapshot.get("engine_version") != ENGINE_VERSION
                or run.snapshot.get("specification_sha256") != approved_specification_digest()
            ):
                raise ValueError(
                    "The immutable Model 2 engine/specification snapshot is unsupported."
                )
            request = REQUEST.validate_python(run.snapshot["model2_request"])
            with preparation_heartbeat(self.settings, run_id, owner):
                feed = Model2BundleFeed(self.storage.resolve(run.snapshot["bundle_storage_key"]))
            if (
                feed.manifest.bundle_checksum != request.dataset_identity
                or feed.calendar.provenance.checksum != run.snapshot["calendar_sha256"]
            ):
                raise ValueError("Market/calendar identity changed after the run snapshot.")
            policy = cast(ScenarioPolicy, member.policy)
            if policy not in POLICIES:
                raise ValueError("Unknown Model 2 policy.")
            current: BacktestCycle | None = None
            summary: RunSummary | None = None
            visited_cycles = 0
            scheduled_cycles = len(request.validate_calendar(feed.calendar))
            cancellation = run.cancellation_requested

            def is_cancelled() -> bool:
                return cancellation

            for event in iter_replay(request, feed, feed.calendar, policy, cancelled=is_cancelled):
                locked = self.fence(run_id, owner, attempt_id)
                cancellation = locked.cancellation_requested
                self.runs.renew_lease(run_id, owner)
                payload = event_json(event)
                self.runs.append_journal(
                    run_id,
                    attempt_id,
                    timestamp=event.at,
                    event_type=event.kind,
                    payload=payload,
                    position_state=event.state,
                    flush=False,
                )
                if event.cycle is not None:
                    if current is None or current.expiry != event.cycle.expiry:
                        current = BacktestCycle(
                            run_id=run_id,
                            attempt_id=attempt_id,
                            expiry=event.cycle.expiry,
                            entry_date=event.cycle.entry_session,
                            state=event.state
                            if event.state in {"ENTRY_PENDING", "OPEN", "EXIT_PENDING"}
                            else "ENTRY_PENDING",
                            last_sequence=event.sequence,
                            payload={},
                        )
                        self.session.add(current)
                    current.last_sequence = event.sequence
                    if event.kind != "CANCELLED":
                        current.state = event.result.state if event.result else event.state
                    current.reason = event.reason
                    current.payload = payload
                    if event.result and event.result.risk_entry:
                        self.session.flush()
                        ledger = event.result.risk_entry
                        self.session.add(
                            BacktestRiskEntry(
                                cycle_id=current.id,
                                run_id=run_id,
                                attempt_id=attempt_id,
                                booking_date=ledger.booking_date,
                                source=ledger.source,
                                risk_amount=ledger.risk_amount,
                                priced_performance_amount=ledger.priced_performance_amount,
                                counters=payload.get("risk_state") or {},
                            )
                        )
                if event.summary is not None:
                    summary = event.summary
                if event.kind == "CYCLE_FINALIZED":
                    visited_cycles += 1
                important = event.kind in {
                    "ENTRY_OPENED",
                    "OVERNIGHT_PENDING",
                    "CYCLE_FINALIZED",
                    "CANCELLED",
                    "RUN_FINISHED",
                }
                if important or event.sequence % 25 == 0:
                    run.progress = min(
                        99,
                        int(100 * visited_cycles / max(scheduled_cycles, 1)),
                    )
                    notice = self.runs.append_event(
                        run_id,
                        "BACKTEST_PROGRESS",
                        {
                            "mode": MODE,
                            "attempt_id": str(attempt_id),
                            "clock_sequence": event.sequence,
                            "market_time": event.at.isoformat(),
                        },
                    )
                    self.runs.append_event(
                        run_id,
                        "BACKTEST_PORTFOLIO_UPDATED",
                        {
                            "mode": MODE,
                            "group_id": str(member.group_id),
                            "attempt_id": str(attempt_id),
                            "clock_sequence": event.sequence,
                        },
                    )
                    self._commit(notice)
            if summary is None:
                raise ValueError("The Model 2 clock did not produce a terminal summary.")
            run = self.fence(run_id, owner, attempt_id)
            if summary.scope == "CANCELLED" or run.cancellation_requested:
                run.status = "CANCELLED"
            else:
                self.project(run, attempt_id, request)
                run.status = "COMPLETED"
            from app.domain.model2.performance import PricedOutcome, priced_statistics

            priced = tuple(
                PricedOutcome(
                    trade.exit_timestamp,
                    trade.gross_demo_units,
                    trade.costs_demo_units,
                    trade.net_demo_units,
                )
                for trade in self.session.scalars(
                    select(Trade)
                    .where(Trade.run_id == run_id)
                    .order_by(Trade.exit_timestamp, Trade.sequence)
                )
            )
            statistics = priced_statistics(
                priced, request.start, request.end, request.starting_capital
            )
            if (
                run.status == "COMPLETED"
                and sum((value.net for value in priced), Decimal(0)) != summary.priced_net_pnl
            ):
                raise ValueError("Saved priced trades do not reconcile with the clock summary.")
            summary_payload = SUMMARY.dump_python(summary, mode="json")
            self.session.add(
                RunMetrics(
                    run_id=run_id,
                    metrics={
                        "model2_summary": summary_payload,
                        "priced_statistics": statistics,
                        "total_trades": statistics["trade_count"],
                        "wins": statistics["wins"],
                        "losses": statistics["losses"],
                        "mode": MODE,
                        "unit": "INR",
                        "synthetic": False,
                        "financial_pnl_available": True,
                        "net_pnl_inr": str(summary.priced_net_pnl),
                        "trade_count": summary.priced_cycles,
                        "starting_capital_inr": str(request.starting_capital),
                        "executable_quotes": False,
                    },
                )
            )
            self.runs.finish_attempt(attempt_id, run.status)
            run.completed_at, run.progress = datetime.now(UTC), 100
            run.lease_owner = run.lease_expires_at = None
            self.handoff(run, member)
            terminal = self.runs.append_event(
                run_id,
                "BACKTEST_COMPLETED" if run.status == "COMPLETED" else "BACKTEST_CANCELLED",
                {"mode": MODE, "scope": summary.scope},
            )
            self._commit(terminal)
            return {"run_id": str(run_id), "status": run.status}
        except LostAttempt:
            self.session.rollback()
            return {"run_id": str(run_id), "status": "LOST_LEASE"}
        except Exception as exc:
            self.session.rollback()
            try:
                run = self.fence(run_id, owner, attempt_id)
            except LostAttempt:
                self.session.rollback()
                return {"run_id": str(run_id), "status": "LOST_LEASE"}
            run.status, run.error_code, run.error_message = (
                "FAILED",
                "MODEL2_EXECUTION_FAILED",
                str(exc),
            )
            run.completed_at = datetime.now(UTC)
            run.lease_owner = run.lease_expires_at = None
            self.runs.finish_attempt(attempt_id, "FAILED", str(exc))
            self.handoff(run, member)
            failed = self.runs.append_event(
                run_id, "BACKTEST_FAILED", {"mode": MODE, "code": run.error_code}
            )
            self._commit(failed)
            raise

    def project(self, run: BacktestRun, attempt_id: UUID, request: ReplayRequest) -> None:
        cumulative, peak = Decimal(0), request.starting_capital
        count = 0
        rows = self.session.scalars(
            select(BacktestCycle)
            .where(BacktestCycle.run_id == run.id, BacktestCycle.attempt_id == attempt_id)
            .order_by(BacktestCycle.expiry)
            .execution_options(yield_per=100)
        )
        for row in rows:
            raw = row.payload.get("result")
            if raw is None or row.state not in {"CLOSED", "SETTLED"}:
                continue
            cycle = CYCLE.validate_python(raw)
            entry, value = cycle.entry, cycle.valuation
            if (
                entry is None
                or value is None
                or cycle.exit_at is None
                or tuple(leg.role for leg in entry.legs) != tuple(LegRole)
                or len(value.exit_fill_prices) != 4
            ):
                raise ValueError(
                    "A priced Model 2 trade requires exactly four complete role/fill records."
                )
            count += 1
            trade = Trade(
                id=uuid4(),
                run_id=run.id,
                sequence=count,
                entry_timestamp=entry.execution_at,
                exit_timestamp=cycle.exit_at,
                exit_reason=cycle.reason,
                quantity=entry.selection.quantity,
                entry_reference=entry.credit_points,
                exit_reference=value.exit_debit_points,
                gross_demo_units=value.gross_pnl_inr,
                costs_demo_units=value.charges_inr,
                net_demo_units=value.net_pnl_inr,
                synthetic=False,
                unit="INR",
                financial_pnl_available=True,
                assumptions={
                    "algorithm_key": ALGORITHM_KEY,
                    "leg_count": 4,
                    "entry_reference_kind": "OPEN",
                    "exit_reference_kind": "INTRINSIC" if cycle.state == "SETTLED" else "OPEN",
                    "executable_quotes": False,
                    "cycle_id": str(row.id),
                },
            )
            self.session.add(trade)
            self.session.flush()
            for index, (leg, exit_price, pnl) in enumerate(
                zip(entry.legs, value.exit_fill_prices, value.leg_gross_pnl_inr, strict=True), 1
            ):
                self.session.add(
                    TradeLeg(
                        trade_id=trade.id,
                        leg_number=index,
                        name=leg.role.value,
                        entry_reference=leg.fill_price,
                        exit_reference=exit_price,
                        demo_units=pnl,
                        synthetic=False,
                        unit="INR",
                        financial_pnl_available=True,
                        role=leg.role.value,
                        option_type=leg.contract.option_type,
                        strike=leg.contract.strike,
                        expiry=leg.contract.expiry,
                        contract_id=leg.contract.contract_id,
                        side=leg.side.value,
                        quantity=leg.quantity,
                        lot_size=leg.contract.lot_size,
                        tick_size=leg.contract.tick_size,
                        pricing_evidence={
                            "entry_reference": str(leg.reference_price),
                            "entry_at": entry.execution_at.isoformat(),
                            "exit_at": cycle.exit_at.isoformat(),
                            "kind": trade.assumptions["exit_reference_kind"],
                        },
                    )
                )
            row.trade_id = trade.id
            cumulative += value.net_pnl_inr
            equity = request.starting_capital + cumulative
            peak = max(peak, equity)
            self.session.add(
                EquityPoint(
                    run_id=run.id,
                    sequence=count,
                    timestamp=cycle.exit_at,
                    equity_demo_units=equity,
                    drawdown_demo_units=peak - equity,
                    synthetic=False,
                    unit="INR",
                    financial_pnl_available=True,
                )
            )
            if count % 100 == 0:
                self.session.flush()
        self.session.flush()

    def handoff(self, run: BacktestRun, member: ResearchScenarioMember) -> None:
        group = self.session.scalar(
            select(ResearchScenarioGroup)
            .where(ResearchScenarioGroup.id == member.group_id)
            .with_for_update()
        )
        assert group is not None
        if run.status != "COMPLETED" or group.cancellation_requested:
            group.status = (
                "CANCELLED"
                if group.cancellation_requested or run.status == "CANCELLED"
                else "FAILED"
            )
            group.completed_at = datetime.now(UTC)
            for sibling in self.session.scalars(
                select(BacktestRun)
                .join(ResearchScenarioMember, ResearchScenarioMember.run_id == BacktestRun.id)
                .where(
                    ResearchScenarioMember.group_id == group.id,
                    ResearchScenarioMember.ordinal > member.ordinal,
                )
            ):
                if sibling.status == "PENDING":
                    sibling.status = "CANCELLED"
                    sibling.completed_at = datetime.now(UTC)
            return
        next_member = self.session.scalar(
            select(ResearchScenarioMember).where(
                ResearchScenarioMember.group_id == group.id,
                ResearchScenarioMember.ordinal == member.ordinal + 1,
            )
        )
        if next_member is None:
            group.status = "COMPLETED"
            group.completed_at = datetime.now(UTC)
        else:
            group.status = "RUNNING"
            enqueue_job(self.session, "BACKTEST", next_member.run_id, depends_on_run_id=run.id)
