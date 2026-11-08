"""Opt-in real PostgreSQL journey for the bounded evidence export readers."""

import hashlib
import json
import shutil
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.application.algorithms import sync_registry
from app.application.backtests import BacktestService
from app.application.exports import BacktestExportService
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import JournalEvent, SignalEvaluation, Trade
from app.db.models.datasets import Dataset
from app.infrastructure.datasets.storage import LocalDatasetStorage
from tests.conftest import make_test_settings
from tests.test_backtests import dataset


def test_postgres_run_and_streamed_exports_reconcile(postgres_factory, tmp_path):
    fixture = Path(__file__).parent / "fixtures/platform_demo_nifty.csv"
    shutil.copyfile(fixture, tmp_path / "fixture.csv")
    settings = make_test_settings().model_copy(update={"storage_root": tmp_path})
    with postgres_factory() as session:
        sync_registry(session)
        configured = session.scalar(
            select(AlgorithmVersion).where(AlgorithmVersion.algorithm_key == "platform_demo")
        )
        assert configured is not None
        stored = dataset()
        stored.checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
        stored.coverage_start = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
        stored.coverage_end = datetime.fromisoformat("2026-01-02T11:50:00+05:30")
        session.add(Dataset(id=stored.dataset_id, name="PostgreSQL export fixture"))
        session.flush()
        session.add(stored)
        session.commit()
        service = BacktestService(session, LocalDatasetStorage(tmp_path), settings)
        run, _ = service.create(
            dataset_version_id=stored.id,
            algorithm_version_id=configured.id,
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 2),
            starting_capital=Decimal("100000"),
            warning_acknowledged=False,
            idempotency_key="postgres-export-fixture",
        )
        assert service.execute(run.id)["status"] == "COMPLETED"
        signals = list(
            session.scalars(select(SignalEvaluation).where(SignalEvaluation.run_id == run.id))
        )
        journal = list(session.scalars(select(JournalEvent).where(JournalEvent.run_id == run.id)))
        trades = list(session.scalars(select(Trade).where(Trade.run_id == run.id)))
        assert signals and journal and trades
        leg_count = sum(len(trade.legs) for trade in trades)
        exports = BacktestExportService(session, settings)
        zip_job = exports.build(exports.create(run.id, format="ZIP").id)
        xlsx_job = exports.build(exports.create(run.id, format="XLSX").id)

    with zipfile.ZipFile(tmp_path / zip_job.artifact_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["row_counts"]["signals"] == len(signals)
        assert manifest["row_counts"]["journal"] == len(journal)
        assert manifest["row_counts"]["trades"] == len(trades)
        assert manifest["row_counts"]["legs"] == leg_count
        assert len(json.loads(archive.read("trades.json"))) == len(trades)
        assert len(json.loads(archive.read("legs.json"))) == manifest["row_counts"]["legs"]
        assert len(archive.read("signals.jsonl").splitlines()) == len(signals)
        assert len(archive.read("journal.jsonl").splitlines()) == len(journal)
        for name, identity in manifest["files"].items():
            content = archive.read(name)
            assert identity == {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    assert xlsx_job.row_counts["signals"] == len(signals)
    assert xlsx_job.row_counts["journal"] == len(journal)
