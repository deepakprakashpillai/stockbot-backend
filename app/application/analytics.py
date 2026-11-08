"""Persistence adapter for the pure completed-run analytics calculator."""

from __future__ import annotations

import hashlib
import logging
from datetime import date
from decimal import Decimal
from time import monotonic
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.db.models.backtests import BacktestRun, RunAnalytics, RunAnalyticsPeriod
from app.db.models.datasets import DatasetVersion
from app.domain.run_analytics import ClosedOutcome, calculate_run_analytics

logger = logging.getLogger(__name__)


def _json_safe(value: object) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def persist_completed_run_analytics(
    session: Session, run: BacktestRun, dataset: DatasetVersion
) -> RunAnalytics:
    """Rebuild analytics deterministically; safe for old-run backfills and retries."""

    trades = list(run.trades)
    outcomes = [
        ClosedOutcome(
            sequence=item.sequence,
            entry_timestamp=item.entry_timestamp,
            exit_timestamp=item.exit_timestamp,
            gross=item.gross_demo_units,
            costs=item.costs_demo_units,
            net=item.net_demo_units,
        )
        for item in trades
    ]
    snapshot = getattr(run, "snapshot", None)
    coverage = snapshot.get("coverage") if isinstance(snapshot, dict) else None
    included = coverage.get("included_sessions") if isinstance(coverage, dict) else None
    sessions = (
        [date.fromisoformat(str(item)) for item in included]
        if isinstance(included, list) and included
        else [date.fromisoformat(item) for item in dataset.session_dates]
    )
    result = calculate_run_analytics(
        outcomes,
        sessions,
        start_date=run.start_date,
        end_date=run.end_date,
        timezone=dataset.timezone or "Asia/Kolkata",
    )
    fingerprint = hashlib.sha256(result.source_fingerprint.encode()).hexdigest()
    session.execute(delete(RunAnalyticsPeriod).where(RunAnalyticsPeriod.run_id == run.id))
    current = session.get(RunAnalytics, run.id)
    if current is None:
        current = RunAnalytics(
            run_id=run.id,
            schema_version=str(result.summary["schema_version"]),
            calculator_version=str(result.summary["calculator_version"]),
            source_fingerprint=fingerprint,
            timezone=dataset.timezone or "Asia/Kolkata",
            summary=_json_safe(result.summary),
        )
        session.add(current)
    else:
        current.schema_version = str(result.summary["schema_version"])
        current.calculator_version = str(result.summary["calculator_version"])
        current.source_fingerprint = fingerprint
        current.timezone = dataset.timezone or "Asia/Kolkata"
        current.summary = _json_safe(result.summary)
    for period in (*result.daily, *result.monthly):
        session.add(
            RunAnalyticsPeriod(
                run_id=run.id,
                granularity=period.granularity,
                period_start=period.period_start,
                period_end=period.period_end,
                partial=period.partial,
                eligible_session_count=period.eligible_session_count,
                trade_count=period.trade_count,
                wins=period.wins,
                losses=period.losses,
                breakevens=period.breakevens,
                gross_demo_units=period.gross,
                costs_demo_units=period.costs,
                net_demo_units=period.net,
                cumulative_net_demo_units=period.cumulative_net,
                equity_demo_units=period.equity_end,
                local_max_drawdown_demo_units=period.local_max_drawdown,
                run_drawdown_demo_units=period.run_drawdown_end,
            )
        )
    return current


def backfill_completed_run_analytics(session: Session, *, limit: int = 100) -> int:
    """Persist a bounded, deterministic analytics rebuild for completed runs.

    Callers own the transaction boundary so operational tooling can commit every
    small batch and resume safely after an interruption. Rebuilding is
    idempotent: immutable run inputs and trades are only read.
    """

    statement = (
        select(BacktestRun)
        .where(BacktestRun.status == "COMPLETED")
        .options(selectinload(BacktestRun.trades), selectinload(BacktestRun.dataset_version))
        .order_by(BacktestRun.created_at, BacktestRun.id)
        .limit(limit)
    )
    runs = list(session.scalars(statement))
    started = monotonic()
    logger.info("completed-run analytics backfill started", extra={"run_count": len(runs)})
    try:
        for index, run in enumerate(runs, start=1):
            persist_completed_run_analytics(session, run, run.dataset_version)
            logger.info(
                "completed-run analytics backfill progress",
                extra={"processed": index, "total": len(runs)},
            )
        session.flush()
    except Exception:
        logger.exception(
            "completed-run analytics backfill failed",
            extra={"processed": index if "index" in locals() else 0, "total": len(runs)},
        )
        raise
    logger.info(
        "completed-run analytics backfill completed",
        extra={"processed": len(runs), "duration_ms": round((monotonic() - started) * 1000, 3)},
    )
    return len(runs)


def analytics_summary_payload(analytics: RunAnalytics) -> dict[str, Any]:
    return {
        "schema_version": analytics.schema_version,
        "calculator_version": analytics.calculator_version,
        "source_fingerprint": analytics.source_fingerprint,
        "timezone": analytics.timezone,
        "summary": analytics.summary,
    }
