from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.backtest_coverage import analyze_derivatives_coverage
from app.application.datasets.service import DatasetService
from app.application.day_classification import DayDecisionSummary
from app.application.jobs import enqueue_job
from app.core.config import Settings
from app.db.models.backtests import (
    BacktestRun,
    Trade,
)
from app.db.repositories.algorithms import AlgorithmRepository
from app.db.repositories.backtests import BacktestRepository
from app.db.repositories.datasets import DatasetRepository
from app.domain.algorithms import BullCallSpreadSignalParameters, get_algorithm
from app.domain.backtests import (
    ENGINE_VERSION,
    TERMINAL_BACKTEST_STATUSES,
    BacktestStatus,
)
from app.domain.datasets import DatasetStatus
from app.domain.demo_execution import (
    DemoExecutionAssumptions,
)
from app.domain.option_spread_execution import (
    OptionSpreadAssumptions,
)
from app.infrastructure.datasets.derivatives_bundle import (
    load_derivatives_bundle,
    verify_derivatives_bundle_files,
)
from app.infrastructure.datasets.storage import DatasetStorage
from app.infrastructure.market_data.csv_reader import CsvMarketDataProvider
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider
from app.infrastructure.realtime import NullRunEventPublisher, RunEventPublisher

logger = logging.getLogger(__name__)


class BacktestServiceError(Exception):
    def __init__(
        self, code: str, message: str, status_code: int, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code, self.details = (
            code,
            message,
            status_code,
            details or {},
        )


class BacktestService:
    def __init__(
        self,
        session: Session,
        storage: DatasetStorage,
        settings: Settings,
        publisher: RunEventPublisher | None = None,
    ) -> None:
        self.session, self.storage, self.settings = session, storage, settings
        self.datasets = DatasetRepository(session)
        self.algorithms = AlgorithmRepository(session)
        self.runs = BacktestRepository(session)
        self.publisher = publisher or NullRunEventPublisher()

    def _commit_and_publish(self, *events: object) -> None:
        """Commit authoritative state first, then send best-effort Redis wake-ups."""

        self.session.commit()
        for event in events:
            run_id = getattr(event, "run_id", None)
            sequence = getattr(event, "sequence", None)
            if isinstance(run_id, UUID) and isinstance(sequence, int):
                try:
                    getattr(self, "publisher", NullRunEventPublisher()).publish(run_id, sequence)
                except Exception:
                    logger.warning(
                        "real-time publish failed after durable commit",
                        extra={"run_id": str(run_id), "sequence": sequence},
                        exc_info=True,
                    )

    @staticmethod
    def _mode(algorithm_key: str, code_version: str | None = None) -> str:
        if algorithm_key == "platform_demo":
            return "PLATFORM_DEMO"
        if algorithm_key == "bull_call_spread" and code_version in {
            "1.0.0-bull-call-spread-v1",
            "1.1.0-bull-call-spread-v1",
            "1.2.0-bull-call-spread-v1",
            "1.3.0-bull-call-spread-v1",
        }:
            return "OPTION_SPREAD_SIMULATION"
        return "SIGNAL_ONLY"

    def preflight(
        self,
        dataset_version_id: UUID,
        algorithm_version_id: UUID,
        start_date: date,
        end_date: date,
        warning_acknowledged: bool,
        accepted_coverage_digest: str | None = None,
        *,
        parameters_override: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        dataset = self.datasets.get_version(dataset_version_id)
        algorithm_version = self.algorithms.get_version(algorithm_version_id)
        if dataset is None:
            raise BacktestServiceError(
                "DATASET_VERSION_NOT_FOUND", "The requested dataset version does not exist.", 404
            )
        if algorithm_version is None:
            raise BacktestServiceError(
                "ALGORITHM_VERSION_NOT_FOUND",
                "The requested algorithm version does not exist.",
                404,
            )
        definition = get_algorithm(
            algorithm_version.algorithm_key,
            code_version=algorithm_version.code_version,
            include_inactive=True,
        )
        if definition is None or algorithm_version.archived:
            raise BacktestServiceError(
                "ALGORITHM_VERSION_UNAVAILABLE",
                "The selected algorithm version is unavailable.",
                409,
            )
        mode = self._mode(algorithm_version.algorithm_key, algorithm_version.code_version)
        range_result = DatasetService(self.session, self.storage, self.settings).range_preflight(
            dataset_version_id, start_date, end_date
        )
        available = sorted(key for key, value in dataset.capabilities.items() if value)
        missing = sorted(set(definition.required_capabilities) - set(available))
        issues = list(cast(list[dict[str, object]], range_result["issues"]))
        if missing:
            issues.append(
                {
                    "code": "DATASET_CAPABILITIES_MISSING",
                    "message": "The dataset cannot provide all required market inputs.",
                    "details": {"missing_capabilities": missing},
                }
            )
        if (
            mode == "OPTION_SPREAD_SIMULATION"
            and not missing
            and algorithm_version.code_version
            not in {
                "1.1.0-bull-call-spread-v1",
                "1.2.0-bull-call-spread-v1",
                "1.3.0-bull-call-spread-v1",
            }
        ):
            supported_session_dates: list[str] = []
            unsupported_session_reasons: list[str] = []
            session_support: list[tuple[date, bool]] = []
            recommended_range: dict[str, object] | None = None
            try:
                bundle_key = dataset.profile.get("derivatives_storage_key")
                if not isinstance(bundle_key, str) or not bundle_key:
                    raise ValueError("Dataset profile has no derivatives_storage_key.")
                bundle_dir = self.storage.resolve(bundle_key)
                manifest = load_derivatives_bundle(bundle_dir)
                if manifest.get("bundle_checksum") != dataset.profile.get("bundle_checksum"):
                    raise ValueError("Registered bundle checksum does not match its manifest.")
                verify_derivatives_bundle_files(bundle_dir, manifest)
                derivatives = ParquetDerivativesProvider(bundle_dir)
                parameters = BullCallSpreadSignalParameters.model_validate(
                    algorithm_version.parameters
                )
                spot_path = self.storage.resolve(dataset.storage_key)
                spot_history = list(
                    CsvMarketDataProvider(spot_path).stream(
                        start_date - timedelta(days=7), end_date
                    )
                )
                enriched = derivatives.enrich_spot_candles(spot_history)
                requested = [
                    candle
                    for candle in enriched
                    if start_date <= candle.timestamp.date() <= end_date
                ]
                sessions = sorted({candle.timestamp.date() for candle in requested})
                for session_date in sessions:
                    try:
                        expiry = derivatives.get_active_expiry_for_date(
                            session_date,
                            allow_same_day=parameters.same_day_weekly_expiry,
                        )
                        if expiry is None:
                            raise ValueError(
                                f"No eligible weekly expiry covers {session_date.isoformat()}."
                            )
                        contracts = derivatives.contracts_by_expiry.get(expiry, {})
                        if not contracts:
                            raise ValueError(
                                f"Weekly contract metadata is missing for {expiry.isoformat()}."
                            )
                        session_candles = [
                            candle
                            for candle in requested
                            if candle.timestamp.date() == session_date
                        ]
                        if any(
                            candle.futures_vwap is None
                            or candle.india_vix is None
                            or candle.previous_india_vix is None
                            for candle in session_candles
                        ):
                            raise ValueError(
                                "Synchronized futures or VIX inputs are missing for "
                                f"{session_date.isoformat()}."
                            )
                        candidate_atms = {
                            int(
                                (
                                    Decimal(str(candle.close)) / Decimal(parameters.atm_increment)
                                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                                * parameters.atm_increment
                            )
                            for candle in session_candles
                        }
                        for atm in candidate_atms:
                            buy = contracts.get((atm, "CE"))
                            primary = contracts.get((atm + parameters.primary_spread_width, "CE"))
                            fallback = contracts.get((atm + parameters.fallback_spread_width, "CE"))
                            if buy is None or (primary is None and fallback is None):
                                raise ValueError(
                                    "Required CE strikes are missing for "
                                    f"{session_date.isoformat()} at ATM {atm}."
                                )
                    except ValueError as exc:
                        unsupported_session_reasons.append(str(exc))
                        session_support.append((session_date, False))
                        continue
                    supported_session_dates.append(session_date.isoformat())
                    session_support.append((session_date, True))
                if unsupported_session_reasons:
                    current_start: date | None = None
                    current_end: date | None = None
                    current_count = 0
                    longest_start: date | None = None
                    longest_end: date | None = None
                    longest_count = 0
                    for session_date, supported in session_support:
                        if supported:
                            current_start = current_start or session_date
                            current_end = session_date
                            current_count += 1
                            if current_count > longest_count:
                                longest_start = current_start
                                longest_end = current_end
                                longest_count = current_count
                        else:
                            current_start, current_end, current_count = None, None, 0
                    if longest_start is not None and longest_end is not None:
                        recommended_range = {
                            "start_date": longest_start.isoformat(),
                            "end_date": longest_end.isoformat(),
                            "session_count": longest_count,
                        }
                    raise ValueError(unsupported_session_reasons[0])
            except (OSError, ValueError) as exc:
                details: dict[str, object] = {"reason": str(exc)}
                if supported_session_dates:
                    details["supported_session_dates"] = supported_session_dates[:5]
                if recommended_range is not None:
                    details["recommended_range"] = recommended_range
                issues.append(
                    {
                        "code": "DERIVATIVES_BUNDLE_PREFLIGHT_FAILED",
                        "message": "The derivatives bundle cannot support the selected range.",
                        "details": details,
                    }
                )
        range_session_dates = cast(list[str], range_result.get("session_dates") or [])
        range_usable_session_count = cast(int, range_result["usable_session_count"])
        basic_coverage_payload = {
            "schema_version": "stockbot.backtest-coverage.v1",
            "dataset_version_id": str(dataset_version_id),
            "requested_start_date": start_date.isoformat(),
            "requested_end_date": end_date.isoformat(),
            "included_sessions": list(range_session_dates),
            "excluded_sessions": [],
        }
        coverage: dict[str, Any] = {
            **basic_coverage_payload,
            "coverage_status": "COMPLETE",
            "coverage_digest": hashlib.sha256(
                json.dumps(basic_coverage_payload, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "requested_session_count": range_usable_session_count,
            "included_session_count": range_usable_session_count,
            "excluded_session_count": 0,
        }
        if (
            mode == "OPTION_SPREAD_SIMULATION"
            and not missing
            and algorithm_version.code_version
            in {
                "1.1.0-bull-call-spread-v1",
                "1.2.0-bull-call-spread-v1",
                "1.3.0-bull-call-spread-v1",
            }
        ):
            try:
                bundle_key = dataset.profile.get("derivatives_storage_key")
                if not isinstance(bundle_key, str) or not bundle_key:
                    raise ValueError("Dataset profile has no derivatives_storage_key.")
                coverage = analyze_derivatives_coverage(
                    bundle_dir=self.storage.resolve(bundle_key),
                    spot_path=self.storage.resolve(dataset.storage_key),
                    start_date=start_date,
                    end_date=end_date,
                    parameters=BullCallSpreadSignalParameters.model_validate(
                        parameters_override or algorithm_version.parameters
                    ),
                    algorithm=definition,
                    dataset_version_id=str(dataset.id),
                    algorithm_version_id=str(algorithm_version.id),
                )
            except (OSError, ValueError) as exc:
                issues.append(
                    {
                        "code": "DERIVATIVES_BUNDLE_PREFLIGHT_FAILED",
                        "message": "The corrected derivatives bundle cannot support this run.",
                        "details": {"reason": str(exc)},
                    }
                )
                coverage = {
                    **basic_coverage_payload,
                    "coverage_status": "BLOCKED",
                    "coverage_digest": hashlib.sha256(
                        json.dumps(basic_coverage_payload, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "requested_session_count": range_usable_session_count,
                    "included_session_count": 0,
                    "excluded_session_count": range_usable_session_count,
                }
            if coverage["coverage_status"] == "BLOCKED" and not any(
                issue.get("code") == "DERIVATIVES_BUNDLE_PREFLIGHT_FAILED" for issue in issues
            ):
                issues.append(
                    {
                        "code": "NO_USABLE_SESSIONS",
                        "message": "No complete trading session remains after coverage checks.",
                    }
                )
            if (
                coverage["coverage_status"] == "PARTIAL"
                and accepted_coverage_digest != coverage["coverage_digest"]
            ):
                issues.append(
                    {
                        "code": "PARTIAL_COVERAGE_ACKNOWLEDGEMENT_REQUIRED",
                        "message": "Acknowledge the exact excluded-session coverage before starting.",
                        "details": {"coverage_digest": coverage["coverage_digest"]},
                    }
                )

        warning_needed = dataset.status == DatasetStatus.WARNING.value
        if warning_needed and not warning_acknowledged:
            issues.append(
                {
                    "code": "DATASET_WARNING_ACKNOWLEDGEMENT_REQUIRED",
                    "message": "Dataset warnings must be acknowledged before a run can start.",
                }
            )
        allowed = (
            bool(range_result["selection_allowed"])
            and not missing
            and (not warning_needed or warning_acknowledged)
            and not issues
        )
        warnings = list(dataset.warnings)
        if mode == "OPTION_SPREAD_SIMULATION":
            warnings.append(
                {
                    "code": "ESTIMATED_HISTORICAL_OHLC_PRICING",
                    "message": (
                        "Option fills use completed candle closes plus configured adverse slippage; "
                        "historical executable bid/ask quotes are unavailable."
                    ),
                    "details": {"executable_quotes": False},
                }
            )
        return {
            "allowed": allowed,
            "mode": mode,
            "dataset_version_id": dataset_version_id,
            "algorithm_version_id": algorithm_version_id,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "usable_session_count": range_result["usable_session_count"],
            "estimated_row_count": range_result["estimated_row_count"],
            "required_capabilities": list(definition.required_capabilities),
            "missing_capabilities": missing,
            "acknowledgement_required": warning_needed or coverage["coverage_status"] == "PARTIAL",
            "warnings": warnings,
            "issues": issues,
            "coverage_status": coverage["coverage_status"],
            "coverage_digest": coverage["coverage_digest"],
            "requested_session_count": coverage["requested_session_count"],
            "included_session_count": coverage["included_session_count"],
            "excluded_session_count": coverage["excluded_session_count"],
            "excluded_sessions": coverage.get("excluded_sessions", []),
            "included_sessions": coverage.get("included_sessions", []),
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
        commit: bool = True,
        depends_on_run_id: UUID | None = None,
    ) -> tuple[BacktestRun, bool]:
        if idempotency_key:
            if len(idempotency_key) > 200:
                raise BacktestServiceError(
                    "IDEMPOTENCY_KEY_INVALID",
                    "Idempotency keys are limited to 200 characters.",
                    422,
                )
            existing = self.runs.get_idempotent(idempotency_key)
            if existing:
                coverage = existing.snapshot.get("coverage", {})
                if (
                    existing.dataset_version_id != dataset_version_id
                    or existing.algorithm_version_id != algorithm_version_id
                    or existing.start_date != start_date
                    or existing.end_date != end_date
                    or existing.starting_capital != starting_capital
                    or coverage.get("accepted_coverage_digest") != accepted_coverage_digest
                ):
                    raise BacktestServiceError(
                        "IDEMPOTENCY_KEY_CONFLICT",
                        "This idempotency key was already used for different run inputs.",
                        409,
                    )
                return existing, True
        preflight = self.preflight(
            dataset_version_id,
            algorithm_version_id,
            start_date,
            end_date,
            warning_acknowledged,
            accepted_coverage_digest,
        )
        if not preflight["allowed"]:
            raise BacktestServiceError(
                "BACKTEST_PREFLIGHT_FAILED",
                "The selected inputs cannot start a backtest.",
                409,
                {"preflight": preflight},
            )
        dataset = self.datasets.get_version(dataset_version_id)
        algorithm_version = self.algorithms.get_version(algorithm_version_id)
        assert dataset is not None and algorithm_version is not None
        dataset_profile = dataset.profile or {}
        definition = get_algorithm(
            algorithm_version.algorithm_key,
            code_version=algorithm_version.code_version,
            include_inactive=True,
        )
        assert definition is not None
        mode = self._mode(algorithm_version.algorithm_key, algorithm_version.code_version)
        demo_execution = DemoExecutionAssumptions() if mode == "PLATFORM_DEMO" else None
        option_execution = None
        if mode == "OPTION_SPREAD_SIMULATION":
            option_parameters = definition.configure(algorithm_version.parameters).parameters
            assert isinstance(option_parameters, BullCallSpreadSignalParameters)
            option_execution = OptionSpreadAssumptions.from_parameters(
                option_parameters, starting_capital=starting_capital
            )
        snapshot = {
            "dataset": {
                "id": str(dataset.id),
                "checksum": dataset.checksum,
                "schema_profile": dataset.schema_profile,
                "storage_key": dataset.storage_key,
                "capabilities": dataset.capabilities,
                "profile": dataset_profile,
                "derivatives_storage_key": dataset_profile.get("derivatives_storage_key"),
                "derivatives_checksum": dataset_profile.get("bundle_checksum"),
            },
            "algorithm": {
                "key": algorithm_version.algorithm_key,
                "version_id": str(algorithm_version.id),
                "version_number": algorithm_version.version_number,
                "code_version": algorithm_version.code_version,
                "schema_version": algorithm_version.schema_version,
                "rule_register_version": definition.documentation.rule_register_version,
                "parameters": algorithm_version.parameters,
            },
            "range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "coverage": {
                "status": preflight.get("coverage_status", "COMPLETE"),
                "digest": preflight.get("coverage_digest", ""),
                "policy": "EXCLUDE_WHOLE_SESSION",
                "included_sessions": preflight.get("included_sessions", []),
                "excluded_sessions": preflight.get("excluded_sessions", []),
                "accepted_coverage_digest": accepted_coverage_digest,
            },
            "starting_capital": str(starting_capital),
            "engine_version": ENGINE_VERSION,
            "assumptions": {
                "mode": mode,
                "option_execution": option_execution.snapshot()
                if option_execution
                else "not_evaluated",
                "synthetic": mode == "PLATFORM_DEMO",
                "unit": "INR"
                if mode == "OPTION_SPREAD_SIMULATION"
                else ("DEMO_UNITS" if mode == "PLATFORM_DEMO" else None),
                "financial_pnl_available": mode == "OPTION_SPREAD_SIMULATION",
                "pricing_quality": (option_execution.pricing_quality if option_execution else None),
                "executable_quotes": (
                    option_execution.executable_quotes if option_execution else False
                ),
                "live_trading_approved": False,
                "demo_execution": demo_execution.snapshot() if demo_execution else None,
            },
        }
        run = BacktestRun(
            dataset_version_id=dataset.id,
            algorithm_version_id=algorithm_version.id,
            start_date=start_date,
            end_date=end_date,
            starting_capital=starting_capital,
            snapshot=snapshot,
            mode=mode,
            status=BacktestStatus.PENDING.value,
            idempotency_key=idempotency_key,
        )
        self.session.add(run)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            if (
                not commit
                or not idempotency_key
                or self.runs.get_idempotent(idempotency_key) is None
            ):
                raise
            # A concurrent request won the unique key. Reuse the same equivalence
            # validation as a normal retry; never hide an unrelated database error.
            return self.create(
                dataset_version_id=dataset_version_id,
                algorithm_version_id=algorithm_version_id,
                start_date=start_date,
                end_date=end_date,
                starting_capital=starting_capital,
                warning_acknowledged=warning_acknowledged,
                accepted_coverage_digest=accepted_coverage_digest,
                idempotency_key=idempotency_key,
            )
        queued = self.runs.append_event(run.id, "BACKTEST_QUEUED", {"mode": mode})
        enqueue_job(self.session, "BACKTEST", run.id, depends_on_run_id)
        if commit:
            self._commit_and_publish(queued)
        else:
            self.session.flush()
        return run, False

    def cancel(self, run_id: UUID) -> BacktestRun:
        run = self.runs.get_run(run_id, lock=True)
        if run is None:
            raise BacktestServiceError(
                "BACKTEST_NOT_FOUND", "The requested backtest does not exist.", 404
            )
        if BacktestStatus(run.status) in TERMINAL_BACKTEST_STATUSES:
            raise BacktestServiceError(
                "BACKTEST_TERMINAL", "Terminal backtests cannot be cancelled.", 409
            )
        run.cancellation_requested = True
        requested = self.runs.append_event(run.id, "CANCELLATION_REQUESTED", {})
        if run.status == "PENDING" and getattr(run, "lease_owner", None) is None:
            run.status = BacktestStatus.CANCELLED.value
            run.progress = 100
            run.completed_at = datetime.now(UTC)
            cancelled = self.runs.append_event(
                run.id, "BACKTEST_CANCELLED", {"mode": getattr(run, "mode", "SIGNAL_ONLY")}
            )
            self._commit_and_publish(requested, cancelled)
        else:
            self._commit_and_publish(requested)
        return run

    def _persist_day_classifications(
        self,
        run: BacktestRun,
        attempt_id: UUID,
        day_summaries: dict[date, DayDecisionSummary],
    ) -> None:
        """Persist one mutually exclusive explanation for every requested session."""

        self.session.flush()
        coverage = run.snapshot.get("coverage")
        coverage_snapshot = coverage if isinstance(coverage, dict) else {}
        values: list[dict[str, object]] = []
        for exclusion in coverage_snapshot.get("excluded_sessions", []):
            if not isinstance(exclusion, dict):
                continue
            values.append(
                {
                    "session_date": date.fromisoformat(str(exclusion["session_date"])),
                    "classification": str(exclusion["classification"]),
                    "included": False,
                    "primary_reason": str(exclusion.get("reason_code") or "DATA_GAP"),
                    "evidence": exclusion.get("details") or {},
                }
            )
        included_values = coverage_snapshot.get("included_sessions")
        included = (
            {date.fromisoformat(str(value)) for value in included_values}
            if isinstance(included_values, list) and included_values
            else set(day_summaries)
        )
        trade_days = {
            item.entry_timestamp.date()
            for item in self.session.scalars(select(Trade).where(Trade.run_id == run.id))
        }
        for session_date in sorted(included):
            summary = day_summaries.get(session_date, DayDecisionSummary())
            accepted_count = summary.accepted_count
            reason_counts = summary.rejection_reasons
            if session_date in trade_days:
                classification = "TRADED"
                primary_reason = None
            elif accepted_count:
                classification = "SIGNAL_REJECTED"
                primary_reason = "EXECUTION_OR_DAILY_GATE_REJECTED"
            elif summary.evaluation_count:
                classification = "NO_QUALIFYING_SIGNAL"
                primary_reason = reason_counts.most_common(1)[0][0] if reason_counts else None
            else:
                classification = "INCLUDED"
                primary_reason = None
            values.append(
                {
                    "session_date": session_date,
                    "classification": classification,
                    "included": True,
                    "primary_reason": primary_reason,
                    "evidence": {
                        "evaluation_count": summary.evaluation_count,
                        "accepted_candidate_count": accepted_count,
                        "rejections_by_reason": dict(reason_counts),
                    },
                }
            )
        self.runs.replace_day_classifications(run.id, attempt_id, values)

    def execute(self, run_id: UUID) -> dict[str, object]:
        from app.application.backtest_execution import execute_backtest

        return execute_backtest(self, run_id)
