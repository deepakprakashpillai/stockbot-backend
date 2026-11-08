"""platform demo result persistence

Revision ID: 20260805_05
Revises: 20260802_04
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260805_05"
down_revision = "20260802_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("entry_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_reason", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_reference", sa.Numeric(20, 8), nullable=False),
        sa.Column("exit_reference", sa.Numeric(20, 8), nullable=False),
        sa.Column("gross_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("net_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="DEMO_UNITS"),
        sa.Column(
            "financial_pnl_available", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("assumptions", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_trades_positive_sequence"),
        sa.CheckConstraint("quantity > 0", name="ck_trades_positive_quantity"),
        sa.CheckConstraint("synthetic", name="ck_trades_synthetic"),
        sa.CheckConstraint("unit = 'DEMO_UNITS'", name="ck_trades_demo_unit"),
        sa.CheckConstraint("NOT financial_pnl_available", name="ck_trades_no_financial_pnl"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_trades_run_sequence"),
    )
    op.create_index("ix_trades_run_id", "trades", ["run_id"])

    op.create_table(
        "trade_legs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trade_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trades.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("entry_reference", sa.Numeric(20, 8), nullable=False),
        sa.Column("exit_reference", sa.Numeric(20, 8), nullable=False),
        sa.Column("demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="DEMO_UNITS"),
        sa.Column(
            "financial_pnl_available", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.CheckConstraint("leg_number IN (1, 2)", name="ck_trade_legs_placeholder_number"),
        sa.CheckConstraint("synthetic", name="ck_trade_legs_synthetic"),
        sa.CheckConstraint("unit = 'DEMO_UNITS'", name="ck_trade_legs_demo_unit"),
        sa.CheckConstraint("NOT financial_pnl_available", name="ck_trade_legs_no_financial_pnl"),
        sa.UniqueConstraint("trade_id", "leg_number", name="uq_trade_legs_number"),
    )
    op.create_index("ix_trade_legs_trade_id", "trade_legs", ["trade_id"])

    op.create_table(
        "equity_points",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("drawdown_demo_units", sa.Numeric(24, 8), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="DEMO_UNITS"),
        sa.Column(
            "financial_pnl_available", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.CheckConstraint("sequence > 0", name="ck_equity_points_positive_sequence"),
        sa.CheckConstraint(
            "drawdown_demo_units <= 0", name="ck_equity_points_nonpositive_drawdown"
        ),
        sa.CheckConstraint("synthetic", name="ck_equity_points_synthetic"),
        sa.CheckConstraint("unit = 'DEMO_UNITS'", name="ck_equity_points_demo_unit"),
        sa.CheckConstraint("NOT financial_pnl_available", name="ck_equity_points_no_financial_pnl"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_equity_points_run_sequence"),
    )
    op.create_index("ix_equity_points_run_id", "equity_points", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_equity_points_run_id", table_name="equity_points")
    op.drop_table("equity_points")
    op.drop_index("ix_trade_legs_trade_id", table_name="trade_legs")
    op.drop_table("trade_legs")
    op.drop_index("ix_trades_run_id", table_name="trades")
    op.drop_table("trades")
