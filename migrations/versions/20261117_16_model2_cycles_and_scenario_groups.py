"""model2_cycles_and_scenario_groups

Revision ID: 20261117_16
Revises: 20261108_15
Create Date: 2026-11-17 19:32:56.871492

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261117_16"
down_revision: str | Sequence[str] | None = "20261108_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


GUARDS_SQL = r"""CREATE FUNCTION guard_scenario_group_inputs() RETURNS trigger AS $$ BEGIN
        IF ROW(NEW.snapshot,NEW.request_digest,NEW.idempotency_key,NEW.created_at) IS DISTINCT FROM ROW(OLD.snapshot,OLD.request_digest,OLD.idempotency_key,OLD.created_at) THEN
            RAISE EXCEPTION 'Scenario inputs are immutable' USING ERRCODE='23514';
        END IF; RETURN NEW; END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER protect_scenario_group_inputs BEFORE UPDATE ON research_scenario_groups FOR EACH ROW EXECUTE FUNCTION guard_scenario_group_inputs();
        CREATE FUNCTION guard_scenario_member_inputs() RETURNS trigger AS $$ BEGIN
            RAISE EXCEPTION 'Scenario membership is immutable' USING ERRCODE='23514';
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER protect_scenario_member_inputs BEFORE UPDATE ON research_scenario_members FOR EACH ROW EXECUTE FUNCTION guard_scenario_member_inputs();

CREATE FUNCTION guard_model2_leg_count() RETURNS trigger AS $$
DECLARE tid uuid; expected boolean;
BEGIN
    IF TG_TABLE_NAME = 'trades' THEN tid := COALESCE(NEW.id,OLD.id); ELSE tid := COALESCE(NEW.trade_id,OLD.trade_id); END IF;
    SELECT assumptions->>'algorithm_key' = 'model2_overnight_iron_condor' INTO expected FROM trades WHERE id=tid;
    IF expected AND ((SELECT count(*) FROM trade_legs WHERE trade_id=tid) <> 4 OR
        (SELECT count(DISTINCT role) FROM trade_legs WHERE trade_id=tid) <> 4 OR
        EXISTS (SELECT 1 FROM trade_legs l JOIN trades t ON t.id=l.trade_id WHERE t.id=tid AND
            (l.contract_id IS NULL OR l.option_type IS NULL OR l.expiry IS NULL OR l.strike IS NULL OR l.quantity IS NULL OR l.quantity <> t.quantity OR l.side IS NULL OR l.side NOT IN ('BUY','SELL') OR l.tick_size IS NULL OR l.tick_size <= 0 OR l.lot_size IS NULL OR l.quantity <> l.lot_size))) THEN
        RAISE EXCEPTION 'Model 2 requires exactly four complete uniquely named legs' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER model2_trade_cardinality AFTER INSERT OR UPDATE ON trades DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION guard_model2_leg_count();
CREATE CONSTRAINT TRIGGER model2_leg_cardinality AFTER INSERT OR UPDATE OR DELETE ON trade_legs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION guard_model2_leg_count();
"""


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_backtest_attempt_identity", "backtest_execution_attempts", ["id", "run_id"]
    )

    op.drop_constraint("ck_trade_legs_placeholder_number", "trade_legs", type_="check")
    op.create_check_constraint(
        "ck_trade_legs_placeholder_number", "trade_legs", "leg_number BETWEEN 1 AND 4"
    )

    op.create_table(
        "research_scenario_groups",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "research_scenario_members",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("policy", sa.String(length=30), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "policy IN ('MAX_LOSS_RISK','ZERO_RISK','HALT_AT_FIRST_EXCLUSION')",
            name="ck_scenario_policy",
        ),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 3", name="ck_scenario_ordinal"),
        sa.ForeignKeyConstraint(["group_id"], ["research_scenario_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("group_id", "ordinal", name="uq_scenario_group_ordinal"),
        sa.UniqueConstraint("group_id", "policy", name="uq_scenario_group_policy"),
    )
    op.create_index(
        op.f("ix_research_scenario_members_group_id"),
        "research_scenario_members",
        ["group_id"],
        unique=False,
    )
    op.create_table(
        "backtest_cycles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trade_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "state IN ('ENTRY_PENDING','OPEN','EXIT_PENDING','SKIPPED','CLOSED','SETTLED','EXCLUDED')",
            name="ck_backtest_cycle_state",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "run_id"],
            ["backtest_execution_attempts.id", "backtest_execution_attempts.run_id"],
            ondelete="CASCADE",
            name="fk_cycle_same_run_attempt",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "expiry", name="uq_backtest_cycle_attempt_expiry"),
        sa.UniqueConstraint("id", "run_id", "attempt_id", name="uq_backtest_cycle_identity"),
    )
    op.create_index(
        op.f("ix_backtest_cycles_attempt_id"), "backtest_cycles", ["attempt_id"], unique=False
    )
    op.create_index(op.f("ix_backtest_cycles_run_id"), "backtest_cycles", ["run_id"], unique=False)
    op.create_table(
        "backtest_risk_entries",
        sa.Column("cycle_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("risk_amount", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("priced_performance_amount", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("counters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "(source = 'PRICED_RESULT' AND risk_amount IS NOT NULL AND priced_performance_amount IS NOT NULL AND risk_amount = priced_performance_amount) OR (source IN ('ASSUMED_MAX_LOSS','ASSUMED_ZERO') AND risk_amount IS NOT NULL AND priced_performance_amount IS NULL) OR (source = 'HALTED_AT_EXCLUSION' AND risk_amount IS NULL AND priced_performance_amount IS NULL)",
            name="ck_risk_separate_performance",
        ),
        sa.CheckConstraint(
            "source IN ('PRICED_RESULT','ASSUMED_MAX_LOSS','ASSUMED_ZERO','HALTED_AT_EXCLUSION')",
            name="ck_risk_source",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id", "run_id", "attempt_id"],
            ["backtest_cycles.id", "backtest_cycles.run_id", "backtest_cycles.attempt_id"],
            name="fk_risk_cycle_same_attempt",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("cycle_id"),
    )
    op.create_index(
        op.f("ix_backtest_risk_entries_attempt_id"),
        "backtest_risk_entries",
        ["attempt_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_backtest_risk_entries_run_id"), "backtest_risk_entries", ["run_id"], unique=False
    )
    op.add_column("trade_legs", sa.Column("role", sa.String(length=30), nullable=True))
    op.add_column("trade_legs", sa.Column("option_type", sa.String(length=2), nullable=True))
    op.add_column("trade_legs", sa.Column("strike", sa.Integer(), nullable=True))
    op.add_column("trade_legs", sa.Column("expiry", sa.Date(), nullable=True))
    op.add_column("trade_legs", sa.Column("contract_id", sa.String(length=200), nullable=True))
    op.add_column("trade_legs", sa.Column("side", sa.String(length=4), nullable=True))
    op.add_column("trade_legs", sa.Column("quantity", sa.Integer(), nullable=True))
    op.add_column("trade_legs", sa.Column("lot_size", sa.Integer(), nullable=True))
    op.add_column(
        "trade_legs", sa.Column("tick_size", sa.Numeric(precision=20, scale=8), nullable=True)
    )
    op.add_column(
        "trade_legs",
        sa.Column("pricing_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_unique_constraint("uq_trade_legs_role", "trade_legs", ["trade_id", "role"])
    op.create_check_constraint(
        "ck_trade_leg_role",
        "trade_legs",
        "role IS NULL OR role IN ('SHORT_CALL','LONG_CALL','SHORT_PUT','LONG_PUT')",
    )
    op.execute(GUARDS_SQL)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM research_scenario_groups) OR EXISTS (SELECT 1 FROM trade_legs WHERE leg_number > 2) THEN
            RAISE EXCEPTION 'Model 2 records exist; disable new submissions instead of a destructive downgrade';
        END IF;
    END $$;""")
    op.execute("DROP TRIGGER model2_trade_cardinality ON trades")
    op.execute("DROP TRIGGER model2_leg_cardinality ON trade_legs")
    op.execute("DROP FUNCTION guard_model2_leg_count()")
    op.drop_constraint("uq_trade_legs_role", "trade_legs", type_="unique")
    op.drop_constraint("ck_trade_leg_role", "trade_legs", type_="check")
    op.execute("DROP TRIGGER protect_scenario_group_inputs ON research_scenario_groups")
    op.execute("DROP FUNCTION guard_scenario_group_inputs()")
    op.execute("DROP TRIGGER protect_scenario_member_inputs ON research_scenario_members")
    op.execute("DROP FUNCTION guard_scenario_member_inputs()")
    op.drop_constraint("ck_trade_legs_placeholder_number", "trade_legs", type_="check")
    op.create_check_constraint(
        "ck_trade_legs_placeholder_number", "trade_legs", "leg_number IN (1, 2)"
    )

    op.drop_column("trade_legs", "pricing_evidence")
    op.drop_column("trade_legs", "tick_size")
    op.drop_column("trade_legs", "lot_size")
    op.drop_column("trade_legs", "quantity")
    op.drop_column("trade_legs", "side")
    op.drop_column("trade_legs", "contract_id")
    op.drop_column("trade_legs", "expiry")
    op.drop_column("trade_legs", "strike")
    op.drop_column("trade_legs", "option_type")
    op.drop_column("trade_legs", "role")
    op.drop_index(op.f("ix_backtest_risk_entries_run_id"), table_name="backtest_risk_entries")
    op.drop_index(op.f("ix_backtest_risk_entries_attempt_id"), table_name="backtest_risk_entries")
    op.drop_table("backtest_risk_entries")
    op.drop_index(op.f("ix_backtest_cycles_run_id"), table_name="backtest_cycles")
    op.drop_index(op.f("ix_backtest_cycles_attempt_id"), table_name="backtest_cycles")
    op.drop_table("backtest_cycles")
    op.drop_index(
        op.f("ix_research_scenario_members_group_id"), table_name="research_scenario_members"
    )
    op.drop_table("research_scenario_members")
    op.drop_table("research_scenario_groups")
    op.drop_constraint(
        "uq_backtest_attempt_identity", "backtest_execution_attempts", type_="unique"
    )
