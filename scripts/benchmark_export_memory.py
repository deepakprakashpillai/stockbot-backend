"""Measure export Python allocation against synthetic saved-evidence row counts.

Requires STOCKBOT_TEST_DATABASE_URL pointing to a disposable PostgreSQL database.
Each invocation creates and drops its own schema. Python allocation is measured
around export; process RSS includes setup and is a coarse upper bound.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import resource
import shutil
import tempfile
import tracemalloc
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.application.algorithms import sync_registry
from app.application.backtests import BacktestService
from app.application.exports import BacktestExportService
from app.db.base import Base
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import SignalEvaluation
from app.db.models.datasets import Dataset
from app.infrastructure.datasets.storage import LocalDatasetStorage
from tests.conftest import make_test_settings
from tests.test_backtests import dataset


def benchmark(database_url: str, rows: int, format: str) -> dict[str, object]:
    schema = "stockbot_export_bench_" + uuid4().hex
    admin = create_engine(database_url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
        with tempfile.TemporaryDirectory(prefix="stockbot-export-benchmark-") as directory:
            root = Path(directory)
            fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/platform_demo_nifty.csv"
            shutil.copyfile(fixture, root / "fixture.csv")
            settings = make_test_settings().model_copy(update={"storage_root": root})
            storage = LocalDatasetStorage(root)
            with factory() as session:
                sync_registry(session)
                configured = session.scalar(
                    select(AlgorithmVersion).where(
                        AlgorithmVersion.algorithm_key == "platform_demo"
                    )
                )
                assert configured is not None
                stored = dataset()
                stored.checksum = hashlib.sha256(fixture.read_bytes()).hexdigest()
                stored.coverage_start = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
                stored.coverage_end = datetime.fromisoformat("2026-01-02T11:50:00+05:30")
                session.add(Dataset(id=stored.dataset_id, name="Export memory benchmark"))
                session.flush()
                session.add(stored)
                session.commit()
                service = BacktestService(session, storage, settings)
                run, _ = service.create(
                    dataset_version_id=stored.id,
                    algorithm_version_id=configured.id,
                    start_date=date(2026, 1, 2),
                    end_date=date(2026, 1, 2),
                    starting_capital=Decimal("100000"),
                    warning_acknowledged=False,
                    idempotency_key="export-memory-benchmark",
                )
                assert service.execute(run.id)["status"] == "COMPLETED"
                existing = session.scalar(
                    select(SignalEvaluation)
                    .where(SignalEvaluation.run_id == run.id)
                    .order_by(SignalEvaluation.sequence.desc())
                )
                assert existing is not None
                first_sequence = existing.sequence + 1
                for offset in range(0, rows, 500):
                    batch = [
                        {
                            "id": uuid4(),
                            "run_id": run.id,
                            "sequence": first_sequence + index,
                            "timestamp": existing.timestamp + timedelta(minutes=5 * index),
                            "session_date": existing.session_date,
                            "decision": "REJECTED",
                            "rejection_reason": "BENCHMARK_ONLY",
                            "window": None,
                            "score": 0,
                            "inputs": {"benchmark": True, "index": index},
                            "diagnostic": None,
                        }
                        for index in range(offset, min(offset + 500, rows))
                    ]
                    session.execute(insert(SignalEvaluation), batch)
                session.commit()
                gc.collect()
                tracemalloc.start()
                try:
                    exports = BacktestExportService(session, settings)
                    job = exports.build(exports.create(run.id, format=format).id)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                artifact = root / str(job.artifact_path)
                max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                peak_rss_mib = max_rss / (1024**2 if platform.system() == "Darwin" else 1024)
                return {
                    "format": format,
                    "synthetic_extra_signals": rows,
                    "exported_signals": job.row_counts["signals"],
                    "peak_python_allocation_mib": round(peak / 1024**2, 2),
                    "peak_process_rss_mib_including_setup": round(peak_rss_mib, 1),
                    "artifact_mib": round(artifact.stat().st_size / 1024**2, 2),
                }
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra-signals", type=int, required=True)
    parser.add_argument("--format", choices=["ZIP", "XLSX"], required=True)
    args = parser.parse_args()
    if args.extra_signals < 0:
        parser.error("--extra-signals must be nonnegative")
    url = os.environ.get("STOCKBOT_TEST_DATABASE_URL")
    if not url:
        parser.error("Set STOCKBOT_TEST_DATABASE_URL to a disposable PostgreSQL database.")
    print(json.dumps(benchmark(url, args.extra_signals, args.format), indent=2))
