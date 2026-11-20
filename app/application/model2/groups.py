"""Model 2 preflight, immutable group creation and group cancellation."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.application.jobs import enqueue_job
from app.application.model2.datasets import PROFILE, digest
from app.application.model2.execution import MODE
from app.application.model2.serialization import request_json
from app.core.config import Settings
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun
from app.db.models.datasets import DatasetVersion
from app.db.models.model2 import ResearchScenarioGroup, ResearchScenarioMember
from app.domain.model2.approved_specification import approved_specification_digest
from app.domain.model2.execution import (
    ALGORITHM_CODE_VERSION,
    ALGORITHM_KEY,
    ENGINE_VERSION,
    POLICIES,
    ReplayRequest,
    market_at,
)
from app.domain.model2.parameters import Model2Parameters
from app.domain.model2.pricing import collect_opening_quotes, evaluate_entry
from app.domain.model2.selection import select_condor
from app.infrastructure.datasets.storage import DatasetStorage
from app.infrastructure.market_data.model2_feed import Model2BundleFeed


class Model2ServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.status_code, self.details = (
            code,
            message,
            status_code,
            details,
        )


class Model2GroupService:
    def __init__(self, session: Session, storage: DatasetStorage, settings: Settings):
        self.session, self.storage, self.settings = session, storage, settings

    def preflight(
        self,
        dataset_version_id: UUID,
        algorithm_version_id: UUID,
        start_date: date,
        end_date: date,
        starting_capital: Decimal,
        warning_acknowledged: bool = False,
        accepted_coverage_digest: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.model2_enabled:
            raise Model2ServiceError(
                "MODEL2_NOT_ENABLED",
                "Model 2 submissions are not enabled on this installation.",
                409,
            )
        stored = self.session.get(DatasetVersion, dataset_version_id)
        version = self.session.get(AlgorithmVersion, algorithm_version_id)
        if stored is None or version is None:
            raise Model2ServiceError(
                "MODEL2_INPUT_NOT_FOUND", "The dataset or algorithm version does not exist.", 404
            )
        if (
            version.algorithm_key != ALGORITHM_KEY
            or version.code_version != ALGORITHM_CODE_VERSION
            or version.archived
        ):
            raise Model2ServiceError(
                "MODEL2_VERSION_UNAVAILABLE", "Choose an available immutable Model 2 version.", 409
            )
        if stored.schema_profile != PROFILE or stored.status not in {
            "VALID",
            "VALID_WITH_WARNINGS",
        }:
            raise Model2ServiceError(
                "MODEL2_DATASET_REQUIRED",
                "Select a verified Model 2 v3 bundle. Older bundles must be imported as a new immutable version.",
                409,
            )
        if (
            stored.coverage_start is None
            or stored.coverage_end is None
            or not stored.coverage_start.date()
            <= start_date
            <= end_date
            <= stored.coverage_end.date()
        ):
            raise Model2ServiceError(
                "DATASET_RANGE_UNAVAILABLE", "Choose dates inside the preserved spot coverage.", 409
            )
        feed = Model2BundleFeed(self.storage.resolve(stored.storage_key))
        checks = tuple(
            (date.fromisoformat(day), datetime.fromisoformat(at))
            for day, at in stored.profile["model2_settlement_checks"]
        )
        expected = digest(
            {
                "bundle_checksum": feed.manifest.bundle_checksum,
                "settlement_checks": [(day.isoformat(), at.isoformat()) for day, at in checks],
                "profile": PROFILE,
            }
        )
        if (
            expected != stored.checksum
            or feed.manifest.bundle_checksum != stored.profile["bundle_checksum"]
        ):
            raise Model2ServiceError(
                "MODEL2_DATASET_CHANGED",
                "The registered input identity no longer matches the preserved bundle.",
                409,
            )
        parameters = Model2Parameters.model_validate(version.parameters)
        request = ReplayRequest(
            feed.manifest.bundle_checksum,
            start_date,
            end_date,
            starting_capital,
            parameters,
            checks,
        )
        cycles = request.validate_calendar(feed.calendar)
        rows = []
        ready = 0
        for cycle in cycles:
            reason: str | None = cycle.classification if cycle.classification != "READY" else None
            available = False
            at = market_at(cycle.entry_session, 15, 20)
            estimate = None
            if reason is None:
                spot = feed.index_close("spot", at, as_of=at)
                vix = feed.index_close("india_vix", at, as_of=at)
                if spot is None or vix is None or spot.close <= 0 or vix.close <= 0:
                    reason = "NO_VALID_ENTRY_INPUT"
                else:
                    selected = select_condor(cycle, spot.close, vix.close, parameters, feed)
                    if selected.selection is None:
                        reason = selected.reason
                        # Strategy filters may legitimately produce a zero-trade run.
                        available = reason == "VIX_LIMIT"
                    else:
                        reference = feed.previous_vix_reference(cycle.entry_session, as_of=at)
                        for minute in (20, 25):
                            quotes = collect_opening_quotes(
                                feed,
                                feed.calendar,
                                selected.selection,
                                market_at(cycle.entry_session, 15, minute),
                            )
                            entry = evaluate_entry(
                                selected.selection,
                                quotes,
                                parameters,
                                starting_capital,
                                previous_vix=reference.close if reference else None,
                            )
                            reason = entry.reason
                            if not entry.retryable:
                                available = True
                                if entry.estimate:
                                    estimate = {
                                        "credit_points": str(entry.estimate.credit_points),
                                        "maximum_loss_inr": str(entry.estimate.maximum_loss_inr),
                                        "assumed_margin_inr": str(
                                            entry.estimate.assumed_margin_inr
                                        ),
                                        "margin_source": "ASSUMED",
                                    }
                                break
            ready += available
            rows.append(
                {
                    "expiry": cycle.expiry.isoformat(),
                    "entry_date": cycle.entry_session.isoformat(),
                    "classification": cycle.classification,
                    "entry_data_available": available,
                    "reason": reason,
                    "entry_estimate": estimate,
                }
            )
        issues = []
        if ready == 0:
            issues.append(
                {
                    "code": "NO_AVAILABLE_CYCLES",
                    "message": "No complete cycles have usable entry inputs/contracts.",
                    "details": {},
                }
            )
        coverage_digest = digest(
            {
                "dataset_version_id": str(stored.id),
                "dataset_checksum": stored.checksum,
                "bundle_manifest": feed.manifest.model_dump(mode="json"),
                "algorithm_version_id": str(version.id),
                "code_version": version.code_version,
                "request": request_json(request),
                "calendar_sha256": feed.calendar.provenance.checksum,
                "specification_sha256": approved_specification_digest(),
                "policies": POLICIES,
                "cycles": rows,
            }
        )
        partial = any(not row["entry_data_available"] for row in rows)
        warnings = list(stored.warnings)
        warnings.append(
            {
                "code": "MODEL2_RESEARCH_LIMITATIONS",
                "message": "Estimated opening fills; assumed margin and placeholder charges. No executable-quote or live-trading claim. Missing exit data is handled chronologically, not removed by preflight.",
                "details": {
                    "experimental_vix_filter": parameters.experimental,
                    "development_window": ["2023-11-16", "2026-07-17"],
                    "later_data_previously_exposed": True,
                },
            }
        )
        acknowledged = warning_acknowledged and (
            not partial or accepted_coverage_digest == coverage_digest
        )
        return {
            "allowed": not issues and acknowledged,
            "mode": MODE,
            "dataset_version_id": stored.id,
            "algorithm_version_id": version.id,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "usable_session_count": ready * 2,
            "estimated_row_count": 0,
            "required_capabilities": ["model2_bundle_v3"],
            "missing_capabilities": [],
            "acknowledgement_required": True,
            "warnings": warnings,
            "issues": issues,
            "coverage_status": "BLOCKED" if issues else "PARTIAL" if partial else "COMPLETE",
            "coverage_digest": coverage_digest,
            "requested_session_count": len(cycles) * 2,
            "included_session_count": ready * 2,
            "excluded_session_count": 0,
            "excluded_sessions": [],
            "requested_cycle_count": len(cycles),
            "entry_ready_cycle_count": ready,
            "cycles": rows,
            "request": request_json(request),
            "calendar_sha256": feed.calendar.provenance.checksum,
            "dataset_checksum": stored.checksum,
            "bundle_manifest": feed.manifest.model_dump(mode="json"),
            "bundle_storage_key": stored.storage_key,
        }

    def create(
        self,
        *,
        dataset_version_id: UUID,
        algorithm_version_id: UUID,
        start_date: date,
        end_date: date,
        starting_capital: Decimal,
        warning_acknowledged: bool,
        accepted_coverage_digest: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ResearchScenarioGroup, bool]:
        key = idempotency_key or str(uuid4())
        if not 1 <= len(key) <= 200:
            raise Model2ServiceError(
                "IDEMPOTENCY_KEY_INVALID", "Use an idempotency key of 1–200 characters."
            )
        inputs = {
            "dataset_version_id": str(dataset_version_id),
            "algorithm_version_id": str(algorithm_version_id),
            "start_date": str(start_date),
            "end_date": str(end_date),
            "starting_capital": str(starting_capital.normalize()),
            "accepted_coverage_digest": accepted_coverage_digest,
        }
        identity = digest(inputs)
        self.session.execute(select(func.pg_advisory_xact_lock(int(digest(key)[:15], 16))))
        existing = self.session.scalar(
            select(ResearchScenarioGroup).where(ResearchScenarioGroup.idempotency_key == key)
        )
        if existing:
            if existing.request_digest != identity:
                raise Model2ServiceError(
                    "IDEMPOTENCY_KEY_CONFLICT",
                    "This key already belongs to a different research request.",
                    409,
                )
            return existing, True
        preflight = self.preflight(
            dataset_version_id,
            algorithm_version_id,
            start_date,
            end_date,
            starting_capital,
            warning_acknowledged,
            accepted_coverage_digest,
        )
        if not preflight["allowed"]:
            raise Model2ServiceError(
                "MODEL2_PREFLIGHT_FAILED",
                "Review and acknowledge the current Model 2 readiness report.",
                409,
                preflight,
            )
        preflight = json.loads(json.dumps(preflight, default=str))
        group = ResearchScenarioGroup(
            id=uuid4(),
            idempotency_key=key,
            request_digest=identity,
            snapshot={"inputs": inputs, "preflight": preflight},
            status="PENDING",
        )
        self.session.add(group)
        self.session.flush()
        first = None
        for ordinal, policy in enumerate(POLICIES, 1):
            snapshot = {
                "dataset": {
                    "version_id": str(dataset_version_id),
                    "checksum": preflight["dataset_checksum"],
                    "bundle_manifest": preflight["bundle_manifest"],
                },
                "algorithm": {
                    "key": ALGORITHM_KEY,
                    "code_version": ALGORITHM_CODE_VERSION,
                    "version_id": str(algorithm_version_id),
                },
                "engine_version": ENGINE_VERSION,
                "specification_sha256": approved_specification_digest(),
                "model2_request": preflight["request"],
                "calendar_sha256": preflight["calendar_sha256"],
                "bundle_storage_key": preflight["bundle_storage_key"],
                "scenario_group_id": str(group.id),
                "policy": policy,
                "coverage": {
                    "status": preflight["coverage_status"],
                    "digest": preflight["coverage_digest"],
                    "accepted_coverage_digest": accepted_coverage_digest,
                },
                "assumptions": {
                    "unit": "INR",
                    "synthetic": False,
                    "executable_quotes": False,
                    "pricing_quality": "estimated_historical_ohlc_open",
                    "margin_source": "ASSUMED",
                    "result_population": "PRICED_CYCLES_ONLY",
                },
            }
            run = BacktestRun(
                id=uuid4(),
                dataset_version_id=dataset_version_id,
                algorithm_version_id=algorithm_version_id,
                start_date=start_date,
                end_date=end_date,
                mode=MODE,
                starting_capital=starting_capital,
                snapshot=snapshot,
                status="PENDING",
            )
            self.session.add(run)
            self.session.flush()
            self.session.add(
                ResearchScenarioMember(
                    run_id=run.id, group_id=group.id, policy=policy, ordinal=ordinal
                )
            )
            if ordinal == 1:
                first = run.id
        assert first is not None
        enqueue_job(self.session, "BACKTEST", first)
        self.session.commit()
        return group, False

    def cancel(self, group_id: UUID) -> ResearchScenarioGroup:
        group = self.session.scalar(
            select(ResearchScenarioGroup)
            .where(ResearchScenarioGroup.id == group_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if group is None:
            raise Model2ServiceError("SCENARIO_GROUP_NOT_FOUND", "Research group not found.", 404)
        if group.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return group
        group.cancellation_requested = True
        running = False
        for run in self.session.scalars(
            select(BacktestRun)
            .join(ResearchScenarioMember, ResearchScenarioMember.run_id == BacktestRun.id)
            .where(ResearchScenarioMember.group_id == group_id)
            .with_for_update(of=BacktestRun)
        ):
            run.cancellation_requested = True
            running |= run.status == "RUNNING"
            if run.status == "PENDING":
                run.status = "CANCELLED"
                run.completed_at = datetime.now(UTC)
        group.status = "CANCELLING" if running else "CANCELLED"
        if not running:
            group.completed_at = datetime.now(UTC)
        self.session.commit()
        return group
