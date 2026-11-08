#!/usr/bin/env python3
"""Backfill versioned analytics for completed platform-demo backtests.

The default is a dry run. Applying the command reads immutable run inputs and
trades, then deterministically replaces only derived analytics rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.application.analytics import persist_completed_run_analytics  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models.backtests import BacktestRun, RunAnalytics  # noqa: E402
from app.db.session import create_session_factory  # noqa: E402

CONFIRMATION = "BACKFILL_RUN_ANALYTICS"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="Persist derived analytics rows.")
    parser.add_argument("--confirm", help=f"Required with --apply: {CONFIRMATION}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.batch_size < 1 or args.batch_size > 1_000:
        raise SystemExit("--batch-size must be between 1 and 1000")
    if args.apply and args.confirm != CONFIRMATION:
        raise SystemExit(f"--apply requires --confirm {CONFIRMATION}")

    session = create_session_factory(get_settings())()
    try:
        total = (
            session.scalar(
                select(func.count())
                .select_from(BacktestRun)
                .where(BacktestRun.status == "COMPLETED")
            )
            or 0
        )
        existing = session.scalar(select(func.count()).select_from(RunAnalytics)) or 0
        report = {
            "mode": "apply" if args.apply else "dry_run",
            "completed_runs": total,
            "existing_analytics": existing,
        }
        if not args.apply:
            print(json.dumps(report, sort_keys=True))
            return 0

        processed = 0
        offset = 0
        while offset < total:
            statement = (
                select(BacktestRun)
                .where(BacktestRun.status == "COMPLETED")
                .order_by(BacktestRun.created_at, BacktestRun.id)
                .offset(offset)
                .limit(args.batch_size)
            )
            runs = list(session.scalars(statement))
            if not runs:
                break
            for run in runs:
                session.refresh(run, attribute_names=["trades", "dataset_version"])
                persist_completed_run_analytics(session, run, run.dataset_version)
            session.commit()
            processed += len(runs)
            offset += len(runs)
        print(json.dumps(report | {"processed": processed}, sort_keys=True))
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
