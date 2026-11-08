#!/usr/bin/env python3
"""Measure database-backed completed-run analytics reads on a disposable fixture.

The command creates one temporary PLATFORM_DEMO run using existing immutable
dataset and algorithm references, inserts synthetic closed trades, finalizes
analytics, measures fresh-session endpoint reads, and deletes that exact run in
``finally``. It never changes existing runs, datasets, algorithms, or trades.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, insert, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.api.v1.backtests import get_analytics_summary, list_analytics_periods  # noqa: E402
from app.application.analytics import persist_completed_run_analytics  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models.backtests import BacktestRun, Trade  # noqa: E402
from app.db.session import create_session_factory  # noqa: E402

CONFIRMATION = "RUN_ANALYTICS_BENCHMARK"


def _request():
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "analytics-benchmark"
    return request


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"--confirm must equal {CONFIRMATION}")
    if args.trades < 100_000:
        raise SystemExit("--trades must be at least 100000 for the Phase 10 acceptance gate")
    if args.iterations < 5:
        raise SystemExit("--iterations must be at least 5")
    return args


def _insert_fixture_trades(session, run_id: UUID, start: datetime, count: int) -> None:
    batch_size = 5_000
    for offset in range(0, count, batch_size):
        rows = []
        for sequence in range(offset + 1, min(offset + batch_size, count) + 1):
            entry = start + timedelta(microseconds=(sequence - 1) * 10)
            exit_timestamp = entry + timedelta(microseconds=1)
            net = Decimal("1") if sequence % 2 else Decimal("-1")
            costs = Decimal("0.1")
            rows.append(
                {
                    "id": uuid4(),
                    "run_id": run_id,
                    "sequence": sequence,
                    "entry_timestamp": entry,
                    "exit_timestamp": exit_timestamp,
                    "exit_reason": "ANALYTICS_BENCHMARK",
                    "quantity": Decimal("1"),
                    "entry_reference": Decimal("100"),
                    "exit_reference": Decimal("100"),
                    "gross_demo_units": net + costs,
                    "costs_demo_units": costs,
                    "net_demo_units": net,
                    "synthetic": True,
                    "unit": "DEMO_UNITS",
                    "financial_pnl_available": False,
                    "assumptions": {"fixture": "analytics_benchmark"},
                }
            )
        session.execute(insert(Trade), rows)
        session.flush()


def main() -> int:
    args = _parse_args()
    factory = create_session_factory(get_settings())
    benchmark_run_id: UUID | None = None
    try:
        session = factory()
        try:
            template = session.scalar(
                select(BacktestRun)
                .where(BacktestRun.status == "COMPLETED")
                .order_by(BacktestRun.created_at)
                .limit(1)
            )
            if template is None:
                raise SystemExit(
                    "A completed PLATFORM_DEMO run is required as the benchmark template."
                )
            benchmark = BacktestRun(
                dataset_version_id=template.dataset_version_id,
                algorithm_version_id=template.algorithm_version_id,
                start_date=template.start_date,
                end_date=template.end_date,
                mode="PLATFORM_DEMO",
                starting_capital=template.starting_capital,
                snapshot=copy.deepcopy(template.snapshot),
                status="COMPLETED",
                progress=100,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(benchmark)
            session.flush()
            benchmark_run_id = benchmark.id
            zone = ZoneInfo("Asia/Kolkata")
            start = datetime.combine(benchmark.start_date, time(9, 15), tzinfo=zone).astimezone(UTC)
            _insert_fixture_trades(session, benchmark.id, start, args.trades)
            session.refresh(benchmark, attribute_names=["trades", "dataset_version"])
            persist_completed_run_analytics(session, benchmark, benchmark.dataset_version)
            session.commit()
        finally:
            session.close()

        summary_times: list[float] = []
        period_times: list[float] = []
        for _ in range(args.iterations):
            session = factory()
            try:
                started = perf_counter()
                summary = get_analytics_summary(benchmark_run_id, _request(), session)
                summary_times.append(perf_counter() - started)
                if getattr(summary, "summary", None) is None:
                    raise RuntimeError("Summary endpoint did not return analytics.")
                started = perf_counter()
                periods = list_analytics_periods(
                    benchmark_run_id,
                    _request(),
                    granularity="DAY",
                    purpose="table",
                    session=session,
                )
                period_times.append(perf_counter() - started)
                if getattr(periods, "items", None) is None:
                    raise RuntimeError("Period endpoint did not return analytics.")
            finally:
                session.close()
        report = {
            "run_id": str(benchmark_run_id),
            "trade_count": args.trades,
            "iterations": args.iterations,
            "summary_p95_ms": round(_p95(summary_times) * 1000, 2),
            "precomputed_day_p95_ms": round(_p95(period_times) * 1000, 2),
            "summary_target_ms": 200,
            "precomputed_period_target_ms": 300,
        }
        print(json.dumps(report, sort_keys=True))
        if report["summary_p95_ms"] > 200 or report["precomputed_day_p95_ms"] > 300:
            raise SystemExit("Analytics benchmark acceptance threshold failed.")
        return 0
    finally:
        if benchmark_run_id is not None:
            session = factory()
            try:
                session.execute(delete(BacktestRun).where(BacktestRun.id == benchmark_run_id))
                session.commit()
            finally:
                session.close()


if __name__ == "__main__":
    raise SystemExit(main())
