"""Attempt-scoped Model 2 cycles and risk evidence; existing runs stay unchanged."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ResearchScenarioGroup(Base):
    __tablename__ = "research_scenario_groups"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchScenarioMember(Base):
    __tablename__ = "research_scenario_members"
    __table_args__ = (
        UniqueConstraint("group_id", "policy", name="uq_scenario_group_policy"),
        UniqueConstraint("group_id", "ordinal", name="uq_scenario_group_ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 3", name="ck_scenario_ordinal"),
        CheckConstraint(
            "policy IN ('MAX_LOSS_RISK','ZERO_RISK','HALT_AT_FIRST_EXCLUSION')",
            name="ck_scenario_policy",
        ),
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_scenario_groups.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    policy: Mapped[str] = mapped_column(String(30), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class BacktestCycle(Base):
    __tablename__ = "backtest_cycles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "run_id"],
            ["backtest_execution_attempts.id", "backtest_execution_attempts.run_id"],
            ondelete="CASCADE",
            name="fk_cycle_same_run_attempt",
        ),
        UniqueConstraint("attempt_id", "expiry", name="uq_backtest_cycle_attempt_expiry"),
        UniqueConstraint("id", "run_id", "attempt_id", name="uq_backtest_cycle_identity"),
        CheckConstraint(
            "state IN ('ENTRY_PENDING','OPEN','EXIT_PENDING','SKIPPED','CLOSED','SETTLED','EXCLUDED')",
            name="ck_backtest_cycle_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(100))
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trade_id: Mapped[UUID | None] = mapped_column(ForeignKey("trades.id", ondelete="RESTRICT"))


class BacktestRiskEntry(Base):
    __tablename__ = "backtest_risk_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cycle_id", "run_id", "attempt_id"],
            ["backtest_cycles.id", "backtest_cycles.run_id", "backtest_cycles.attempt_id"],
            ondelete="CASCADE",
            name="fk_risk_cycle_same_attempt",
        ),
        CheckConstraint(
            "source IN ('PRICED_RESULT','ASSUMED_MAX_LOSS','ASSUMED_ZERO','HALTED_AT_EXCLUSION')",
            name="ck_risk_source",
        ),
        CheckConstraint(
            "(source = 'PRICED_RESULT' AND risk_amount IS NOT NULL AND priced_performance_amount IS NOT NULL AND risk_amount = priced_performance_amount) OR (source IN ('ASSUMED_MAX_LOSS','ASSUMED_ZERO') AND risk_amount IS NOT NULL AND priced_performance_amount IS NULL) OR (source = 'HALTED_AT_EXCLUSION' AND risk_amount IS NULL AND priced_performance_amount IS NULL)",
            name="ck_risk_separate_performance",
        ),
    )
    cycle_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    priced_performance_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
