"""Add durable linked-exploration indexes and signal-to-trade identity.

Revision ID: 20260816_09
Revises: 20260816_08
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260816_09"
down_revision = "20260816_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_signal_evaluations_run_id_id", "signal_evaluations", ["run_id", "id"]
    )
    op.add_column(
        "trades",
        sa.Column("entry_signal_evaluation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # A legacy PLATFORM_DEMO trade is linked only when precisely one accepted
    # evaluation in its run shares its deterministic entry timestamp. Ambiguous
    # or absent evidence remains deliberately unlinked.
    op.execute(
        """
        WITH proven_links AS (
            SELECT trades.id AS trade_id, (array_agg(signal_evaluations.id))[1] AS signal_id
            FROM trades
            JOIN signal_evaluations
              ON signal_evaluations.run_id = trades.run_id
             AND signal_evaluations.timestamp = trades.entry_timestamp
             AND signal_evaluations.decision = 'ACCEPTED'
            GROUP BY trades.id
            HAVING count(signal_evaluations.id) = 1
        )
        UPDATE trades
        SET entry_signal_evaluation_id = proven_links.signal_id
        FROM proven_links
        WHERE trades.id = proven_links.trade_id
        """
    )
    op.create_foreign_key(
        "fk_trades_entry_signal_same_run",
        "trades",
        "signal_evaluations",
        ["run_id", "entry_signal_evaluation_id"],
        ["run_id", "id"],
    )
    op.create_index(
        "ix_trades_entry_signal_evaluation_id",
        "trades",
        ["entry_signal_evaluation_id"],
    )
    op.create_index(
        "ix_signal_evaluations_run_timestamp", "signal_evaluations", ["run_id", "timestamp"]
    )
    op.create_index("ix_run_events_run_created_at", "run_events", ["run_id", "created_at"])
    op.create_index(
        "ix_trades_run_entry_exit", "trades", ["run_id", "entry_timestamp", "exit_timestamp"]
    )
    op.create_index("ix_equity_points_run_timestamp", "equity_points", ["run_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_equity_points_run_timestamp", table_name="equity_points")
    op.drop_index("ix_trades_run_entry_exit", table_name="trades")
    op.drop_index("ix_run_events_run_created_at", table_name="run_events")
    op.drop_index("ix_signal_evaluations_run_timestamp", table_name="signal_evaluations")
    op.drop_index("ix_trades_entry_signal_evaluation_id", table_name="trades")
    op.drop_constraint("fk_trades_entry_signal_same_run", "trades", type_="foreignkey")
    op.drop_column("trades", "entry_signal_evaluation_id")
    op.drop_constraint("uq_signal_evaluations_run_id_id", "signal_evaluations", type_="unique")
