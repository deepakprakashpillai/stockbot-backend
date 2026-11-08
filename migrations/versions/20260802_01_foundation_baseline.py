"""foundation baseline

Revision ID: 20260802_01
Revises:
Create Date: 2026-08-02
"""

revision = "20260802_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic's version table is the only foundation persistence required in Phase 1.
    pass


def downgrade() -> None:
    pass
