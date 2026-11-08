"""versioned completed-run analytics

Revision ID: 20260814_06
Revises: 20260805_05
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260814_06"
down_revision = "20260805_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("costs_demo_units", sa.Numeric(24, 8), nullable=True))
    op.execute("UPDATE trades SET costs_demo_units = gross_demo_units - net_demo_units")
    op.alter_column("trades", "costs_demo_units", nullable=False)
    op.create_table(
        "run_analytics",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("calculator_version", sa.String(length=100), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "run_analytics_periods",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("granularity", sa.String(length=10), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("partial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("eligible_session_count", sa.Integer(), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("breakevens", sa.Integer(), nullable=False),
        sa.Column("gross_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("costs_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("net_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("cumulative_net_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("equity_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("local_max_drawdown_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("run_drawdown_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.CheckConstraint("granularity IN ('DAY', 'MONTH')", name="ck_run_analytics_granularity"),
        sa.UniqueConstraint(
            "run_id", "granularity", "period_start", name="uq_run_analytics_period"
        ),
    )
    op.create_index("ix_run_analytics_periods_run_id", "run_analytics_periods", ["run_id"])
    op.create_index(
        "ix_run_analytics_periods_lookup",
        "run_analytics_periods",
        ["run_id", "granularity", "period_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_analytics_periods_lookup", table_name="run_analytics_periods")
    op.drop_index("ix_run_analytics_periods_run_id", table_name="run_analytics_periods")
    op.drop_table("run_analytics_periods")
    op.drop_table("run_analytics")
    op.drop_column("trades", "costs_demo_units")
