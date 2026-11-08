"""Add stable per-run signal sequences for live checkpoint recovery.

Revision ID: 20260816_08
Revises: 20260816_07
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "20260816_08"
down_revision = "20260816_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_evaluations", sa.Column("sequence", sa.Integer(), nullable=True))
    op.execute(
        """
        WITH ordered AS (
            SELECT id, row_number() OVER (PARTITION BY run_id ORDER BY timestamp, id) AS sequence
            FROM signal_evaluations
        )
        UPDATE signal_evaluations
        SET sequence = ordered.sequence
        FROM ordered
        WHERE signal_evaluations.id = ordered.id
        """
    )
    op.alter_column("signal_evaluations", "sequence", nullable=False)
    op.create_unique_constraint(
        "uq_signal_evaluations_sequence", "signal_evaluations", ["run_id", "sequence"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_signal_evaluations_sequence", "signal_evaluations", type_="unique")
    op.drop_column("signal_evaluations", "sequence")
