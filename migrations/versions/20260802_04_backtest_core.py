"""backtest core and signal evaluation

Revision ID: 20260802_04
Revises: 20260802_03
Create Date: 2026-08-02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260802_04"
down_revision = "20260802_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dataset_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "algorithm_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("algorithm_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("starting_capital", sa.Numeric(20, 2), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cancellation_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("lease_owner", sa.String(100)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_backtest_runs_idempotency_key"),
    )
    op.create_index("ix_backtest_runs_dataset_version_id", "backtest_runs", ["dataset_version_id"])
    op.create_index(
        "ix_backtest_runs_algorithm_version_id", "backtest_runs", ["algorithm_version_id"]
    )
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"])
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])
    op.create_table(
        "signal_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("rejection_reason", sa.String(100)),
        sa.Column("window", sa.String(30)),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_signal_evaluations_run_id", "signal_evaluations", ["run_id"])
    op.create_index("ix_signal_evaluations_timestamp", "signal_evaluations", ["timestamp"])
    op.create_index("ix_signal_evaluations_decision", "signal_evaluations", ["decision"])
    op.create_index(
        "ix_signal_evaluations_rejection_reason", "signal_evaluations", ["rejection_reason"]
    )
    op.create_table(
        "run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_table(
        "run_metrics",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("run_metrics")
    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_signal_evaluations_rejection_reason", table_name="signal_evaluations")
    op.drop_index("ix_signal_evaluations_decision", table_name="signal_evaluations")
    op.drop_index("ix_signal_evaluations_timestamp", table_name="signal_evaluations")
    op.drop_index("ix_signal_evaluations_run_id", table_name="signal_evaluations")
    op.drop_table("signal_evaluations")
    op.drop_index("ix_backtest_runs_created_at", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_status", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_algorithm_version_id", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_dataset_version_id", table_name="backtest_runs")
    op.drop_table("backtest_runs")
