"""Enable option spread financial simulation and INR units.

Revision ID: 20260828_11
Revises: 20260816_10
Create Date: 2026-08-28
"""

from alembic import op

revision = "20260828_11"
down_revision = "20260816_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Update trades check constraints
    op.drop_constraint("ck_trades_synthetic", "trades", type_="check")
    op.drop_constraint("ck_trades_demo_unit", "trades", type_="check")
    op.drop_constraint("ck_trades_no_financial_pnl", "trades", type_="check")
    op.create_check_constraint("ck_trades_unit", "trades", "unit IN ('DEMO_UNITS', 'INR')")

    # 2. Update trade_legs check constraints
    op.drop_constraint("ck_trade_legs_synthetic", "trade_legs", type_="check")
    op.drop_constraint("ck_trade_legs_demo_unit", "trade_legs", type_="check")
    op.drop_constraint("ck_trade_legs_no_financial_pnl", "trade_legs", type_="check")
    op.create_check_constraint("ck_trade_legs_unit", "trade_legs", "unit IN ('DEMO_UNITS', 'INR')")

    # 3. Update equity_points check constraints
    op.drop_constraint("ck_equity_points_synthetic", "equity_points", type_="check")
    op.drop_constraint("ck_equity_points_demo_unit", "equity_points", type_="check")
    op.drop_constraint("ck_equity_points_no_financial_pnl", "equity_points", type_="check")
    op.create_check_constraint(
        "ck_equity_points_unit", "equity_points", "unit IN ('DEMO_UNITS', 'INR')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_equity_points_unit", "equity_points", type_="check")
    op.create_check_constraint("ck_equity_points_synthetic", "equity_points", "synthetic")
    op.create_check_constraint("ck_equity_points_demo_unit", "equity_points", "unit = 'DEMO_UNITS'")
    op.create_check_constraint(
        "ck_equity_points_no_financial_pnl", "equity_points", "NOT financial_pnl_available"
    )

    op.drop_constraint("ck_trade_legs_unit", "trade_legs", type_="check")
    op.create_check_constraint("ck_trade_legs_synthetic", "trade_legs", "synthetic")
    op.create_check_constraint("ck_trade_legs_demo_unit", "trade_legs", "unit = 'DEMO_UNITS'")
    op.create_check_constraint(
        "ck_trade_legs_no_financial_pnl", "trade_legs", "NOT financial_pnl_available"
    )

    op.drop_constraint("ck_trades_unit", "trades", type_="check")
    op.create_check_constraint("ck_trades_synthetic", "trades", "synthetic")
    op.create_check_constraint("ck_trades_demo_unit", "trades", "unit = 'DEMO_UNITS'")
    op.create_check_constraint(
        "ck_trades_no_financial_pnl", "trades", "NOT financial_pnl_available"
    )
