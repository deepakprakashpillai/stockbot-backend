"""Allow hard-gate signal decisions to omit an uncalculated score.

Revision ID: 20260920_13
Revises: 20260914_12
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "20260920_13"
down_revision = "20260914_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("signal_evaluations", "score", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE signal_evaluations SET score = 0 WHERE score IS NULL")
    op.alter_column("signal_evaluations", "score", existing_type=sa.Integer(), nullable=False)
