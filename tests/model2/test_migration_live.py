"""Upgrade/downgrade on a disposable schema containing legacy two-leg records."""

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.models.algorithms import Algorithm, AlgorithmVersion
from app.db.models.backtests import BacktestRun, Trade
from app.db.models.datasets import Dataset, DatasetVersion


def test_model2_migration_preserves_legacy_rows_and_refuses_destructive_rollback(postgres_factory):
    url = os.environ["STOCKBOT_TEST_DATABASE_URL"]
    schema = "stockbot_migration_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    env = os.environ.copy()
    env.update(DATABASE_URL=url, PGOPTIONS=f"-c search_path={schema}")

    def migrate(*args, success=True):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (result.returncode == 0) == success, result.stderr

    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        migrate("upgrade", "20261108_15")
        with Session(engine) as session:
            session.add(
                Algorithm(
                    key="legacy-fixture",
                    display_name="Legacy",
                    description="Migration fixture",
                    code_version="legacy",
                    schema_version=1,
                    schema_hash="a" * 64,
                    active=True,
                    supported_modes=[],
                    required_capabilities=[],
                )
            )
            session.flush()
            dataset = Dataset(id=uuid4(), name="Legacy fixture")
            algorithm = AlgorithmVersion(
                id=uuid4(),
                algorithm_key="legacy-fixture",
                version_number=1,
                name="Legacy",
                code_version="legacy",
                schema_version=1,
                parameters={},
                creator_context={},
            )
            session.add_all([dataset, algorithm])
            session.flush()
            version = DatasetVersion(
                id=uuid4(),
                dataset_id=dataset.id,
                version_number=1,
                schema_profile="fixture",
                original_filename="fixture.csv",
                checksum="b" * 64,
                storage_key="fixture.csv",
                status="VALID",
                session_dates=[],
                capabilities={},
                profile={},
                warnings=[],
                errors=[],
            )
            session.add(version)
            session.flush()
            run = BacktestRun(
                id=uuid4(),
                dataset_version_id=version.id,
                algorithm_version_id=algorithm.id,
                start_date=date(2026, 1, 2),
                end_date=date(2026, 1, 2),
                mode="PLATFORM_DEMO",
                starting_capital=Decimal(100000),
                snapshot={"legacy": "preserve"},
                status="COMPLETED",
            )
            session.add(run)
            session.flush()
            trade = Trade(
                id=uuid4(),
                run_id=run.id,
                sequence=1,
                entry_timestamp=datetime(2026, 1, 2, 10, tzinfo=UTC),
                exit_timestamp=datetime(2026, 1, 2, 11, tzinfo=UTC),
                exit_reason="LEGACY",
                quantity=1,
                entry_reference=100,
                exit_reference=102,
                gross_demo_units=2,
                costs_demo_units=0,
                net_demo_units=2,
                synthetic=True,
                unit="DEMO_UNITS",
                financial_pnl_available=False,
                assumptions={},
            )
            session.add(trade)
            session.flush()
            for index in (1, 2):
                session.execute(
                    text(
                        "INSERT INTO trade_legs(id,trade_id,leg_number,name,entry_reference,exit_reference,demo_units,synthetic,unit,financial_pnl_available) VALUES(:id,:trade,:number,'Legacy',100,101,1,true,'DEMO_UNITS',false)"
                    ),
                    {"id": uuid4(), "trade": trade.id, "number": index},
                )
            session.commit()
        with engine.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT id,trade_id,leg_number,name,entry_reference,exit_reference,demo_units,synthetic,unit,financial_pnl_available FROM trade_legs ORDER BY leg_number"
                )
            ).all()
        migrate("upgrade", "head")
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT id,trade_id,leg_number,name,entry_reference,exit_reference,demo_units,synthetic,unit,financial_pnl_available FROM trade_legs ORDER BY leg_number"
                    )
                ).all()
                == before
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM trade_legs WHERE role IS NOT NULL")
                ).scalar()
                == 0
            )
            group_id = uuid4()
            connection.execute(
                text(
                    "INSERT INTO research_scenario_groups(id,idempotency_key,request_digest,snapshot,status,cancellation_requested) VALUES(:id,'rollback-guard',:digest,'{}','PENDING',false)"
                ),
                {"id": group_id, "digest": "c" * 64},
            )
        migrate("downgrade", "20261108_15", success=False)
        with engine.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM trade_legs")).scalar() == 2
            connection.execute(
                text("DELETE FROM research_scenario_groups WHERE id=:id"), {"id": group_id}
            )
        migrate("downgrade", "20261108_15")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT id,trade_id,leg_number,name,entry_reference,exit_reference,demo_units,synthetic,unit,financial_pnl_available FROM trade_legs ORDER BY leg_number"
                    )
                ).all()
                == before
            )
        migrate("upgrade", "head")
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
