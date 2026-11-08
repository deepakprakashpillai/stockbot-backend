"""Durable dispatch and export ownership.

Revision ID: 20261017_14
Revises: 20260920_13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261017_14"
down_revision = "20260920_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("depends_on_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("kind", "resource_id", name="uq_job_dispatch_resource"),
    )
    op.create_index("ix_job_dispatches_status", "job_dispatches", ["status"])
    op.add_column("backtest_export_jobs", sa.Column("lease_owner", sa.String(100)))
    op.add_column("backtest_export_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    # Deploy with old API/worker processes stopped. Pre-migration RUNNING exports
    # have no ownership lease and need a new fenced build rather than remaining stuck.
    op.execute("UPDATE backtest_export_jobs SET status = 'PENDING' WHERE status = 'RUNNING'")
    for table in ("trades", "trade_legs", "equity_points"):
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_mode_flags CHECK ((unit = 'DEMO_UNITS' AND synthetic AND NOT financial_pnl_available) OR (unit = 'INR' AND NOT synthetic AND financial_pnl_available)) NOT VALID"
        )
    for kind, table, statuses in (
        ("BACKTEST", "backtest_runs", "'PENDING'"),
        ("DATASET_VALIDATION", "dataset_versions", "'PENDING'"),
        ("EXPORT", "backtest_export_jobs", "'PENDING'"),
    ):
        op.execute(
            f"INSERT INTO job_dispatches (id, kind, resource_id, status, attempts) SELECT gen_random_uuid(), '{kind}', id, 'PENDING', 0 FROM {table} WHERE status IN ({statuses})"
        )


def downgrade() -> None:
    for table in ("trades", "trade_legs", "equity_points"):
        op.drop_constraint(f"ck_{table}_mode_flags", table, type_="check")
    op.drop_column("backtest_export_jobs", "lease_expires_at")
    op.drop_column("backtest_export_jobs", "lease_owner")
    op.drop_index("ix_job_dispatches_status", table_name="job_dispatches")
    op.drop_table("job_dispatches")
