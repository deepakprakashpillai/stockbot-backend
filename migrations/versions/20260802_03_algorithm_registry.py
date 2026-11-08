"""algorithm registry and immutable configuration versions

Revision ID: 20260802_03
Revises: 20260802_02
Create Date: 2026-08-02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260802_03"
down_revision = "20260802_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "algorithms",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("code_version", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("schema_hash", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("supported_modes", postgresql.JSONB(), nullable=False),
        sa.Column("required_capabilities", postgresql.JSONB(), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_table(
        "algorithm_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "algorithm_key",
            sa.String(100),
            sa.ForeignKey("algorithms.key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "parent_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("algorithm_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("code_version", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("creator_context", postgresql.JSONB(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("algorithm_key", "version_number", name="uq_algorithm_version_number"),
    )
    op.create_index("ix_algorithm_versions_algorithm_key", "algorithm_versions", ["algorithm_key"])
    op.create_index(
        "ix_algorithm_versions_parent_version_id", "algorithm_versions", ["parent_version_id"]
    )
    op.create_index("ix_algorithm_versions_archived", "algorithm_versions", ["archived"])
    op.create_index("ix_algorithm_versions_created_at", "algorithm_versions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_algorithm_versions_created_at", table_name="algorithm_versions")
    op.drop_index("ix_algorithm_versions_archived", table_name="algorithm_versions")
    op.drop_index("ix_algorithm_versions_parent_version_id", table_name="algorithm_versions")
    op.drop_index("ix_algorithm_versions_algorithm_key", table_name="algorithm_versions")
    op.drop_table("algorithm_versions")
    op.drop_table("algorithms")
