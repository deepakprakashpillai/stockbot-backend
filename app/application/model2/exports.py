"""Complete, streamed scenario evidence through the existing export job lease."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import xlsxwriter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.application.model2.reads import assessment_data, position
from app.core.config import Settings
from app.db.models.backtests import (
    BacktestExportJob,
    BacktestRun,
    EquityPoint,
    JournalEvent,
    RunEvent,
    RunMetrics,
    Trade,
)
from app.db.models.model2 import (
    BacktestCycle,
    BacktestRiskEntry,
    ResearchScenarioGroup,
    ResearchScenarioMember,
)
from app.db.repositories.backtests import BacktestRepository


def encode(value: object) -> bytes:
    return (json.dumps(value, default=str, sort_keys=True, separators=(",", ":")) + "\n").encode()


def group_for_run(session: Session, run_id: object) -> ResearchScenarioGroup:
    member = session.get(ResearchScenarioMember, run_id)
    if member is None:
        raise ValueError("Model 2 run has no scenario group.")
    group = session.get(ResearchScenarioGroup, member.group_id)
    if group is None or group.status not in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise ValueError(
            "Wait for all scenario children to finish or stop before exporting the group."
        )
    return group


def tables(
    session: Session, group: ResearchScenarioGroup
) -> Iterator[tuple[str, Callable[[], Iterator[dict[str, Any]]]]]:
    members = list(
        session.scalars(
            select(ResearchScenarioMember)
            .where(ResearchScenarioMember.group_id == group.id)
            .order_by(ResearchScenarioMember.ordinal)
        )
    )

    def group_rows() -> Iterator[dict[str, Any]]:
        yield {
            "id": str(group.id),
            "status": group.status,
            "request_digest": group.request_digest,
            "snapshot": group.snapshot,
            "created_at": group.created_at,
            "completed_at": group.completed_at,
            "primary_policy": "MAX_LOSS_RISK",
            "scenario_totals_are_not_combined": True,
        }

    yield "scenario_group", group_rows
    for member in members:
        found = session.get(BacktestRun, member.run_id)
        assert found is not None
        run: BacktestRun = found
        prefix = member.policy

        def snapshot(
            run: BacktestRun = run, member: ResearchScenarioMember = member
        ) -> Iterator[dict[str, Any]]:
            metrics = session.get(RunMetrics, run.id)
            yield {
                "run_id": str(run.id),
                "policy": member.policy,
                "ordinal": member.ordinal,
                "status": run.status,
                "error_code": run.error_code,
                "error_message": run.error_message,
                "active_attempt_id": str(run.active_attempt_id) if run.active_attempt_id else None,
                "snapshot": run.snapshot,
                "metrics": metrics.metrics if metrics else None,
            }

        yield prefix + "/snapshot", snapshot

        def cycles(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(BacktestCycle)
                .where(
                    BacktestCycle.run_id == run.id,
                    BacktestCycle.attempt_id == run.active_attempt_id,
                )
                .order_by(BacktestCycle.expiry)
                .execution_options(yield_per=100)
            ):
                yield {
                    "cycle_id": str(row.id),
                    "expiry": str(row.expiry),
                    "entry_date": str(row.entry_date),
                    "state": row.state,
                    "reason": row.reason,
                    "last_sequence": row.last_sequence,
                    "trade_id": str(row.trade_id) if row.trade_id else None,
                    "payload": row.payload,
                }

        yield prefix + "/cycles", cycles

        def positions(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(BacktestCycle)
                .where(
                    BacktestCycle.run_id == run.id,
                    BacktestCycle.attempt_id == run.active_attempt_id,
                )
                .order_by(BacktestCycle.expiry)
                .execution_options(yield_per=100)
            ):
                value = position(row)
                if value is not None:
                    yield value

        yield prefix + "/positions", positions

        def legs(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(BacktestCycle)
                .where(
                    BacktestCycle.run_id == run.id,
                    BacktestCycle.attempt_id == run.active_attempt_id,
                )
                .order_by(BacktestCycle.expiry)
                .execution_options(yield_per=100)
            ):
                value = position(row)
                if value is not None:
                    for leg in value["legs"]:
                        yield {
                            "cycle_id": str(row.id),
                            "state": row.state,
                            "entry_at": value["entry_at"],
                            "exit_at": value["exit_at"],
                            **leg,
                        }

        yield prefix + "/legs", legs

        def risk(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(BacktestRiskEntry)
                .where(
                    BacktestRiskEntry.run_id == run.id,
                    BacktestRiskEntry.attempt_id == run.active_attempt_id,
                )
                .order_by(BacktestRiskEntry.booking_date)
                .execution_options(yield_per=100)
            ):
                yield {
                    "cycle_id": str(row.cycle_id),
                    "booking_date": str(row.booking_date),
                    "source": row.source,
                    "risk_amount": row.risk_amount,
                    "priced_performance_amount": row.priced_performance_amount,
                    "counters": row.counters,
                }

        yield prefix + "/risk", risk

        def journal(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(JournalEvent)
                .where(
                    JournalEvent.run_id == run.id, JournalEvent.attempt_id == run.active_attempt_id
                )
                .order_by(JournalEvent.timestamp, JournalEvent.sequence)
                .execution_options(yield_per=100)
            ):
                yield {
                    "sequence": row.sequence,
                    "timestamp": row.timestamp,
                    "event_type": row.event_type,
                    "state": row.position_state,
                    "payload": row.payload,
                }

        yield prefix + "/journal", journal

        def trades(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(Trade)
                .where(Trade.run_id == run.id)
                .options(selectinload(Trade.legs))
                .order_by(Trade.sequence)
                .execution_options(yield_per=100)
            ):
                yield {
                    "trade_id": str(row.id),
                    "sequence": row.sequence,
                    "entry_at": row.entry_timestamp,
                    "exit_at": row.exit_timestamp,
                    "reason": row.exit_reason,
                    "gross_pnl_inr": row.gross_demo_units,
                    "charges_inr": row.costs_demo_units,
                    "net_pnl_inr": row.net_demo_units,
                    "quantity": row.quantity,
                    "legs": [
                        {
                            "role": leg.role,
                            "contract_id": leg.contract_id,
                            "option_type": leg.option_type,
                            "strike": leg.strike,
                            "expiry": leg.expiry,
                            "side": leg.side,
                            "quantity": leg.quantity,
                            "lot_size": leg.lot_size,
                            "tick_size": leg.tick_size,
                            "entry_fill": leg.entry_reference,
                            "exit_fill": leg.exit_reference,
                            "gross_pnl": leg.demo_units,
                            "pricing_evidence": leg.pricing_evidence,
                        }
                        for leg in row.legs
                    ],
                }

        yield prefix + "/trades", trades

        def events(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run.id)
                .order_by(RunEvent.sequence)
                .execution_options(yield_per=100)
            ):
                yield {
                    "sequence": row.sequence,
                    "created_at": row.created_at,
                    "type": row.event_type,
                    "payload": row.payload,
                }

        yield prefix + "/events", events

        def equity(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            for row in session.scalars(
                select(EquityPoint)
                .where(EquityPoint.run_id == run.id)
                .order_by(EquityPoint.sequence)
                .execution_options(yield_per=100)
            ):
                yield {
                    "sequence": row.sequence,
                    "timestamp": row.timestamp,
                    "equity_inr": row.equity_demo_units,
                    "closed_trade_drawdown_inr": row.drawdown_demo_units,
                    "population": "PRICED_CYCLES_ONLY",
                }

        yield prefix + "/equity", equity

        def assessed(run: BacktestRun = run) -> Iterator[dict[str, Any]]:
            # The read function uses the same saved trades/summary as the API.
            yield assessment_data(session, run.id)

        yield prefix + "/assessment", assessed


def build(session: Session, settings: Settings, job: BacktestExportJob) -> BacktestExportJob:
    repository = BacktestRepository(session)
    if job.status == "COMPLETED":
        return job
    if (
        job.status == "RUNNING"
        and job.lease_expires_at
        and job.lease_expires_at > datetime.now(UTC)
    ):
        return job
    owner = str(uuid4())
    job.status = "RUNNING"
    job.lease_owner = owner
    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    session.commit()
    temporary = None

    def heartbeat() -> None:
        # A separate short transaction renews ownership without invalidating
        # the server-side cursor streaming evidence in the main session.
        with Session(bind=session.get_bind()) as renewal:
            locked = BacktestRepository(renewal).get_export_job(job.id, lock=True)
            if (
                locked is None
                or locked.lease_owner != owner
                or locked.lease_expires_at is None
                or locked.lease_expires_at <= datetime.now(UTC)
            ):
                raise RuntimeError("Model 2 export ownership was lost.")
            locked.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
            renewal.commit()

    try:
        run = session.get(BacktestRun, job.run_id)
        assert run is not None
        group = group_for_run(session, run.id)
        relative = Path("exports") / str(group.id) / f"{job.id}.{job.format.lower()}"
        destination = settings.storage_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{job.id}.{owner}.tmp")
        counts = {}
        identities = {}
        if job.format == "ZIP":
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, rows in tables(session, group):
                    heartbeat()
                    digest = hashlib.sha256()
                    size = count = 0
                    filename = name + ".jsonl"
                    with archive.open(filename, "w") as output:
                        for value in rows():
                            content = encode(value)
                            output.write(content)
                            digest.update(content)
                            size += len(content)
                            count += 1
                            if count % 500 == 0:
                                heartbeat()
                    counts[name] = count
                    identities[filename] = {
                        "sha256": digest.hexdigest(),
                        "bytes": size,
                        "rows": count,
                    }
                archive.writestr(
                    "manifest.json",
                    encode(
                        {
                            "schema_version": "stockbot.model2-evidence.v1",
                            "group_id": str(group.id),
                            "group_status": group.status,
                            "exported_at": datetime.now(UTC),
                            "selected_run_id": str(run.id),
                            "scenario_results_are_not_combined": True,
                            "row_counts": counts,
                            "files": identities,
                        }
                    ),
                )
        elif job.format == "XLSX":
            with xlsxwriter.Workbook(
                temporary,
                {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False},
            ) as book:
                continuations = book.add_worksheet("Cell continuations")
                continuations.write_row(
                    0, 0, ["Table", "Row", "Column", "Part", "Parts", "Full text in order"]
                )
                continuation_row = 1
                for table_index, (name, rows) in enumerate(tables(session, group)):
                    heartbeat()
                    count = 0
                    sheet = None
                    keys = []
                    for value in rows():
                        if count % 1048575 == 0:
                            sheet = book.add_worksheet(
                                f"{table_index + 1}_{name.rsplit('/', 1)[-1][:16]}_{count // 1048575 + 1}"
                            )
                            keys = list(value)
                            sheet.write_row(0, 0, keys)
                            sheet.freeze_panes(1, 0)
                            sheet.set_column(0, len(keys) - 1, 24)
                        assert sheet is not None
                        for column, key in enumerate(keys):
                            raw = value.get(key)
                            render = (
                                json.dumps(raw, default=str, sort_keys=True)
                                if isinstance(raw, dict | list | tuple)
                                else ""
                                if raw is None
                                else str(raw)
                            )
                            if len(render) > 15000:
                                parts = [
                                    render[start : start + 15000]
                                    for start in range(0, len(render), 15000)
                                ]
                                first_part = continuation_row
                                for part_index, part in enumerate(parts, 1):
                                    if continuation_row == 1048576:
                                        raise ValueError(
                                            "Continuation worksheet is full; export ZIP for the complete evidence."
                                        )
                                    continuations.write_row(
                                        continuation_row,
                                        0,
                                        [name, count + 1, key, part_index, len(parts), part],
                                    )
                                    continuation_row += 1
                                render = f"Full value: Cell continuations rows {first_part + 1}–{continuation_row}; {len(parts)} parts."
                            sheet.write_string(count % 1048575 + 1, column, render)
                        count += 1
                        if count % 500 == 0:
                            heartbeat()
                    counts[name] = count
                manifest = book.add_worksheet("Manifest")
                manifest.write_row(0, 0, ["Table", "Rows"])
                for index, (name, count) in enumerate(counts.items(), 1):
                    manifest.write_row(index, 0, [name, count])
        else:
            raise ValueError("Unsupported Model 2 export format.")
        locked = repository.get_export_job(job.id, lock=True)
        if (
            locked is None
            or locked.lease_owner != owner
            or locked.lease_expires_at is None
            or locked.lease_expires_at <= datetime.now(UTC)
        ):
            raise RuntimeError("Model 2 export ownership was lost.")
        temporary.replace(destination)
        checksum = hashlib.sha256()
        with destination.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                checksum.update(chunk)
        locked.status = "COMPLETED"
        locked.artifact_path = relative.as_posix()
        locked.sha256 = checksum.hexdigest()
        locked.row_counts = counts
        locked.completed_at = datetime.now(UTC)
        locked.lease_owner = None
        locked.lease_expires_at = None
        session.commit()
        return locked
    except Exception as exc:
        session.rollback()
        locked = repository.get_export_job(job.id, lock=True)
        if locked and locked.lease_owner == owner:
            locked.status = "FAILED"
            locked.error_code = "MODEL2_EXPORT_FAILED"
            locked.error_message = str(exc)
            locked.lease_owner = None
            locked.lease_expires_at = None
            session.commit()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
