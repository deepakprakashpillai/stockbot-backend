from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.algorithms import AlgorithmVersion
    from app.db.models.datasets import DatasetVersion


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_backtest_runs_idempotency_key"),)

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    dataset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), index=True
    )
    algorithm_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("algorithm_versions.id", ondelete="RESTRICT"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False, default="SIGNAL_ONLY")
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellation_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_attempt_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True, index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signals: Mapped[list[SignalEvaluation]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    metrics: Mapped[RunMetrics | None] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    analytics: Mapped[RunAnalytics | None] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    analytics_periods: Mapped[list[RunAnalyticsPeriod]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    trades: Mapped[list[Trade]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="Trade.sequence",
    )
    equity_points: Mapped[list[EquityPoint]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="EquityPoint.sequence",
    )
    execution_attempts: Mapped[list[ExecutionAttempt]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ExecutionAttempt.attempt_number",
    )
    journal_events: Mapped[list[JournalEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    day_classifications: Mapped[list[RunDayClassification]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    dataset_version: Mapped[DatasetVersion] = relationship()
    algorithm_version: Mapped[AlgorithmVersion] = relationship()


class ExecutionAttempt(Base):
    __tablename__ = "backtest_execution_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_backtest_attempt_number"),
        CheckConstraint("attempt_number > 0", name="ck_backtest_attempt_number_positive"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="RUNNING", index=True)
    worker_owner: Mapped[str | None] = mapped_column(String(100))
    legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    metadata_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run: Mapped[BacktestRun] = relationship(back_populates="execution_attempts")
    journal_events: Mapped[list[JournalEvent]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )
    day_classifications: Mapped[list[RunDayClassification]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class JournalEvent(Base):
    __tablename__ = "backtest_journal_events"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_backtest_journal_attempt_sequence"),
        CheckConstraint("sequence > 0", name="ck_backtest_journal_sequence_positive"),
        Index("ix_backtest_journal_run_timestamp", "run_id", "timestamp"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_execution_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    position_state: Mapped[str | None] = mapped_column(String(30), index=True)
    signal_sequence: Mapped[int | None] = mapped_column(Integer)
    trade_sequence: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    run: Mapped[BacktestRun] = relationship(back_populates="journal_events")
    attempt: Mapped[ExecutionAttempt] = relationship(back_populates="journal_events")


class RunDayClassification(Base):
    __tablename__ = "backtest_day_classifications"
    __table_args__ = (
        UniqueConstraint("attempt_id", "session_date", name="uq_backtest_day_attempt_session"),
        Index("ix_backtest_day_run_session", "run_id", "session_date"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_execution_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    classification: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    primary_reason: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    run: Mapped[BacktestRun] = relationship(back_populates="day_classifications")
    attempt: Mapped[ExecutionAttempt] = relationship(back_populates="day_classifications")


class BacktestExportJob(Base):
    __tablename__ = "backtest_export_jobs"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("backtest_execution_attempts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING", index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    format: Mapped[str] = mapped_column(String(10), nullable=False, default="ZIP")
    artifact_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    row_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperimentPlan(Base):
    __tablename__ = "experiment_plans"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    baseline_algorithm_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("algorithm_versions.id", ondelete="RESTRICT"), nullable=False
    )
    dataset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    parameter_key: Mapped[str] = mapped_column(String(100), nullable=False)
    parameter_values: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    development_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    development_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    validation_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    validation_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT", index=True)
    validation_unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    selected_algorithm_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("algorithm_versions.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    __table_args__ = (
        UniqueConstraint(
            "experiment_plan_id", "phase", "variant_index", name="uq_experiment_variant_index"
        ),
        CheckConstraint(
            "variant_index >= 0 AND variant_index < 10", name="ck_experiment_variant_index"
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    experiment_plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiment_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    backtest_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    parameter_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="DEVELOPMENT")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    backtest_run: Mapped[BacktestRun] = relationship()

    @property
    def algorithm_version_id(self) -> UUID:
        return self.backtest_run.algorithm_version_id

    @property
    def run_status(self) -> str:
        return self.backtest_run.status

    @property
    def outcome_metrics(self) -> dict[str, Any]:
        return dict(self.backtest_run.metrics.metrics) if self.backtest_run.metrics else {}


class SignalEvaluation(Base):
    __tablename__ = "signal_evaluations"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_signal_evaluations_sequence"),
        UniqueConstraint("run_id", "id", name="uq_signal_evaluations_run_id_id"),
        Index("ix_signal_evaluations_run_timestamp", "run_id", "timestamp"),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(100), index=True)
    window: Mapped[str | None] = mapped_column(String(30))
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diagnostic: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    run: Mapped[BacktestRun] = relationship(back_populates="signals")
    entry_trades: Mapped[list[Trade]] = relationship(back_populates="entry_signal", viewonly=True)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
        Index("ix_run_events_run_created_at", "run_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    run: Mapped[BacktestRun] = relationship(back_populates="events")


class RunMetrics(Base):
    __tablename__ = "run_metrics"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    run: Mapped[BacktestRun] = relationship(back_populates="metrics")


class RunAnalytics(Base):
    __tablename__ = "run_analytics"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    calculator_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    run: Mapped[BacktestRun] = relationship(back_populates="analytics")


class RunAnalyticsPeriod(Base):
    __tablename__ = "run_analytics_periods"
    __table_args__ = (
        UniqueConstraint("run_id", "granularity", "period_start", name="uq_run_analytics_period"),
        CheckConstraint("granularity IN ('DAY', 'MONTH')", name="ck_run_analytics_granularity"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    granularity: Mapped[str] = mapped_column(String(10), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    eligible_session_count: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, nullable=False)
    breakevens: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    costs_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    net_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    cumulative_net_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    equity_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    local_max_drawdown_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    run_drawdown_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    run: Mapped[BacktestRun] = relationship(back_populates="analytics_periods")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_trades_run_sequence"),
        CheckConstraint("sequence > 0", name="ck_trades_positive_sequence"),
        CheckConstraint("quantity > 0", name="ck_trades_positive_quantity"),
        CheckConstraint("unit IN ('DEMO_UNITS', 'INR')", name="ck_trades_unit"),
        CheckConstraint(
            "(unit = 'DEMO_UNITS' AND synthetic AND NOT financial_pnl_available) OR (unit = 'INR' AND NOT synthetic AND financial_pnl_available)",
            name="ck_trades_mode_flags",
        ),
        ForeignKeyConstraint(
            ["run_id", "entry_signal_evaluation_id"],
            ["signal_evaluations.run_id", "signal_evaluations.id"],
            name="fk_trades_entry_signal_same_run",
        ),
        Index("ix_trades_run_entry_exit", "run_id", "entry_timestamp", "exit_timestamp"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_signal_evaluation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True, index=True
    )
    entry_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_reference: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exit_reference: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    gross_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    costs_demo_units: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal("0")
    )
    net_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="DEMO_UNITS")
    financial_pnl_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    run: Mapped[BacktestRun] = relationship(back_populates="trades")
    entry_signal: Mapped[SignalEvaluation | None] = relationship(
        back_populates="entry_trades",
        foreign_keys=[entry_signal_evaluation_id],
    )
    legs: Mapped[list[TradeLeg]] = relationship(
        back_populates="trade",
        cascade="all, delete-orphan",
        order_by="TradeLeg.leg_number",
    )


class TradeLeg(Base):
    __tablename__ = "trade_legs"
    __table_args__ = (
        UniqueConstraint("trade_id", "leg_number", name="uq_trade_legs_number"),
        CheckConstraint("leg_number IN (1, 2)", name="ck_trade_legs_placeholder_number"),
        CheckConstraint("unit IN ('DEMO_UNITS', 'INR')", name="ck_trade_legs_unit"),
        CheckConstraint(
            "(unit = 'DEMO_UNITS' AND synthetic AND NOT financial_pnl_available) OR (unit = 'INR' AND NOT synthetic AND financial_pnl_available)",
            name="ck_trade_legs_mode_flags",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    trade_id: Mapped[UUID] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    leg_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    entry_reference: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exit_reference: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="DEMO_UNITS")
    financial_pnl_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trade: Mapped[Trade] = relationship(back_populates="legs")


class EquityPoint(Base):
    __tablename__ = "equity_points"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_equity_points_run_sequence"),
        CheckConstraint("sequence > 0", name="ck_equity_points_positive_sequence"),
        CheckConstraint("unit IN ('DEMO_UNITS', 'INR')", name="ck_equity_points_unit"),
        CheckConstraint(
            "(unit = 'DEMO_UNITS' AND synthetic AND NOT financial_pnl_available) OR (unit = 'INR' AND NOT synthetic AND financial_pnl_available)",
            name="ck_equity_points_mode_flags",
        ),
        Index("ix_equity_points_run_timestamp", "run_id", "timestamp"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    equity_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    drawdown_demo_units: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="DEMO_UNITS")
    financial_pnl_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run: Mapped[BacktestRun] = relationship(back_populates="equity_points")
