"""dataset explorer sessions and immutable profiles

Revision ID: 20260816_07
Revises: 20260814_06
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260816_07"
down_revision = "20260814_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dataset_versions",
        sa.Column(
            "profile", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.alter_column("dataset_versions", "profile", server_default=None)
    op.create_table(
        "dataset_upload_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("staging_key", sa.String(length=1000), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("schema_profile", sa.String(length=100), nullable=True),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_dataset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("committed_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_dataset_upload_sessions_owner_key", "dataset_upload_sessions", ["owner_key"]
    )
    op.create_index("ix_dataset_upload_sessions_status", "dataset_upload_sessions", ["status"])
    op.create_index("ix_dataset_upload_sessions_checksum", "dataset_upload_sessions", ["checksum"])
    op.create_index(
        "ix_dataset_upload_sessions_expires_at", "dataset_upload_sessions", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_upload_sessions_expires_at", table_name="dataset_upload_sessions")
    op.drop_index("ix_dataset_upload_sessions_checksum", table_name="dataset_upload_sessions")
    op.drop_index("ix_dataset_upload_sessions_status", table_name="dataset_upload_sessions")
    op.drop_index("ix_dataset_upload_sessions_owner_key", table_name="dataset_upload_sessions")
    op.drop_table("dataset_upload_sessions")
    op.drop_column("dataset_versions", "profile")
