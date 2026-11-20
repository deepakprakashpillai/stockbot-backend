import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from persistence_fixture import prepared_case
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.application.backtests import BacktestService
from app.application.model2.execution import Model2ExecutionService
from app.db.models.backtests import (
    BacktestRun,
    ExecutionAttempt,
    JournalEvent,
    RunMetrics,
    Trade,
    TradeLeg,
)
from app.db.models.jobs import JobDispatch
from app.db.models.model2 import BacktestCycle, BacktestRiskEntry, ResearchScenarioGroup


def test_three_children_persist_once_and_handoff_in_same_transaction(
    postgres_factory, bundle_factory
):
    storage, settings, group_id, run_ids = prepared_case(postgres_factory, bundle_factory)
    with postgres_factory() as session:
        service = BacktestService(session, storage, settings)
        assert service.execute(run_ids[1])["status"] == "WAITING_FOR_PREDECESSOR"
        for run_id in run_ids:
            assert service.execute(run_id)["status"] == "COMPLETED"
            assert service.execute(run_id)["status"] == "NOT_CLAIMED"
        assert session.get(ResearchScenarioGroup, group_id).status == "COMPLETED"
        assert session.scalar(select(func.count()).select_from(BacktestCycle)) == 3
        assert session.scalar(select(func.count()).select_from(BacktestRiskEntry)) == 3
        assert session.scalar(select(func.count()).select_from(Trade)) == 3
        assert session.scalar(select(func.count()).select_from(TradeLeg)) == 12
        assert session.scalar(select(func.count()).select_from(JobDispatch)) == 2
        for run_id in run_ids:
            trade = session.scalar(select(Trade).where(Trade.run_id == run_id))
            assert trade.net_demo_units == Decimal("-400")
            assert len(trade.legs) == 4
            assert {leg.role for leg in trade.legs} == {
                "SHORT_CALL",
                "LONG_CALL",
                "SHORT_PUT",
                "LONG_PUT",
            }
            assert all(leg.quantity == leg.lot_size == 65 for leg in trade.legs)
            assert {leg.expiry.isoformat() for leg in trade.legs} == {"2026-01-06"}
            assert (
                session.get(RunMetrics, run_id).metrics["model2_summary"]["priced_net_pnl"]
                == "-400.00"
            )
            risk = session.scalar(
                select(BacktestRiskEntry).where(BacktestRiskEntry.run_id == run_id)
            )
            assert risk.risk_amount == risk.priced_performance_amount == Decimal("-400")


def test_stale_attempt_cannot_finalize_or_handoff(postgres_factory, bundle_factory, monkeypatch):
    storage, settings, group_id, run_ids = prepared_case(postgres_factory, bundle_factory)
    original = Model2ExecutionService._commit
    stolen = False

    def steal(self, event=None):
        nonlocal stolen
        original(self, event)
        if event and event.event_type == "BACKTEST_PROGRESS" and not stolen:
            stolen = True
            with postgres_factory() as replacement:
                replacement.execute(
                    update(BacktestRun)
                    .where(BacktestRun.id == run_ids[0])
                    .values(
                        lease_owner="replacement",
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                    )
                )
                replacement.commit()

    monkeypatch.setattr(Model2ExecutionService, "_commit", steal)
    with postgres_factory() as session:
        assert (
            BacktestService(session, storage, settings).execute(run_ids[0])["status"]
            == "LOST_LEASE"
        )
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        assert session.scalar(select(func.count()).select_from(JobDispatch)) == 0
        assert session.get(ResearchScenarioGroup, group_id).status == "RUNNING"
        assert session.scalar(select(func.count()).select_from(BacktestCycle)) == 1


def install_guards(session):
    path = Path("migrations/versions/20261117_16_model2_cycles_and_scenario_groups.py")
    spec = importlib.util.spec_from_file_location("model2_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    session.execute(text(module.GUARDS_SQL))
    session.commit()


def test_database_guards_preserve_group_inputs_and_four_leg_cardinality(
    postgres_factory, bundle_factory
):
    storage, settings, group_id, run_ids = prepared_case(postgres_factory, bundle_factory)
    with postgres_factory() as session:
        install_guards(session)
        with pytest.raises(IntegrityError):
            session.execute(
                update(ResearchScenarioGroup)
                .where(ResearchScenarioGroup.id == group_id)
                .values(snapshot={"changed": True})
            )
        session.rollback()
        assert (
            BacktestService(session, storage, settings).execute(run_ids[0])["status"] == "COMPLETED"
        )
        trade = session.scalar(select(Trade).where(Trade.run_id == run_ids[0]))
        leg_id = trade.legs[0].id
        session.delete(session.get(TradeLeg, leg_id))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.scalar(select(func.count()).select_from(TradeLeg)) == 4


@pytest.mark.parametrize("crash_time", ["started", "overnight"])
def test_process_death_replays_into_new_attempt_without_duplicate_results(
    postgres_factory, bundle_factory, crash_time
):
    storage, settings, group_id, run_ids = prepared_case(
        postgres_factory, bundle_factory, pending_target=True
    )
    with postgres_factory() as session:
        schema = session.scalar(select(func.current_schema()))
    child = """
import os,sys
from uuid import UUID
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.application.backtests import BacktestService
from app.application.model2.execution import Model2ExecutionService
from app.infrastructure.datasets.storage import LocalDatasetStorage
original=Model2ExecutionService._commit

def crash(self,event=None):
    original(self,event)
    if event and ((sys.argv[2]=='started' and event.event_type=='BACKTEST_STARTED') or (sys.argv[2]=='overnight' and event.event_type=='BACKTEST_PROGRESS' and '15:30:00' in event.payload.get('market_time',''))):
        os._exit(83)
Model2ExecutionService._commit=crash
settings=get_settings()
with create_session_factory(settings)() as session:
    BacktestService(session,LocalDatasetStorage(settings.storage_root),settings).execute(UUID(sys.argv[1]))
"""
    env = os.environ.copy()
    env.update(
        DATABASE_URL=os.environ["STOCKBOT_TEST_DATABASE_URL"],
        STORAGE_ROOT=str(storage.root),
        PGOPTIONS=f"-c search_path={schema}",
    )
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(run_ids[0]), crash_time],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert crashed.returncode == 83, crashed.stderr
    with postgres_factory() as session:
        run = session.get(BacktestRun, run_ids[0])
        previous_attempt = run.active_attempt_id
        assert run.status == "RUNNING"
        if crash_time == "overnight":
            cycle = session.scalar(
                select(BacktestCycle).where(BacktestCycle.attempt_id == previous_attempt)
            )
            assert cycle.state == "EXIT_PENDING"
            assert cycle.payload["trigger"]["original_reason"] == "TARGET"
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        assert (
            BacktestService(session, storage, settings).execute(run_ids[0])["status"] == "COMPLETED"
        )
        assert session.scalar(select(func.count()).select_from(Trade)) == 1
        assert session.scalar(select(func.count()).select_from(TradeLeg)) == 4
        assert session.scalar(select(func.count()).select_from(JobDispatch)) == 1
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.run_id == run_ids[0])
                .order_by(ExecutionAttempt.attempt_number)
            )
        )
        assert [attempt.status for attempt in attempts] == ["INTERRUPTED", "COMPLETED"]
        if crash_time == "overnight":
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(JournalEvent)
                    .where(JournalEvent.attempt_id == previous_attempt)
                )
                > 0
            )
            assert session.scalar(select(func.count()).select_from(BacktestCycle)) == 2


def test_cancellation_retains_open_evidence_and_cancels_future_children(
    postgres_factory, bundle_factory, monkeypatch
):
    storage, settings, group_id, run_ids = prepared_case(
        postgres_factory, bundle_factory, pending_target=True
    )
    original = Model2ExecutionService._commit

    def cancel_after_entry(self, event=None):
        original(self, event)
        if event and event.event_type == "BACKTEST_PROGRESS":
            with postgres_factory() as control:
                control.execute(
                    update(BacktestRun)
                    .where(BacktestRun.id == run_ids[0])
                    .values(cancellation_requested=True)
                )
                control.commit()

    monkeypatch.setattr(Model2ExecutionService, "_commit", cancel_after_entry)
    with postgres_factory() as session:
        assert (
            BacktestService(session, storage, settings).execute(run_ids[0])["status"] == "CANCELLED"
        )
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        assert session.scalar(select(func.count()).select_from(BacktestRiskEntry)) == 0
        assert session.scalar(select(func.count()).select_from(JobDispatch)) == 0
        cycle = session.scalar(select(BacktestCycle))
        assert cycle.payload["kind"] == "CANCELLED"
        assert cycle.payload["entry"] is not None
        assert [session.get(BacktestRun, run_id).status for run_id in run_ids] == ["CANCELLED"] * 3
        assert session.get(ResearchScenarioGroup, group_id).status == "CANCELLED"


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_dispatcher_recovers_expired_lease_and_preserves_cancellation_evidence(
    postgres_factory, bundle_factory, monkeypatch, cancel_requested
):
    from app.application.model2.recovery import recover_expired

    class ProcessKilled(BaseException):
        pass

    storage, settings, group_id, run_ids = prepared_case(
        postgres_factory, bundle_factory, pending_target=True
    )
    original = Model2ExecutionService._commit

    def crash(self, event=None):
        original(self, event)
        if event and event.event_type == "BACKTEST_PROGRESS":
            raise ProcessKilled()

    monkeypatch.setattr(Model2ExecutionService, "_commit", crash)
    with postgres_factory() as session:
        with pytest.raises(ProcessKilled):
            BacktestService(session, storage, settings).execute(run_ids[0])
    with postgres_factory() as session:
        run = session.get(BacktestRun, run_ids[0])
        attempt_id = run.active_attempt_id
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        run.cancellation_requested = cancel_requested
        session.commit()
        assert recover_expired(session) == 1
        session.commit()
        assert session.scalar(select(func.count()).select_from(Trade)) == 0
        assert session.scalar(select(func.count()).select_from(BacktestCycle)) == 1
        if cancel_requested:
            assert session.get(BacktestRun, run_ids[0]).status == "CANCELLED"
            assert session.get(BacktestRun, run_ids[0]).active_attempt_id == attempt_id
            assert session.get(ExecutionAttempt, attempt_id).status == "CANCELLED"
            assert session.get(ResearchScenarioGroup, group_id).status == "CANCELLED"
            assert session.scalar(select(func.count()).select_from(JobDispatch)) == 0
        else:
            assert session.get(BacktestRun, run_ids[0]).status == "PENDING"
            assert session.scalar(select(JobDispatch)).status == "PENDING"
            monkeypatch.setattr(Model2ExecutionService, "_commit", original)
            assert (
                BacktestService(session, storage, settings).execute(run_ids[0])["status"]
                == "COMPLETED"
            )
            assert session.scalar(select(func.count()).select_from(Trade)) == 1
            assert session.get(ExecutionAttempt, attempt_id).status == "INTERRUPTED"


def test_cycle_cannot_reference_another_runs_attempt(postgres_factory, bundle_factory):
    storage, settings, group_id, run_ids = prepared_case(postgres_factory, bundle_factory)
    with postgres_factory() as session:
        attempt = ExecutionAttempt(
            run_id=run_ids[0],
            attempt_number=1,
            status="RUNNING",
            worker_owner="fixture",
            legacy=False,
            metadata_snapshot={},
        )
        session.add(attempt)
        session.flush()
        session.add(
            BacktestCycle(
                run_id=run_ids[1],
                attempt_id=attempt.id,
                expiry=datetime(2026, 1, 6).date(),
                entry_date=datetime(2026, 1, 5).date(),
                state="OPEN",
                last_sequence=1,
                payload={},
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
