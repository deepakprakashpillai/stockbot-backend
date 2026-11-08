"""Rebuild bounded batches of immutable completed-run analytics.

The operation only rewrites derived analytics rows from stored immutable runs
and trades. It never changes a run snapshot, signals, or synthetic trade data.
"""

from __future__ import annotations

import argparse

from app.application.analytics import backfill_completed_run_analytics
from app.core.config import get_settings
from app.db.session import create_session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum completed runs to rebuild in this idempotent batch (default: 100).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    session = create_session_factory(get_settings())()
    try:
        rebuilt = backfill_completed_run_analytics(session, limit=args.limit)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    print(f"Rebuilt analytics for {rebuilt} completed run(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
