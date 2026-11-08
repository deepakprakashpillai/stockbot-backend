"""Measure local full-year worker persistence at one, two, and four processes.

Use only a disposable PostgreSQL test database. The immutable market bundle is
read without modification; run records are written to the test database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from time import perf_counter
from urllib.parse import urlsplit

from sqlalchemy import select

from app.application.algorithms import sync_registry
from app.application.backtests import BacktestService
from app.core.config import get_settings
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun
from app.db.session import create_session_factory
from app.infrastructure.datasets.storage import LocalDatasetStorage
from scripts.register_derivatives_bundle import register_bundle

WORKER = """
import json
import platform
import resource
import sys
from time import perf_counter
from uuid import UUID
from app.application.backtests import BacktestService
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.infrastructure.datasets.storage import LocalDatasetStorage

settings = get_settings()
began = perf_counter()
with create_session_factory(settings)() as session:
    result = BacktestService(session, LocalDatasetStorage(settings.storage_root), settings).execute(UUID(sys.argv[1]))
usage = resource.getrusage(resource.RUSAGE_SELF)
rss_mib = usage.ru_maxrss / (1024 * 1024 if platform.system() == 'Darwin' else 1024)
print(json.dumps({'run_id': sys.argv[1], 'status': result['status'],
                  'seconds': round(perf_counter() - began, 3),
                  'peak_rss_mib': round(rss_mib, 1),
                  'cpu_seconds': round(usage.ru_utime + usage.ru_stime, 3)}))
"""


def main() -> None:
    settings = get_settings()
    database = urlsplit(settings.database_url).path.rsplit("/", 1)[-1]
    if database != "stockbot_test":
        raise SystemExit("Refusing to benchmark outside a stockbot_test database.")
    bundle = settings.storage_root / "datasets/upstox_nifty_derivatives_v6"
    if not bundle.joinpath("bundle_manifest.json").exists():
        raise SystemExit(f"Immutable benchmark bundle is missing: {bundle}")
    start, end = date(2025, 7, 21), date(2026, 7, 17)
    storage = LocalDatasetStorage(settings.storage_root)
    dataset = register_bundle(bundle, bundle / "spot/nifty_spot_5min.csv", "Worker scale fixture")
    factory = create_session_factory(settings)
    with factory() as session:
        sync_registry(session)
        algorithm = session.scalar(
            select(AlgorithmVersion).where(
                AlgorithmVersion.algorithm_key == "bull_call_spread",
                AlgorithmVersion.code_version == "1.2.0-bull-call-spread-v1",
            )
        )
        if algorithm is None:
            raise SystemExit("The historical Bull Call Spread version is missing.")
        algorithm_id = algorithm.id

    results: list[dict[str, object]] = []
    for count in (1, 2, 4):
        run_ids = []
        with factory() as session:
            service = BacktestService(session, storage, settings)
            preview = service.preflight(dataset.id, algorithm_id, start, end, True)
            digest = preview.get("coverage_digest")
            for index in range(count):
                run, _ = service.create(
                    dataset_version_id=dataset.id,
                    algorithm_version_id=algorithm_id,
                    start_date=start,
                    end_date=end,
                    starting_capital=Decimal("500000"),
                    warning_acknowledged=True,
                    accepted_coverage_digest=str(digest) if digest else None,
                    idempotency_key=f"worker-scale-{count}-{index}",
                )
                run_ids.append(run.id)
        began = perf_counter()
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", WORKER, str(run_id)],
                env=os.environ.copy(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for run_id in run_ids
        ]
        children = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=300)
            if process.returncode != 0:
                raise RuntimeError(f"Worker failed: {stderr[-3000:]}")
            children.append(json.loads(stdout.strip().splitlines()[-1]))
        wall = perf_counter() - began
        with factory() as session:
            statuses = list(
                session.scalars(select(BacktestRun.status).where(BacktestRun.id.in_(run_ids)))
            )
        if statuses != ["COMPLETED"] * count:
            raise RuntimeError(f"Unexpected persisted statuses: {statuses}")
        results.append(
            {
                "workers": count,
                "wall_seconds": round(wall, 3),
                "summed_peak_rss_mib": round(sum(child["peak_rss_mib"] for child in children), 1),
                "children": children,
            }
        )
    print(json.dumps({"kind": "local_worker_persistence", "results": results}, indent=2))


if __name__ == "__main__":
    main()
