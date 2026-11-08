from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, String, and_, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import (
    BacktestExportJob,
    BacktestRun,
    EquityPoint,
    ExecutionAttempt,
    JournalEvent,
    RunAnalytics,
    RunAnalyticsPeriod,
    RunDayClassification,
    RunEvent,
    SignalEvaluation,
    Trade,
)
from app.db.models.datasets import DatasetVersion


@dataclass(frozen=True)
class BacktestRunFilters:
    run_query: str | None = None
    status: str | None = None
    dataset_id: UUID | None = None
    dataset_version_id: UUID | None = None
    algorithm_key: str | None = None
    algorithm_version_id: UUID | None = None
    mode: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


@dataclass(frozen=True)
class TradeListFilters:
    start: datetime | None = None
    end: datetime | None = None
    exit_from: datetime | None = None
    exit_to: datetime | None = None
    outcome: str | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class SignalListFilters:
    start: datetime | None = None
    end: datetime | None = None
    decision: str | None = None
    rejection_reason: str | None = None
    window: str | None = None


@dataclass(frozen=True)
class EventListFilters:
    start: datetime | None = None
    end: datetime | None = None
    event_type: str | None = None


@dataclass(frozen=True)
class EquityListFilters:
    start: datetime | None = None
    end: datetime | None = None


class BacktestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._journal_next_sequence: dict[UUID, int] = {}

    def reset_journal_sequence_cache(self) -> None:
        """Discard numbers reserved by a transaction that may have rolled back."""
        self._journal_next_sequence.clear()

    def get_run(self, run_id: UUID, *, lock: bool = False) -> BacktestRun | None:
        statement = (
            select(BacktestRun)
            .options(
                selectinload(BacktestRun.metrics),
                selectinload(BacktestRun.analytics),
            )
            .where(BacktestRun.id == run_id)
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement.with_for_update() if lock else statement)

    def get_idempotent(self, key: str) -> BacktestRun | None:
        return self.session.scalar(select(BacktestRun).where(BacktestRun.idempotency_key == key))

    @staticmethod
    def _apply_run_filters(
        statement: Select[tuple[BacktestRun]] | Select[tuple[int]],
        filters: BacktestRunFilters,
    ) -> Select[tuple[BacktestRun]] | Select[tuple[int]]:
        if filters.run_query:
            statement = statement.where(cast(BacktestRun.id, String).startswith(filters.run_query))
        if filters.status:
            statement = statement.where(BacktestRun.status == filters.status)
        if filters.dataset_version_id:
            statement = statement.where(
                BacktestRun.dataset_version_id == filters.dataset_version_id
            )
        if filters.dataset_id:
            statement = statement.join(
                DatasetVersion, DatasetVersion.id == BacktestRun.dataset_version_id
            ).where(DatasetVersion.dataset_id == filters.dataset_id)
        if filters.algorithm_version_id:
            statement = statement.where(
                BacktestRun.algorithm_version_id == filters.algorithm_version_id
            )
        if filters.algorithm_key:
            statement = statement.join(
                AlgorithmVersion, AlgorithmVersion.id == BacktestRun.algorithm_version_id
            ).where(AlgorithmVersion.algorithm_key == filters.algorithm_key)
        if filters.mode:
            statement = statement.where(BacktestRun.mode == filters.mode)
        if filters.start_date:
            statement = statement.where(BacktestRun.start_date >= filters.start_date)
        if filters.end_date:
            statement = statement.where(BacktestRun.end_date <= filters.end_date)
        if filters.created_from:
            statement = statement.where(BacktestRun.created_at >= filters.created_from)
        if filters.created_to:
            statement = statement.where(BacktestRun.created_at <= filters.created_to)
        return statement

    def list_runs(
        self,
        filters: BacktestRunFilters | str | None,
        cursor: UUID | None,
        limit: int,
    ) -> tuple[list[BacktestRun], UUID | None]:
        if isinstance(filters, str) or filters is None:
            filters = BacktestRunFilters(status=filters)
        statement = self._apply_run_filters(
            select(BacktestRun).options(
                selectinload(BacktestRun.metrics),
                selectinload(BacktestRun.analytics),
                selectinload(BacktestRun.dataset_version).selectinload(DatasetVersion.dataset),
                selectinload(BacktestRun.algorithm_version).selectinload(
                    AlgorithmVersion.algorithm
                ),
            ),
            filters,
        )
        statement = statement.order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc()).limit(
            limit + 1
        )
        if cursor:
            cursor_created_at = (
                select(BacktestRun.created_at).where(BacktestRun.id == cursor).scalar_subquery()
            )
            statement = statement.where(
                or_(
                    BacktestRun.created_at < cursor_created_at,
                    and_(
                        BacktestRun.created_at == cursor_created_at,
                        BacktestRun.id < cursor,
                    ),
                )
            )
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].id if len(values) > limit else None

    def history_runs(self, filters: BacktestRunFilters) -> list[BacktestRun]:
        statement = self._apply_run_filters(
            select(BacktestRun).options(
                selectinload(BacktestRun.metrics),
                selectinload(BacktestRun.analytics),
                selectinload(BacktestRun.dataset_version).selectinload(DatasetVersion.dataset),
                selectinload(BacktestRun.algorithm_version).selectinload(
                    AlgorithmVersion.algorithm
                ),
            ),
            filters,
        ).order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
        return list(self.session.scalars(statement))

    def count_runs(self, filters: BacktestRunFilters) -> int:
        statement = self._apply_run_filters(select(func.count()).select_from(BacktestRun), filters)
        return int(self.session.scalar(statement) or 0)

    def get_runs(self, run_ids: list[UUID]) -> list[BacktestRun]:
        if not run_ids:
            return []
        values = list(
            self.session.scalars(
                select(BacktestRun)
                .options(
                    selectinload(BacktestRun.metrics),
                    selectinload(BacktestRun.analytics),
                )
                .where(BacktestRun.id.in_(run_ids))
            )
        )
        indexed = {value.id: value for value in values}
        return [indexed[run_id] for run_id in run_ids if run_id in indexed]

    def count_signals(self, run_id: UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(SignalEvaluation)
                .where(SignalEvaluation.run_id == run_id)
            )
            or 0
        )

    def count_trades(self, run_id: UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(Trade).where(Trade.run_id == run_id)
            )
            or 0
        )

    def get_signal(self, run_id: UUID, signal_id: UUID) -> SignalEvaluation | None:
        return self.session.scalar(
            select(SignalEvaluation)
            .options(selectinload(SignalEvaluation.entry_trades))
            .where(SignalEvaluation.run_id == run_id, SignalEvaluation.id == signal_id)
        )

    def signal_neighbors(self, run_id: UUID, sequence: int) -> tuple[UUID | None, UUID | None]:
        previous = self.session.scalar(
            select(SignalEvaluation.id)
            .where(SignalEvaluation.run_id == run_id, SignalEvaluation.sequence < sequence)
            .order_by(SignalEvaluation.sequence.desc())
            .limit(1)
        )
        next_value = self.session.scalar(
            select(SignalEvaluation.id)
            .where(SignalEvaluation.run_id == run_id, SignalEvaluation.sequence > sequence)
            .order_by(SignalEvaluation.sequence)
            .limit(1)
        )
        return previous, next_value

    def get_signal_execution_rejection(
        self, run_id: UUID, attempt_id: UUID | None, signal_sequence: int
    ) -> JournalEvent | None:
        if attempt_id is None:
            return None
        return self.session.scalar(
            select(JournalEvent)
            .where(
                JournalEvent.run_id == run_id,
                JournalEvent.attempt_id == attempt_id,
                JournalEvent.signal_sequence == signal_sequence,
                JournalEvent.event_type == "ENTRY_REJECTED",
            )
            .order_by(JournalEvent.sequence)
            .limit(1)
        )

    def get_event(self, run_id: UUID, event_id: UUID) -> RunEvent | None:
        return self.session.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id == event_id)
        )

    def get_trade(self, run_id: UUID, trade_id: UUID) -> Trade | None:
        return self.session.scalar(
            select(Trade)
            .options(selectinload(Trade.legs))
            .where(Trade.run_id == run_id, Trade.id == trade_id)
        )

    def get_trade_by_sequence(self, run_id: UUID, sequence: int) -> Trade | None:
        return self.session.scalar(
            select(Trade).where(Trade.run_id == run_id, Trade.sequence == sequence)
        )

    def get_equity_point(self, run_id: UUID, point_id: UUID) -> EquityPoint | None:
        return self.session.scalar(
            select(EquityPoint).where(EquityPoint.run_id == run_id, EquityPoint.id == point_id)
        )

    def get_equity_point_by_sequence(self, run_id: UUID, sequence: int) -> EquityPoint | None:
        return self.session.scalar(
            select(EquityPoint).where(
                EquityPoint.run_id == run_id, EquityPoint.sequence == sequence
            )
        )

    def get_event_by_sequence(self, run_id: UUID, sequence: int) -> RunEvent | None:
        return self.session.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.sequence == sequence)
        )

    def list_signals(
        self,
        run_id: UUID,
        cursor: UUID | None,
        limit: int,
        *,
        after_sequence: int | None = None,
        filters: SignalListFilters | None = None,
    ) -> tuple[list[SignalEvaluation], UUID | None]:
        statement = (
            select(SignalEvaluation)
            .options(selectinload(SignalEvaluation.entry_trades))
            .where(SignalEvaluation.run_id == run_id)
            .order_by(SignalEvaluation.sequence)
            .limit(limit + 1)
        )
        filters = filters or SignalListFilters()
        if filters.start is not None:
            statement = statement.where(SignalEvaluation.timestamp >= filters.start)
        if filters.end is not None:
            statement = statement.where(SignalEvaluation.timestamp <= filters.end)
        if filters.decision is not None:
            statement = statement.where(SignalEvaluation.decision == filters.decision)
        if filters.rejection_reason is not None:
            statement = statement.where(
                SignalEvaluation.rejection_reason == filters.rejection_reason
            )
        if filters.window is not None:
            statement = statement.where(SignalEvaluation.window == filters.window)
        if after_sequence is not None:
            statement = statement.where(SignalEvaluation.sequence > after_sequence)
        elif cursor:
            cursor_timestamp = (
                select(SignalEvaluation.timestamp)
                .where(SignalEvaluation.id == cursor)
                .scalar_subquery()
            )
            statement = statement.where(
                or_(
                    SignalEvaluation.timestamp > cursor_timestamp,
                    and_(
                        SignalEvaluation.timestamp == cursor_timestamp,
                        SignalEvaluation.id > cursor,
                    ),
                )
            )
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].id if len(values) > limit else None

    def latest_signal_sequence(self, run_id: UUID) -> int:
        return int(
            self.session.scalar(
                select(func.max(SignalEvaluation.sequence)).where(SignalEvaluation.run_id == run_id)
            )
            or 0
        )

    def latest_trade_sequence(self, run_id: UUID) -> int:
        return int(
            self.session.scalar(select(func.max(Trade.sequence)).where(Trade.run_id == run_id)) or 0
        )

    def latest_equity_sequence(self, run_id: UUID) -> int:
        return int(
            self.session.scalar(
                select(func.max(EquityPoint.sequence)).where(EquityPoint.run_id == run_id)
            )
            or 0
        )

    def list_events(
        self,
        run_id: UUID,
        cursor: int | None,
        limit: int,
        *,
        filters: EventListFilters | None = None,
    ) -> tuple[list[RunEvent], int | None]:
        statement = (
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence)
            .limit(limit + 1)
        )
        filters = filters or EventListFilters()
        if filters.start is not None:
            statement = statement.where(RunEvent.created_at >= filters.start)
        if filters.end is not None:
            statement = statement.where(RunEvent.created_at <= filters.end)
        if filters.event_type is not None:
            statement = statement.where(RunEvent.event_type == filters.event_type)
        if cursor is not None:
            statement = statement.where(RunEvent.sequence > cursor)
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].sequence if len(values) > limit else None

    def list_trades(
        self,
        run_id: UUID,
        cursor: int | None,
        limit: int,
        *,
        filters: TradeListFilters | None = None,
        sort: str = "SEQUENCE_ASC",
    ) -> tuple[list[Trade], int | None]:
        statement = (
            select(Trade)
            .options(
                selectinload(Trade.legs),
                selectinload(Trade.entry_signal),
            )
            .where(Trade.run_id == run_id)
        )
        filters = filters or TradeListFilters()
        if filters.start is not None:
            statement = statement.where(Trade.exit_timestamp >= filters.start)
        if filters.end is not None:
            statement = statement.where(Trade.entry_timestamp <= filters.end)
        if filters.exit_from is not None:
            statement = statement.where(Trade.exit_timestamp >= filters.exit_from)
        if filters.exit_to is not None:
            statement = statement.where(Trade.exit_timestamp < filters.exit_to)
        if filters.outcome == "WIN":
            statement = statement.where(Trade.net_demo_units > 0)
        elif filters.outcome == "LOSS":
            statement = statement.where(Trade.net_demo_units < 0)
        elif filters.outcome == "BREAKEVEN":
            statement = statement.where(Trade.net_demo_units == 0)
        if filters.exit_reason is not None:
            statement = statement.where(Trade.exit_reason == filters.exit_reason)

        exit_ascending = sort == "EXIT_TIMESTAMP_ASC"
        exit_descending = sort == "EXIT_TIMESTAMP_DESC"
        sequence_descending = sort == "SEQUENCE_DESC"
        if exit_ascending or exit_descending:
            cursor_timestamp = (
                select(Trade.exit_timestamp)
                .where(Trade.run_id == run_id, Trade.sequence == cursor)
                .scalar_subquery()
            )
            if cursor is not None:
                statement = statement.where(
                    or_(
                        Trade.exit_timestamp > cursor_timestamp
                        if exit_ascending
                        else Trade.exit_timestamp < cursor_timestamp,
                        and_(
                            Trade.exit_timestamp == cursor_timestamp,
                            Trade.sequence > cursor if exit_ascending else Trade.sequence < cursor,
                        ),
                    )
                )
            statement = statement.order_by(
                Trade.exit_timestamp.asc() if exit_ascending else Trade.exit_timestamp.desc(),
                Trade.sequence.asc() if exit_ascending else Trade.sequence.desc(),
            )
        else:
            if cursor is not None:
                statement = statement.where(
                    Trade.sequence < cursor if sequence_descending else Trade.sequence > cursor
                )
            statement = statement.order_by(
                Trade.sequence.desc() if sequence_descending else Trade.sequence.asc()
            )
        statement = statement.limit(limit + 1)
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].sequence if len(values) > limit else None

    def list_trades_chronological(self, run_id: UUID) -> list[Trade]:
        return list(
            self.session.scalars(
                select(Trade)
                .options(selectinload(Trade.entry_signal))
                .where(Trade.run_id == run_id)
                .order_by(Trade.exit_timestamp, Trade.sequence)
            )
        )

    def list_equity_points(
        self,
        run_id: UUID,
        cursor: int | None,
        limit: int,
        *,
        filters: EquityListFilters | None = None,
    ) -> tuple[list[EquityPoint], int | None]:
        statement = (
            select(EquityPoint)
            .where(EquityPoint.run_id == run_id)
            .order_by(EquityPoint.sequence)
            .limit(limit + 1)
        )
        filters = filters or EquityListFilters()
        if filters.start is not None:
            statement = statement.where(EquityPoint.timestamp >= filters.start)
        if filters.end is not None:
            statement = statement.where(EquityPoint.timestamp <= filters.end)
        if cursor is not None:
            statement = statement.where(EquityPoint.sequence > cursor)
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].sequence if len(values) > limit else None

    def list_comparison_equity(
        self, run_id: UUID, *, start: datetime | None, end: datetime | None
    ) -> list[EquityPoint]:
        statement = select(EquityPoint).where(EquityPoint.run_id == run_id)
        if start is not None:
            statement = statement.where(EquityPoint.timestamp >= start)
        if end is not None:
            statement = statement.where(EquityPoint.timestamp <= end)
        return list(
            self.session.scalars(statement.order_by(EquityPoint.timestamp, EquityPoint.sequence))
        )

    def list_comparison_trades(
        self, run_id: UUID, *, start: datetime | None, end: datetime | None
    ) -> list[Trade]:
        statement = select(Trade).where(Trade.run_id == run_id)
        if start is not None:
            statement = statement.where(Trade.exit_timestamp >= start)
        if end is not None:
            statement = statement.where(Trade.exit_timestamp <= end)
        return list(self.session.scalars(statement.order_by(Trade.exit_timestamp, Trade.sequence)))

    def list_comparison_signals(
        self, run_id: UUID, *, start: datetime | None, end: datetime | None
    ) -> list[SignalEvaluation]:
        statement = select(SignalEvaluation).where(SignalEvaluation.run_id == run_id)
        if start is not None:
            statement = statement.where(SignalEvaluation.timestamp >= start)
        if end is not None:
            statement = statement.where(SignalEvaluation.timestamp <= end)
        return list(
            self.session.scalars(
                statement.order_by(SignalEvaluation.timestamp, SignalEvaluation.sequence)
            )
        )

    def get_analytics(self, run_id: UUID) -> RunAnalytics | None:
        return self.session.get(RunAnalytics, run_id)

    def list_analytics_periods(
        self,
        run_id: UUID,
        granularity: str,
        cursor: date | None,
        limit: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_zero_periods: bool = True,
        sort: str = "PERIOD_ASC",
    ) -> tuple[list[RunAnalyticsPeriod], date | None, int]:
        conditions = [
            RunAnalyticsPeriod.run_id == run_id,
            RunAnalyticsPeriod.granularity == granularity,
        ]
        if start_date is not None:
            conditions.append(RunAnalyticsPeriod.period_end >= start_date)
        if end_date is not None:
            conditions.append(RunAnalyticsPeriod.period_start <= end_date)
        if not include_zero_periods:
            conditions.append(RunAnalyticsPeriod.trade_count > 0)
        if cursor is not None:
            conditions.append(
                RunAnalyticsPeriod.period_start < cursor
                if sort == "PERIOD_DESC"
                else RunAnalyticsPeriod.period_start > cursor
            )
        count = int(
            self.session.scalar(
                select(func.count()).select_from(RunAnalyticsPeriod).where(*conditions)
            )
            or 0
        )
        statement = (
            select(RunAnalyticsPeriod)
            .where(*conditions)
            .order_by(
                RunAnalyticsPeriod.period_start.desc()
                if sort == "PERIOD_DESC"
                else RunAnalyticsPeriod.period_start.asc()
            )
            .limit(limit + 1)
        )
        values = list(self.session.scalars(statement))
        return (
            values[:limit],
            values[limit - 1].period_start if len(values) > limit else None,
            count,
        )

    def append_event(self, run_id: UUID, event_type: str, payload: dict[str, object]) -> RunEvent:
        sequence = (
            self.session.scalar(
                select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
            )
            or 0
        )
        event = RunEvent(
            run_id=run_id, sequence=sequence + 1, event_type=event_type, payload=payload
        )
        self.session.add(event)
        # A run can append several events in one transaction (for example trade-created
        # events followed by completion). Flush makes each sequence visible to the next
        # MAX(sequence) query while preserving the surrounding transaction.
        flush = getattr(self.session, "flush", None)
        if callable(flush):
            flush()
        return event

    def latest_event_sequence(self, run_id: UUID) -> int:
        return (
            self.session.scalar(
                select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
            )
            or 0
        )

    def claim_lease(self, run_id: UUID, owner: str, seconds: int = 60) -> BacktestRun | None:
        run = self.get_run(run_id, lock=True)
        if run is None or run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return None
        now = datetime.now(UTC)
        if run.lease_expires_at and run.lease_expires_at > now and run.lease_owner != owner:
            return None
        run.lease_owner = owner
        run.lease_expires_at = now + timedelta(seconds=seconds)
        # Sessions deliberately disable autoflush. Persist the lease before a
        # second populate-existing lock query (start_attempt) can reload the row
        # and erase these in-memory values.
        self.session.flush()
        return run

    def renew_lease(self, run_id: UUID, owner: str, seconds: int = 60) -> BacktestRun | None:
        run = self.get_run(run_id, lock=True)
        if run is None or run.lease_owner != owner:
            return None
        run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=seconds)
        self.session.flush()
        return run

    def start_attempt(self, run_id: UUID, owner: str) -> ExecutionAttempt:
        run = self.get_run(run_id, lock=True)
        if run is None or run.lease_owner != owner:
            raise RuntimeError("The worker no longer owns the run lease.")
        for previous in self.session.scalars(
            select(ExecutionAttempt).where(
                ExecutionAttempt.run_id == run_id,
                ExecutionAttempt.status == "RUNNING",
            )
        ):
            previous.status = "INTERRUPTED"
            previous.reason = "A recovery worker started a new deterministic attempt."
            previous.completed_at = datetime.now(UTC)
        number = (
            int(
                self.session.scalar(
                    select(func.max(ExecutionAttempt.attempt_number)).where(
                        ExecutionAttempt.run_id == run_id
                    )
                )
                or 0
            )
            + 1
        )
        attempt = ExecutionAttempt(
            run_id=run_id,
            attempt_number=number,
            status="RUNNING",
            worker_owner=owner,
            legacy=False,
            metadata_snapshot={"lease_owner": owner},
        )
        self.session.add(attempt)
        self.session.flush()
        run.active_attempt_id = attempt.id
        return attempt

    def attempt_is_active(self, run_id: UUID, attempt_id: UUID, owner: str) -> bool:
        return bool(
            self.session.scalar(
                select(func.count())
                .select_from(BacktestRun)
                .where(
                    BacktestRun.id == run_id,
                    BacktestRun.active_attempt_id == attempt_id,
                    BacktestRun.lease_owner == owner,
                    BacktestRun.lease_expires_at > datetime.now(UTC),
                )
            )
        )

    def finish_attempt(self, attempt_id: UUID, status: str, reason: str | None = None) -> None:
        attempt = self.session.get(ExecutionAttempt, attempt_id)
        if attempt is None:
            return
        attempt.status = status
        attempt.reason = reason
        attempt.completed_at = datetime.now(UTC)

    def list_attempts(self, run_id: UUID) -> list[ExecutionAttempt]:
        return list(
            self.session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.run_id == run_id)
                .order_by(ExecutionAttempt.attempt_number.desc())
            )
        )

    def append_journal(
        self,
        run_id: UUID,
        attempt_id: UUID,
        *,
        timestamp: datetime,
        event_type: str,
        payload: dict[str, object],
        position_state: str | None = None,
        signal_sequence: int | None = None,
        trade_sequence: int | None = None,
        flush: bool = True,
    ) -> JournalEvent:
        sequence = self._journal_next_sequence.get(attempt_id)
        if sequence is None:
            sequence = (
                int(
                    self.session.scalar(
                        select(func.max(JournalEvent.sequence)).where(
                            JournalEvent.attempt_id == attempt_id
                        )
                    )
                    or 0
                )
                + 1
            )
        event = JournalEvent(
            run_id=run_id,
            attempt_id=attempt_id,
            sequence=sequence,
            timestamp=timestamp,
            session_date=timestamp.date(),
            event_type=event_type,
            position_state=position_state,
            signal_sequence=signal_sequence,
            trade_sequence=trade_sequence,
            payload=payload,
        )
        self.session.add(event)
        if flush:
            self.session.flush()
        self._journal_next_sequence[attempt_id] = sequence + 1
        return event

    def list_journal(
        self, run_id: UUID, attempt_id: UUID, cursor: int | None, limit: int
    ) -> tuple[list[JournalEvent], int | None]:
        statement = (
            select(JournalEvent)
            .where(JournalEvent.run_id == run_id, JournalEvent.attempt_id == attempt_id)
            .order_by(JournalEvent.sequence)
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.where(JournalEvent.sequence > cursor)
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].sequence if len(values) > limit else None

    def list_journal_chronological(
        self, run_id: UUID, attempt_id: UUID, cursor: str | None, limit: int
    ) -> tuple[list[JournalEvent], str | None]:
        statement = select(JournalEvent).where(
            JournalEvent.run_id == run_id, JournalEvent.attempt_id == attempt_id
        )
        if cursor:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor).decode())
                if value["attempt"] != str(attempt_id):
                    raise ValueError("Cursor belongs to another attempt.")
                timestamp = datetime.fromisoformat(value["timestamp"])
                sequence = int(value["sequence"])
            except Exception as exc:
                raise ValueError("Invalid chronological journal cursor.") from exc
            statement = statement.where(
                or_(
                    JournalEvent.timestamp > timestamp,
                    and_(JournalEvent.timestamp == timestamp, JournalEvent.sequence > sequence),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(JournalEvent.timestamp, JournalEvent.sequence).limit(limit + 1)
            )
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "attempt": str(attempt_id),
                        "timestamp": last.timestamp.isoformat(),
                        "sequence": last.sequence,
                    }
                ).encode()
            ).decode()
        return rows[:limit], next_cursor

    def replace_day_classifications(
        self,
        run_id: UUID,
        attempt_id: UUID,
        values: list[dict[str, object]],
    ) -> None:
        for value in values:
            self.session.add(
                RunDayClassification(
                    run_id=run_id,
                    attempt_id=attempt_id,
                    session_date=value["session_date"],
                    classification=value["classification"],
                    included=bool(value["included"]),
                    primary_reason=value.get("primary_reason"),
                    evidence=value.get("evidence") or {},
                )
            )

    def list_day_classifications(
        self, run_id: UUID, attempt_id: UUID, cursor: date | None, limit: int
    ) -> tuple[list[RunDayClassification], date | None]:
        statement = (
            select(RunDayClassification)
            .where(
                RunDayClassification.run_id == run_id,
                RunDayClassification.attempt_id == attempt_id,
            )
            .order_by(RunDayClassification.session_date)
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.where(RunDayClassification.session_date > cursor)
        values = list(self.session.scalars(statement))
        return values[:limit], values[limit - 1].session_date if len(values) > limit else None

    def create_export_job(self, run: BacktestRun, *, format: str = "ZIP") -> BacktestExportJob:
        job = BacktestExportJob(
            run_id=run.id,
            attempt_id=run.active_attempt_id,
            status="PENDING",
            format=format,
            row_counts={},
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get_export_job(self, job_id: UUID, *, lock: bool = False) -> BacktestExportJob | None:
        statement = (
            select(BacktestExportJob)
            .where(BacktestExportJob.id == job_id)
            .execution_options(populate_existing=True)
        )
        return self.session.scalar(statement.with_for_update() if lock else statement)
