"""Opt-in PostgreSQL proof for saved research input protections."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.application.algorithms import sync_registry
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun
from app.db.models.datasets import Dataset
from tests.test_backtests import dataset


def test_postgres_blocks_input_edits_but_allows_lifecycle_updates(postgres_factory) -> None:
    migration = import_module("migrations.versions.20261108_15_immutable_research_inputs")
    with postgres_factory() as session:
        sync_registry(session)
        version = session.scalar(
            select(AlgorithmVersion).where(AlgorithmVersion.algorithm_key == "platform_demo")
        )
        assert version is not None
        stored = dataset()
        session.add(Dataset(id=stored.dataset_id, name="Immutable input fixture"))
        session.flush()
        session.add(stored)
        session.flush()
        run = BacktestRun(
            id=uuid4(),
            dataset_version_id=stored.id,
            algorithm_version_id=version.id,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            mode="PLATFORM_DEMO",
            starting_capital=Decimal("100000.00"),
            snapshot={"dataset": "unchanged"},
            status="PENDING",
        )
        session.add(run)
        session.commit()

        with Operations.context(MigrationContext.configure(session.connection())):
            migration.upgrade()
        session.commit()

        session.execute(
            update(AlgorithmVersion).where(AlgorithmVersion.id == version.id).values(archived=True)
        )
        session.execute(
            update(BacktestRun).where(BacktestRun.id == run.id).values(status="RUNNING")
        )
        session.commit()

        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                update(AlgorithmVersion)
                .where(AlgorithmVersion.id == version.id)
                .values(parameters={"changed": True})
            )
        session.rollback()
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(
                update(BacktestRun)
                .where(BacktestRun.id == run.id)
                .values(snapshot={"changed": True})
            )
        session.rollback()

        with Operations.context(MigrationContext.configure(session.connection())):
            migration.downgrade()
        session.commit()
        session.execute(
            update(BacktestRun)
            .where(BacktestRun.id == run.id)
            .values(snapshot={"downgraded": True})
        )
        session.commit()
