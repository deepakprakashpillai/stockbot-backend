"""Protect saved algorithm definitions and run inputs in PostgreSQL.

Revision ID: 20261108_15
Revises: 20261017_14
"""

from alembic import op

revision = "20261108_15"
down_revision = "20261017_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_algorithm_version_inputs() RETURNS trigger AS $$
        BEGIN
            IF ROW(NEW.algorithm_key, NEW.version_number, NEW.name, NEW.notes,
                   NEW.parent_version_id, NEW.code_version, NEW.schema_version,
                   NEW.parameters, NEW.creator_context, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.algorithm_key, OLD.version_number, OLD.name, OLD.notes,
                   OLD.parent_version_id, OLD.code_version, OLD.schema_version,
                   OLD.parameters, OLD.creator_context, OLD.created_at) THEN
                RAISE EXCEPTION 'Saved algorithm version inputs are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_algorithm_version_inputs
        BEFORE UPDATE ON algorithm_versions
        FOR EACH ROW EXECUTE FUNCTION guard_algorithm_version_inputs();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_backtest_run_inputs() RETURNS trigger AS $$
        BEGIN
            IF ROW(NEW.dataset_version_id, NEW.algorithm_version_id,
                   NEW.start_date, NEW.end_date, NEW.mode, NEW.starting_capital,
                   NEW.snapshot, NEW.idempotency_key, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.dataset_version_id, OLD.algorithm_version_id,
                   OLD.start_date, OLD.end_date, OLD.mode, OLD.starting_capital,
                   OLD.snapshot, OLD.idempotency_key, OLD.created_at) THEN
                RAISE EXCEPTION 'Saved backtest run inputs are immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_backtest_run_inputs
        BEFORE UPDATE ON backtest_runs
        FOR EACH ROW EXECUTE FUNCTION guard_backtest_run_inputs();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER protect_backtest_run_inputs ON backtest_runs")
    op.execute("DROP FUNCTION guard_backtest_run_inputs()")
    op.execute("DROP TRIGGER protect_algorithm_version_inputs ON algorithm_versions")
    op.execute("DROP FUNCTION guard_algorithm_version_inputs()")
