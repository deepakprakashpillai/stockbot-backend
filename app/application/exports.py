"""Complete, asynchronous, checksummed backtest evidence exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.application.jobs import enqueue_job
from app.application.log_workbook import build_log_workbook
from app.application.research_limitations import display_research_limitations
from app.core.config import Settings
from app.db.models.backtests import (
    BacktestExportJob,
    BacktestRun,
    EquityPoint,
    JournalEvent,
    RunAnalytics,
    RunAnalyticsPeriod,
    RunDayClassification,
    RunEvent,
    SignalEvaluation,
    Trade,
)
from app.db.repositories.backtests import BacktestRepository
from app.infrastructure.datasets.derivatives_bundle import sha256_file


class ExportServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _json_bytes(value: object) -> bytes:
    return (json.dumps(jsonable_encoder(value), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _jsonl_bytes(values: list[dict[str, Any]]) -> bytes:
    return (
        "\n".join(json.dumps(jsonable_encoder(value), sort_keys=True) for value in values) + "\n"
    ).encode("utf-8")


def _csv_bytes(values: list[dict[str, Any]]) -> bytes:
    if not values:
        return b""
    output = io.StringIO(newline="")
    columns = list(values[0])
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for value in values:
        writer.writerow(
            {
                key: json.dumps(jsonable_encoder(item), sort_keys=True)
                if isinstance(item, (dict, list))
                else item
                for key, item in value.items()
            }
        )
    return output.getvalue().encode("utf-8")


def _jsonl_chunks(values: Iterable[dict[str, Any]]) -> Iterator[bytes]:
    emitted = False
    for value in values:
        if emitted:
            yield b"\n"
        yield json.dumps(jsonable_encoder(value), sort_keys=True).encode("utf-8")
        emitted = True
    yield b"\n"


def _json_array_chunks(
    values: Iterable[dict[str, Any]], *, indent: int = 0, trailing_newline: bool = True
) -> Iterator[bytes]:
    emitted = False
    item_indent = " " * (indent + 2)
    for value in values:
        if emitted:
            yield b",\n"
        else:
            yield b"[\n"
        item = json.dumps(jsonable_encoder(value), indent=2, sort_keys=True)
        yield (item_indent + item.replace("\n", "\n" + item_indent)).encode("utf-8")
        emitted = True
    ending = "\n" + " " * indent + "]" if emitted else "[]"
    yield (ending + ("\n" if trailing_newline else "")).encode("utf-8")


def _json_with_periods_chunks(
    value: dict[str, Any], periods: Iterable[dict[str, Any]]
) -> Iterator[bytes]:
    encoded = _json_bytes({**value, "periods": []})
    marker = b'  "periods": []'
    before, after = encoded.split(marker, 1)
    yield before + b'  "periods": '
    yield from _json_array_chunks(periods, indent=2, trailing_newline=False)
    yield after


def _csv_chunks(values: Iterable[dict[str, Any]]) -> Iterator[bytes]:
    output = io.StringIO(newline="")
    writer: csv.DictWriter[str] | None = None
    for value in values:
        if writer is None:
            writer = csv.DictWriter(output, fieldnames=list(value), extrasaction="ignore")
            writer.writeheader()
        writer.writerow(
            {
                key: json.dumps(jsonable_encoder(item), sort_keys=True)
                if isinstance(item, (dict, list))
                else item
                for key, item in value.items()
            }
        )
        yield output.getvalue().encode("utf-8")
        output.seek(0)
        output.truncate(0)


def _write_stream_member(
    archive: zipfile.ZipFile, name: str, chunks: Iterable[bytes]
) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(name, "w") as member:
        for chunk in chunks:
            member.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


class ReplayableRows:
    """Re-execute a bounded database read for each workbook evidence pass."""

    def __init__(self, read: Callable[[], Iterator[dict[str, Any]]]) -> None:
        self.read = read

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.read()


class BacktestExportService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.runs = BacktestRepository(session)

    def create(self, run_id: UUID, *, format: str = "ZIP") -> BacktestExportJob:
        if format not in {"ZIP", "XLSX"}:
            raise ExportServiceError("EXPORT_FORMAT_INVALID", "Unsupported export format.", 422)
        run = self.runs.get_run(run_id)
        if run is None:
            raise ExportServiceError("BACKTEST_NOT_FOUND", "The backtest does not exist.", 404)
        if run.status != "COMPLETED":
            raise ExportServiceError(
                "EXPORT_RUN_NOT_COMPLETED", "Only completed runs can be exported.", 409
            )
        job = self.runs.create_export_job(run, format=format)
        enqueue_job(self.session, "EXPORT", job.id)
        self.session.commit()
        return job

    def build(self, job_id: UUID) -> BacktestExportJob:
        job = self.runs.get_export_job(job_id, lock=True)
        if job is None:
            raise ExportServiceError("EXPORT_JOB_NOT_FOUND", "The export job does not exist.", 404)
        if job.status == "COMPLETED":
            return job
        if (
            job.status == "RUNNING"
            and job.lease_expires_at
            and job.lease_expires_at > datetime.now(UTC)
        ):
            return job
        owner = str(uuid4())
        job.lease_owner = owner
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        job.status = "RUNNING"
        job.error_code = None
        job.error_message = None
        job.completed_at = None
        self.session.commit()

        def heartbeat(*, commit: bool = True) -> None:
            current = self.runs.get_export_job(job_id, lock=True)
            if current is None or current.lease_owner != owner:
                raise RuntimeError("Export worker ownership was lost.")
            current.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
            if commit:
                self.session.commit()

        temporary: Path | None = None
        try:
            run = self.session.get(BacktestRun, job.run_id)
            if run is None or run.status != "COMPLETED":
                raise ValueError("The completed run is no longer available.")
            signal_query = (
                select(SignalEvaluation)
                .where(SignalEvaluation.run_id == run.id)
                .order_by(SignalEvaluation.sequence)
            )
            trade_query = (
                select(Trade)
                .options(selectinload(Trade.legs))
                .where(Trade.run_id == run.id)
                .order_by(Trade.sequence)
            )
            journal_query = (
                select(JournalEvent)
                .where(
                    JournalEvent.run_id == run.id,
                    JournalEvent.attempt_id == job.attempt_id,
                )
                .order_by(JournalEvent.timestamp, JournalEvent.sequence)
                if job.attempt_id
                else None
            )
            day_query = (
                select(RunDayClassification)
                .where(
                    RunDayClassification.run_id == run.id,
                    RunDayClassification.attempt_id == job.attempt_id,
                )
                .order_by(RunDayClassification.session_date)
                if job.attempt_id
                else None
            )
            event_query = (
                select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.sequence)
            )
            analytics = self.session.get(RunAnalytics, run.id)
            analytics_period_query = (
                select(RunAnalyticsPeriod)
                .where(RunAnalyticsPeriod.run_id == run.id)
                .order_by(
                    RunAnalyticsPeriod.granularity,
                    RunAnalyticsPeriod.period_start,
                )
            )
            equity_query = (
                select(EquityPoint)
                .where(EquityPoint.run_id == run.id)
                .order_by(EquityPoint.sequence)
            )
            heartbeat()

            def signal_row(item: SignalEvaluation) -> dict[str, Any]:
                return {
                    "id": item.id,
                    "sequence": item.sequence,
                    "timestamp": item.timestamp,
                    "session_date": item.session_date,
                    "decision": item.decision,
                    "rejection_reason": item.rejection_reason,
                    "window": item.window,
                    "score": item.score,
                    "inputs": item.inputs,
                    "diagnostic": item.diagnostic,
                }

            def trade_row(item: Trade) -> dict[str, Any]:
                return {
                    "id": item.id,
                    "sequence": item.sequence,
                    "entry_signal_evaluation_id": item.entry_signal_evaluation_id,
                    "entry_timestamp": item.entry_timestamp,
                    "exit_timestamp": item.exit_timestamp,
                    "exit_reason": item.exit_reason,
                    "quantity": item.quantity,
                    "entry_reference": item.entry_reference,
                    "exit_reference": item.exit_reference,
                    "gross_outcome": item.gross_demo_units,
                    "costs": item.costs_demo_units,
                    "net_outcome": item.net_demo_units,
                    "unit": item.unit,
                    "assumptions": item.assumptions,
                    "legs": [
                        {
                            "leg_number": leg.leg_number,
                            "name": leg.name,
                            "entry_reference": leg.entry_reference,
                            "exit_reference": leg.exit_reference,
                            "gross_outcome": leg.demo_units,
                            "unit": leg.unit,
                        }
                        for leg in item.legs
                    ],
                }

            def leg_row(trade: Trade, leg: Any) -> dict[str, Any]:
                return {
                    "trade_sequence": trade.sequence,
                    "leg_number": leg.leg_number,
                    "name": leg.name,
                    "entry_reference": leg.entry_reference,
                    "exit_reference": leg.exit_reference,
                    "gross_outcome": leg.demo_units,
                    "synthetic": leg.synthetic,
                    "unit": leg.unit,
                    "financial_pnl_available": leg.financial_pnl_available,
                }

            def iter_trade_rows() -> Iterator[dict[str, Any]]:
                for item in self.session.scalars(trade_query.execution_options(yield_per=500)):
                    yield trade_row(item)

            def iter_leg_rows() -> Iterator[dict[str, Any]]:
                for trade in self.session.scalars(trade_query.execution_options(yield_per=500)):
                    for leg in trade.legs:
                        yield leg_row(trade, leg)

            trade_rows: Iterable[dict[str, Any]] = ReplayableRows(iter_trade_rows)

            def journal_row(item: JournalEvent) -> dict[str, Any]:
                return {
                    "sequence": item.sequence,
                    "timestamp": item.timestamp,
                    "session_date": item.session_date,
                    "event_type": item.event_type,
                    "position_state": item.position_state,
                    "signal_sequence": item.signal_sequence,
                    "trade_sequence": item.trade_sequence,
                    "payload": item.payload,
                }

            def iter_signal_rows() -> Iterator[dict[str, Any]]:
                for item in self.session.scalars(signal_query.execution_options(yield_per=500)):
                    yield signal_row(item)

            def iter_journal_rows() -> Iterator[dict[str, Any]]:
                if journal_query is None:
                    return
                for item in self.session.scalars(journal_query.execution_options(yield_per=500)):
                    yield journal_row(item)

            signal_rows: Iterable[dict[str, Any]] = (
                ReplayableRows(iter_signal_rows) if job.format == "XLSX" else []
            )
            journal_rows: Iterable[dict[str, Any]] = (
                ReplayableRows(iter_journal_rows) if job.format == "XLSX" else []
            )

            def day_row(item: RunDayClassification) -> dict[str, Any]:
                return {
                    "session_date": item.session_date,
                    "classification": item.classification,
                    "included": item.included,
                    "primary_reason": item.primary_reason,
                    "evidence": item.evidence,
                }

            def iter_day_rows() -> Iterator[dict[str, Any]]:
                if day_query is None:
                    return
                for item in self.session.scalars(day_query.execution_options(yield_per=500)):
                    yield day_row(item)

            def iter_exclusion_rows() -> Iterator[dict[str, Any]]:
                for row in iter_day_rows():
                    if not row["included"]:
                        yield row

            day_rows: Iterable[dict[str, Any]] = ReplayableRows(iter_day_rows)

            def event_row(item: RunEvent) -> dict[str, Any]:
                return {
                    "sequence": item.sequence,
                    "event_type": item.event_type,
                    "payload": item.payload,
                    "created_at": item.created_at,
                }

            def equity_row(item: EquityPoint) -> dict[str, Any]:
                return {
                    "sequence": item.sequence,
                    "timestamp": item.timestamp,
                    "equity": item.equity_demo_units,
                    "drawdown": item.drawdown_demo_units,
                    "synthetic": item.synthetic,
                    "unit": item.unit,
                    "financial_pnl_available": item.financial_pnl_available,
                }

            def analytics_period_row(item: RunAnalyticsPeriod) -> dict[str, Any]:
                return {
                    "granularity": item.granularity,
                    "period_start": item.period_start,
                    "period_end": item.period_end,
                    "partial": item.partial,
                    "eligible_session_count": item.eligible_session_count,
                    "trade_count": item.trade_count,
                    "wins": item.wins,
                    "losses": item.losses,
                    "breakevens": item.breakevens,
                    "gross_outcome": item.gross_demo_units,
                    "costs": item.costs_demo_units,
                    "net_outcome": item.net_demo_units,
                    "cumulative_net_outcome": item.cumulative_net_demo_units,
                    "equity": item.equity_demo_units,
                    "local_max_drawdown": item.local_max_drawdown_demo_units,
                    "run_drawdown": item.run_drawdown_demo_units,
                }

            def iter_event_rows() -> Iterator[dict[str, Any]]:
                for item in self.session.scalars(event_query.execution_options(yield_per=500)):
                    yield event_row(item)

            def iter_equity_rows() -> Iterator[dict[str, Any]]:
                for item in self.session.scalars(equity_query.execution_options(yield_per=500)):
                    yield equity_row(item)

            def iter_analytics_period_rows() -> Iterator[dict[str, Any]]:
                for item in self.session.scalars(
                    analytics_period_query.execution_options(yield_per=500)
                ):
                    yield analytics_period_row(item)

            event_rows: Iterable[dict[str, Any]] = ReplayableRows(iter_event_rows)
            equity_rows: Iterable[dict[str, Any]] = ReplayableRows(iter_equity_rows)
            analytics_period_rows: Iterable[dict[str, Any]] = ReplayableRows(
                iter_analytics_period_rows
            )
            summary_row = {
                "run_id": run.id,
                "status": run.status,
                "mode": run.mode,
                "start_date": run.start_date,
                "end_date": run.end_date,
                "starting_capital": run.starting_capital,
                "snapshot": run.snapshot,
                "analytics": analytics.summary if analytics else None,
            }
            if job.format == "XLSX":
                signal_count = int(
                    self.session.scalar(
                        select(func.count())
                        .select_from(SignalEvaluation)
                        .where(SignalEvaluation.run_id == run.id)
                    )
                    or 0
                )
                trade_count = int(
                    self.session.scalar(
                        select(func.count()).select_from(Trade).where(Trade.run_id == run.id)
                    )
                    or 0
                )
                journal_count = (
                    int(
                        self.session.scalar(
                            select(func.count())
                            .select_from(JournalEvent)
                            .where(
                                JournalEvent.run_id == run.id,
                                JournalEvent.attempt_id == job.attempt_id,
                            )
                        )
                        or 0
                    )
                    if job.attempt_id
                    else 0
                )
                day_count = (
                    int(
                        self.session.scalar(
                            select(func.count())
                            .select_from(RunDayClassification)
                            .where(
                                RunDayClassification.run_id == run.id,
                                RunDayClassification.attempt_id == job.attempt_id,
                            )
                        )
                        or 0
                    )
                    if job.attempt_id
                    else 0
                )
                event_count = int(
                    self.session.scalar(
                        select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run.id)
                    )
                    or 0
                )
                equity_count = int(
                    self.session.scalar(
                        select(func.count())
                        .select_from(EquityPoint)
                        .where(EquityPoint.run_id == run.id)
                    )
                    or 0
                )
                analytics_count = int(
                    self.session.scalar(
                        select(func.count())
                        .select_from(RunAnalyticsPeriod)
                        .where(RunAnalyticsPeriod.run_id == run.id)
                    )
                    or 0
                )
                relative = Path("exports") / str(run.id) / f"{job.id}.xlsx"
                destination = (self.settings.storage_root / relative).resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(f".{job.id}.{owner}.tmp")
                workbook_counts = build_log_workbook(
                    temporary,
                    summary=summary_row,
                    attempt_id=job.attempt_id,
                    signals=signal_rows,
                    trades=trade_rows,
                    journal=journal_rows,
                    days=day_rows,
                    events=event_rows,
                    equity=equity_rows,
                    analytics=analytics_period_rows,
                    signal_count=signal_count,
                    trade_count=trade_count,
                    journal_count=journal_count,
                    day_count=day_count,
                    event_count=event_count,
                    equity_count=equity_count,
                    analytics_count=analytics_count,
                )
                heartbeat(commit=False)
                temporary.replace(destination)
                job.row_counts = workbook_counts
                job.status = "COMPLETED"
                job.artifact_path = relative.as_posix()
                job.sha256 = sha256_file(destination)
                job.lease_owner = None
                job.lease_expires_at = None
                job.completed_at = datetime.now(UTC)
                self.session.commit()
                return job

            def archive_members() -> Iterator[tuple[str, bytes | None]]:
                yield "summary.json", _json_bytes(summary_row)
                yield "summary.csv", _csv_bytes([summary_row])
                yield "trades.json", None
                yield "trades.csv", None
                yield "legs.json", None
                yield "legs.csv", None
                yield "signals.jsonl", None
                yield "signals.csv", None
                yield "journal.jsonl", None
                yield "journal.csv", None
                yield "days.json", None
                yield "days.csv", None
                yield "exclusions.json", None
                yield "exclusions.csv", None
                yield "equity.json", None
                yield "equity.csv", None
                yield "analytics.json", None
                yield "analytics_periods.json", None
                yield "analytics_periods.csv", None
                yield "events.jsonl", None
                yield "events.csv", None

            assumptions = run.snapshot.get("assumptions") if isinstance(run.snapshot, dict) else {}
            assumptions = assumptions if isinstance(assumptions, dict) else {}
            dataset_snapshot = run.snapshot.get("dataset") if isinstance(run.snapshot, dict) else {}
            dataset_snapshot = dataset_snapshot if isinstance(dataset_snapshot, dict) else {}
            algorithm_snapshot = (
                run.snapshot.get("algorithm") if isinstance(run.snapshot, dict) else {}
            )
            algorithm_snapshot = algorithm_snapshot if isinstance(algorithm_snapshot, dict) else {}
            option_execution = assumptions.get("option_execution")
            option_execution = option_execution if isinstance(option_execution, dict) else {}
            dataset_profile = dataset_snapshot.get("profile")
            dataset_profile = dataset_profile if isinstance(dataset_profile, dict) else {}
            row_counts = {
                "trades": 0,
                "legs": 0,
                "signals": 0,
                "journal": 0,
                "days": 0,
                "exclusions": 0,
                "equity": 0,
                "analytics_periods": 0,
                "events": 0,
            }

            def counted_rows(
                key: str, values: Iterable[dict[str, Any]]
            ) -> Iterator[dict[str, Any]]:
                for value in values:
                    row_counts[key] += 1
                    yield value

            file_identity: dict[str, dict[str, object]] = {}
            manifest = {
                "schema_version": "stockbot.backtest-export.v3",
                "exported_at": datetime.now(UTC),
                "run_id": run.id,
                "attempt_id": job.attempt_id,
                "mode": run.mode,
                "synthetic": bool(assumptions.get("synthetic", run.mode == "PLATFORM_DEMO")),
                "unit": assumptions.get("unit"),
                "financial_pnl_available": bool(assumptions.get("financial_pnl_available", False)),
                "executable_quotes": bool(assumptions.get("executable_quotes", False)),
                "pricing_quality": assumptions.get("pricing_quality"),
                "pricing_model_version": option_execution.get("adapter_version"),
                "estimate_limitations": display_research_limitations(option_execution),
                "financial_validity": {
                    "financial_pnl_available": bool(
                        assumptions.get("financial_pnl_available", False)
                    ),
                    "executable_quotes": bool(assumptions.get("executable_quotes", False)),
                    "estimated": assumptions.get("pricing_quality") == "estimated_historical_ohlc",
                },
                "dataset_version_id": run.dataset_version_id,
                "algorithm_version_id": run.algorithm_version_id,
                "dataset_version": {
                    "id": dataset_snapshot.get("id", run.dataset_version_id),
                    "checksum": dataset_snapshot.get("checksum"),
                    "bundle_checksum": dataset_snapshot.get("derivatives_checksum")
                    or dataset_profile.get("bundle_checksum"),
                    "schema_profile": dataset_snapshot.get("schema_profile"),
                    "bundle_schema_version": dataset_profile.get("bundle_schema_version"),
                },
                "algorithm_version": {
                    "id": algorithm_snapshot.get("version_id", run.algorithm_version_id),
                    "key": algorithm_snapshot.get("key"),
                    "code_version": algorithm_snapshot.get("code_version"),
                    "version_number": algorithm_snapshot.get("version_number"),
                    "schema_version": algorithm_snapshot.get("schema_version"),
                    "rule_register_version": algorithm_snapshot.get("rule_register_version"),
                },
                "engine_version": run.snapshot.get("engine_version"),
                "coverage": run.snapshot.get("coverage"),
                "row_counts": row_counts,
                "files": file_identity,
            }
            relative = Path("exports") / str(run.id) / f"{job.id}.zip"
            destination = (self.settings.storage_root / relative).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{job.id}.{owner}.tmp")
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in archive_members():
                    heartbeat()
                    if content is None:
                        if name == "analytics.json":
                            analytics_payload = {
                                "schema_version": analytics.schema_version if analytics else None,
                                "calculator_version": analytics.calculator_version
                                if analytics
                                else None,
                                "source_fingerprint": analytics.source_fingerprint
                                if analytics
                                else None,
                                "timezone": analytics.timezone if analytics else None,
                                "summary": analytics.summary if analytics else None,
                            }
                            chunks = _json_with_periods_chunks(
                                analytics_payload, iter_analytics_period_rows()
                            )
                            file_identity[name] = _write_stream_member(archive, name, chunks)
                            continue
                        if name.startswith("trades."):
                            key, rows = "trades", iter_trade_rows()
                        elif name.startswith("legs."):
                            key, rows = "legs", iter_leg_rows()
                        elif name.startswith("days."):
                            key, rows = "days", iter_day_rows()
                        elif name.startswith("exclusions."):
                            key, rows = "exclusions", iter_exclusion_rows()
                        elif name.startswith("analytics_periods."):
                            key, rows = "analytics_periods", iter_analytics_period_rows()
                        elif name.startswith("signals."):
                            key, rows = "signals", iter_signal_rows()
                        elif name.startswith("journal."):
                            key, rows = "journal", iter_journal_rows()
                        elif name.startswith("equity."):
                            key, rows = "equity", iter_equity_rows()
                        else:
                            key, rows = "events", iter_event_rows()
                        if name.endswith(".jsonl"):
                            rows = counted_rows(key, rows)
                            chunks = _jsonl_chunks(rows)
                        elif name.endswith(".json"):
                            rows = counted_rows(key, rows)
                            chunks = _json_array_chunks(rows)
                        else:
                            chunks = _csv_chunks(rows)
                        file_identity[name] = _write_stream_member(archive, name, chunks)
                    else:
                        archive.writestr(name, content)
                        file_identity[name] = {
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "bytes": len(content),
                        }
                heartbeat()
                archive.writestr("manifest.json", _json_bytes(manifest))
            heartbeat(commit=False)
            temporary.replace(destination)
            job.status = "COMPLETED"
            job.artifact_path = relative.as_posix()
            job.sha256 = sha256_file(destination)
            job.lease_owner = None
            job.lease_expires_at = None
            job.row_counts = row_counts
            job.completed_at = datetime.now(UTC)
            self.session.commit()
            return job
        except Exception as exc:
            self.session.rollback()
            job = self.runs.get_export_job(job_id, lock=True)
            if job is not None and job.lease_owner == owner:
                job.status = "FAILED"
                job.error_code = "EXPORT_BUILD_FAILED"
                job.error_message = str(exc)
                job.completed_at = datetime.now(UTC)
                job.lease_owner = None
                job.lease_expires_at = None
                self.session.commit()
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
