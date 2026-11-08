"""Persist immutable, versioned signal diagnostics.

Revision ID: 20260816_10
Revises: 20260816_09
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260816_10"
down_revision = "20260816_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signal_evaluations",
        sa.Column("diagnostic", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signal_evaluations", "diagnostic")
