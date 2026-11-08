"""Add backtest attempts, journals, day coverage, exports, and experiments.

Revision ID: 20260914_12
Revises: 20260828_11
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260914_12"
down_revision = "20260828_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("active_attempt_id", sa.UUID(), nullable=True))
    op.create_index("ix_backtest_runs_active_attempt_id", "backtest_runs", ["active_attempt_id"])

    op.create_table(
        "backtest_execution_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("worker_owner", sa.String(length=100), nullable=True),
        sa.Column("legacy", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metadata_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_backtest_attempt_number_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "attempt_number", name="uq_backtest_attempt_number"),
    )
    op.create_index(
        "ix_backtest_execution_attempts_run_id", "backtest_execution_attempts", ["run_id"]
    )
    op.create_index(
        "ix_backtest_execution_attempts_status", "backtest_execution_attempts", ["status"]
    )

    op.create_table(
        "backtest_journal_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("position_state", sa.String(length=30), nullable=True),
        sa.Column("signal_sequence", sa.Integer(), nullable=True),
        sa.Column("trade_sequence", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence > 0", name="ck_backtest_journal_sequence_positive"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["backtest_execution_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "sequence", name="uq_backtest_journal_attempt_sequence"),
    )
    op.create_index(
        "ix_backtest_journal_run_timestamp", "backtest_journal_events", ["run_id", "timestamp"]
    )
    op.create_index(
        "ix_backtest_journal_events_attempt_id", "backtest_journal_events", ["attempt_id"]
    )
    op.create_index(
        "ix_backtest_journal_events_event_type", "backtest_journal_events", ["event_type"]
    )
    op.create_index(
        "ix_backtest_journal_events_position_state", "backtest_journal_events", ["position_state"]
    )
    op.create_index("ix_backtest_journal_events_run_id", "backtest_journal_events", ["run_id"])
    op.create_index(
        "ix_backtest_journal_events_session_date", "backtest_journal_events", ["session_date"]
    )

    op.create_table(
        "backtest_day_classifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("classification", sa.String(length=40), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("primary_reason", sa.String(length=100), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["backtest_execution_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "session_date", name="uq_backtest_day_attempt_session"),
    )
    op.create_index(
        "ix_backtest_day_run_session", "backtest_day_classifications", ["run_id", "session_date"]
    )
    op.create_index(
        "ix_backtest_day_classifications_attempt_id", "backtest_day_classifications", ["attempt_id"]
    )
    op.create_index(
        "ix_backtest_day_classifications_classification",
        "backtest_day_classifications",
        ["classification"],
    )
    op.create_index(
        "ix_backtest_day_classifications_run_id", "backtest_day_classifications", ["run_id"]
    )

    op.create_table(
        "backtest_export_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("row_counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["backtest_execution_attempts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backtest_export_jobs_run_id", "backtest_export_jobs", ["run_id"])
    op.create_index("ix_backtest_export_jobs_status", "backtest_export_jobs", ["status"])

    op.create_table(
        "experiment_plans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("algorithm_key", sa.String(length=100), nullable=False),
        sa.Column("baseline_algorithm_version_id", sa.UUID(), nullable=False),
        sa.Column("dataset_version_id", sa.UUID(), nullable=False),
        sa.Column("parameter_key", sa.String(length=100), nullable=False),
        sa.Column("parameter_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("development_start_date", sa.Date(), nullable=False),
        sa.Column("development_end_date", sa.Date(), nullable=False),
        sa.Column("validation_start_date", sa.Date(), nullable=False),
        sa.Column("validation_end_date", sa.Date(), nullable=False),
        sa.Column("coverage_digest", sa.String(length=64), nullable=False),
        sa.Column("starting_capital", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("validation_unlocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selected_algorithm_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_algorithm_version_id"], ["algorithm_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"], ["dataset_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["selected_algorithm_version_id"], ["algorithm_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_experiment_plans_algorithm_key", "experiment_plans", ["algorithm_key"])
    op.create_index("ix_experiment_plans_status", "experiment_plans", ["status"])
    op.create_table(
        "experiment_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("experiment_plan_id", sa.UUID(), nullable=False),
        sa.Column("backtest_run_id", sa.UUID(), nullable=False),
        sa.Column("variant_index", sa.Integer(), nullable=False),
        sa.Column("parameter_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "variant_index >= 0 AND variant_index < 10", name="ck_experiment_variant_index"
        ),
        sa.ForeignKeyConstraint(["backtest_run_id"], ["backtest_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["experiment_plan_id"], ["experiment_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("backtest_run_id"),
        sa.UniqueConstraint(
            "experiment_plan_id", "phase", "variant_index", name="uq_experiment_variant_index"
        ),
    )
    op.create_index(
        "ix_experiment_runs_experiment_plan_id", "experiment_runs", ["experiment_plan_id"]
    )

    # Preserve old evidence without inventing journal detail.
    op.execute(
        """
        INSERT INTO backtest_execution_attempts
            (id, run_id, attempt_number, status, worker_owner, legacy, reason, metadata_snapshot, started_at, completed_at)
        SELECT gen_random_uuid(), id, 1,
            CASE WHEN status = 'COMPLETED' THEN 'COMPLETED' ELSE status END,
            lease_owner, true, 'Imported legacy attempt; detailed checkpoint journal unavailable.',
            '{}'::jsonb, COALESCE(started_at, created_at), completed_at
        FROM backtest_runs
        """
    )
    op.execute(
        """
        UPDATE backtest_runs AS runs
        SET active_attempt_id = attempts.id
        FROM backtest_execution_attempts AS attempts
        WHERE attempts.run_id = runs.id AND attempts.attempt_number = 1
        """
    )


def downgrade() -> None:
    op.drop_table("experiment_runs")
    op.drop_table("experiment_plans")
    op.drop_table("backtest_export_jobs")
    op.drop_table("backtest_day_classifications")
    op.drop_table("backtest_journal_events")
    op.drop_table("backtest_execution_attempts")
    op.drop_index("ix_backtest_runs_active_attempt_id", table_name="backtest_runs")
    op.drop_column("backtest_runs", "active_attempt_id")
