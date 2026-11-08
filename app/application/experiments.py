"""Explicit, immutable parameter experiments with a locked validation period."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.algorithms import AlgorithmService
from app.application.backtests import BacktestService
from app.core.config import Settings
from app.db.models.algorithms import AlgorithmVersion
from app.db.models.backtests import BacktestRun, ExperimentPlan, ExperimentRun
from app.domain.algorithms import get_algorithm
from app.infrastructure.datasets.storage import DatasetStorage
from app.infrastructure.realtime import RunEventPublisher


class ExperimentServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def experiment_inputs(
    baseline_algorithm_version_id: UUID,
    dataset_version_id: UUID,
    parameter_key: str,
    parameter_values: list[Any],
    development_start_date: Any,
    development_end_date: Any,
    validation_start_date: Any,
    validation_end_date: Any,
    starting_capital: Decimal,
) -> dict[str, Any]:
    return {
        "baseline_algorithm_version_id": baseline_algorithm_version_id,
        "dataset_version_id": dataset_version_id,
        "parameter_key": parameter_key,
        "parameter_values": parameter_values,
        "development_start_date": development_start_date,
        "development_end_date": development_end_date,
        "validation_start_date": validation_start_date,
        "validation_end_date": validation_end_date,
        "starting_capital": starting_capital,
    }


class ExperimentService:
    def __init__(
        self,
        session: Session,
        storage: DatasetStorage,
        settings: Settings,
        publisher: RunEventPublisher,
    ) -> None:
        self.session = session
        self.storage = storage
        self.settings = settings
        self.publisher = publisher

    def preview(self, **values: Any) -> dict[str, Any]:
        baseline = self.session.get(AlgorithmVersion, values["baseline_algorithm_version_id"])
        if (
            baseline is None
            or baseline.archived
            or baseline.code_version != "1.2.0-bull-call-spread-v1"
        ):
            raise ExperimentServiceError(
                "EXPERIMENT_CORRECTED_ENGINE_REQUIRED",
                "Select an available V1.2 research baseline.",
                409,
            )
        definition = get_algorithm(
            baseline.algorithm_key, code_version=baseline.code_version, include_inactive=True
        )
        assert definition is not None
        key = values["parameter_key"]
        field = definition.parameter_model.model_fields.get(key)
        if field is None or (
            isinstance(field.json_schema_extra, dict)
            and not field.json_schema_extra.get("editable", True)
        ):
            raise ExperimentServiceError(
                "EXPERIMENT_PARAMETER_INVALID", "Choose an editable algorithm parameter.", 422
            )
        candidates = [
            baseline.parameters,
            *[{**baseline.parameters, key: value} for value in values["parameter_values"]],
        ]
        service = BacktestService(self.session, self.storage, self.settings, self.publisher)
        variants: list[dict[str, Any]] = []
        issues: list[str] = []
        for index, candidate in enumerate(candidates):
            try:
                normalized = definition.configure(candidate).parameters.model_dump(mode="json")
            except ValidationError as exc:
                raise ExperimentServiceError(
                    "EXPERIMENT_PARAMETER_VALUE_INVALID",
                    "A variant contains invalid parameter values.",
                    422,
                ) from exc
            result = service.preflight(
                values["dataset_version_id"],
                baseline.id,
                values["development_start_date"],
                values["development_end_date"],
                True,
                parameters_override=normalized,
            )
            fatal = [
                item
                for item in cast(list[dict[str, Any]], result["issues"])
                if item["code"] != "PARTIAL_COVERAGE_ACKNOWLEDGEMENT_REQUIRED"
            ]
            if fatal:
                issues.append(
                    f"Variant {index} cannot run: " + ", ".join(item["code"] for item in fatal)
                )
            variants.append(
                {
                    "variant_index": index,
                    "parameters": normalized,
                    "coverage_digest": result["coverage_digest"],
                    "included_session_count": result["included_session_count"],
                    "excluded_sessions": result["excluded_sessions"],
                }
            )
        identity = {
            key: values[key]
            for key in (
                "baseline_algorithm_version_id",
                "dataset_version_id",
                "parameter_key",
                "parameter_values",
                "development_start_date",
                "development_end_date",
                "validation_start_date",
                "validation_end_date",
                "starting_capital",
            )
        }
        # PostgreSQL Numeric returns a fixed-scale Decimal. Hash the same
        # financial value identically before and after the plan is persisted.
        identity["starting_capital"] = format(
            Decimal(str(values["starting_capital"])).quantize(Decimal("0.01")), "f"
        )
        digest = hashlib.sha256(
            json.dumps(
                {"inputs": identity, "variants": variants},
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "allowed": not issues,
            "coverage_digest": digest,
            "variants": variants,
            "issues": issues,
        }

    def create(
        self,
        *,
        name: str,
        hypothesis: str,
        baseline_algorithm_version_id: UUID,
        dataset_version_id: UUID,
        parameter_key: str,
        parameter_values: list[Any],
        development_start_date: Any,
        development_end_date: Any,
        validation_start_date: Any,
        validation_end_date: Any,
        coverage_digest: str,
        starting_capital: Decimal,
        notes: str | None,
    ) -> ExperimentPlan:
        baseline = self.session.get(AlgorithmVersion, baseline_algorithm_version_id)
        if baseline is None:
            raise ExperimentServiceError(
                "EXPERIMENT_BASELINE_NOT_FOUND",
                "The baseline algorithm version does not exist.",
                404,
            )
        if baseline.code_version != "1.2.0-bull-call-spread-v1":
            raise ExperimentServiceError(
                "EXPERIMENT_CORRECTED_ENGINE_REQUIRED",
                "Experiments require the corrected Bull Call Spread engine.",
                409,
            )
        definition = get_algorithm(
            baseline.algorithm_key,
            code_version=baseline.code_version,
            include_inactive=True,
        )
        if definition is None or parameter_key not in definition.parameter_model.model_fields:
            raise ExperimentServiceError(
                "EXPERIMENT_PARAMETER_INVALID", "Choose one supported algorithm parameter.", 422
            )
        if development_start_date > development_end_date:
            raise ExperimentServiceError(
                "EXPERIMENT_DEVELOPMENT_RANGE_INVALID",
                "Development start must be on or before development end.",
                422,
            )
        if (
            validation_start_date > validation_end_date
            or development_end_date >= validation_start_date
        ):
            raise ExperimentServiceError(
                "EXPERIMENT_VALIDATION_RANGE_INVALID",
                "Validation must be a later, non-overlapping chronological period.",
                422,
            )
        if len({repr(value) for value in parameter_values}) != len(parameter_values):
            raise ExperimentServiceError(
                "EXPERIMENT_VALUES_DUPLICATED",
                "Every explicit parameter value must be unique.",
                422,
            )
        for value in parameter_values:
            candidate = {**baseline.parameters, parameter_key: value}
            valid, _, _ = AlgorithmService(self.session).validate(baseline.algorithm_key, candidate)
            if not valid:
                raise ExperimentServiceError(
                    "EXPERIMENT_PARAMETER_VALUE_INVALID",
                    f"The value {value!r} is invalid for {parameter_key}.",
                    422,
                )
        review = self.preview(
            **experiment_inputs(
                baseline_algorithm_version_id,
                dataset_version_id,
                parameter_key,
                parameter_values,
                development_start_date,
                development_end_date,
                validation_start_date,
                validation_end_date,
                starting_capital,
            )
        )
        if not review["allowed"] or review["coverage_digest"] != coverage_digest:
            raise ExperimentServiceError(
                "EXPERIMENT_COVERAGE_REVIEW_REQUIRED",
                "Review and accept coverage for every exact variant before creating the plan.",
                409,
            )
        plan = ExperimentPlan(
            name=name.strip(),
            hypothesis=hypothesis.strip(),
            algorithm_key=baseline.algorithm_key,
            baseline_algorithm_version_id=baseline.id,
            dataset_version_id=dataset_version_id,
            parameter_key=parameter_key,
            parameter_values=parameter_values,
            development_start_date=development_start_date,
            development_end_date=development_end_date,
            validation_start_date=validation_start_date,
            validation_end_date=validation_end_date,
            coverage_digest=coverage_digest,
            starting_capital=starting_capital,
            notes=notes,
            status="DRAFT",
        )
        self.session.add(plan)
        self.session.commit()
        return plan

    def get(self, plan_id: UUID) -> ExperimentPlan | None:
        plan = self.session.get(ExperimentPlan, plan_id)
        if plan and plan.status in {"DEVELOPMENT_RUNNING", "VALIDATION_RUNNING"}:
            phase = "DEVELOPMENT" if plan.status == "DEVELOPMENT_RUNNING" else "VALIDATION"
            records = [item for item in self.runs(plan_id) if item.phase == phase]
            if records and any(item.run_status in {"FAILED", "CANCELLED"} for item in records):
                plan.status = f"{phase}_FAILED"
                self.session.commit()
            elif records and all(item.run_status == "COMPLETED" for item in records):
                plan.status = f"{phase}_COMPLETE"
                self.session.commit()
        return plan

    def runs(self, plan_id: UUID) -> list[ExperimentRun]:
        return list(
            self.session.scalars(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_plan_id == plan_id)
                .order_by(ExperimentRun.phase, ExperimentRun.variant_index)
            )
        )

    def start(self, plan_id: UUID) -> tuple[ExperimentPlan, list[ExperimentRun]]:
        plan = self.session.scalar(
            select(ExperimentPlan).where(ExperimentPlan.id == plan_id).with_for_update()
        )
        if plan is None:
            raise ExperimentServiceError(
                "EXPERIMENT_NOT_FOUND", "The experiment plan does not exist.", 404
            )
        existing = [item for item in self.runs(plan_id) if item.phase == "DEVELOPMENT"]
        if existing:
            return plan, existing
        if plan.status != "DRAFT":
            raise ExperimentServiceError(
                "EXPERIMENT_ALREADY_STARTED", "The experiment plan has already started.", 409
            )
        baseline = self.session.get(AlgorithmVersion, plan.baseline_algorithm_version_id)
        if baseline is None:
            raise ExperimentServiceError(
                "EXPERIMENT_BASELINE_NOT_FOUND",
                "The baseline algorithm version does not exist.",
                404,
            )
        review = self.preview(
            **experiment_inputs(
                plan.baseline_algorithm_version_id,
                plan.dataset_version_id,
                plan.parameter_key,
                plan.parameter_values,
                plan.development_start_date,
                plan.development_end_date,
                plan.validation_start_date,
                plan.validation_end_date,
                plan.starting_capital,
            )
        )
        if not review["allowed"] or review["coverage_digest"] != plan.coverage_digest:
            raise ExperimentServiceError(
                "EXPERIMENT_COVERAGE_CHANGED",
                "Coverage changed after this plan was accepted; create a newly reviewed plan.",
                409,
            )
        versions: list[tuple[AlgorithmVersion, Any]] = [
            (baseline, baseline.parameters[plan.parameter_key])
        ]
        algorithms = AlgorithmService(self.session)
        for index, value in enumerate(plan.parameter_values, start=1):
            version = algorithms.create_version(
                plan.algorithm_key,
                f"{plan.name} — variant {index}",
                f"Experiment {plan.id}; {plan.parameter_key}={value!r}",
                {**baseline.parameters, plan.parameter_key: value},
                baseline.id,
                commit=False,
            )
            versions.append((version, value))
        backtests = BacktestService(self.session, self.storage, self.settings, self.publisher)
        records: list[ExperimentRun] = []
        previous_run_id: UUID | None = None
        for index, (version, value) in enumerate(versions):
            coverage = backtests.preflight(
                plan.dataset_version_id,
                version.id,
                plan.development_start_date,
                plan.development_end_date,
                True,
            )
            run, _ = backtests.create(
                dataset_version_id=plan.dataset_version_id,
                algorithm_version_id=version.id,
                start_date=plan.development_start_date,
                end_date=plan.development_end_date,
                starting_capital=plan.starting_capital,
                warning_acknowledged=True,
                accepted_coverage_digest=str(coverage["coverage_digest"]),
                idempotency_key=f"experiment:{plan.id}:development:{index}",
                commit=False,
                depends_on_run_id=previous_run_id,
            )
            previous_run_id = run.id
            record = ExperimentRun(
                experiment_plan_id=plan.id,
                backtest_run_id=run.id,
                variant_index=index,
                parameter_value=value,
                phase="DEVELOPMENT",
                backtest_run=run,
            )
            self.session.add(record)
            records.append(record)
        plan.status = "DEVELOPMENT_RUNNING"
        self.session.commit()
        return plan, records

    def unlock_validation(
        self,
        plan_id: UUID,
        selected_algorithm_version_id: UUID,
        accepted_coverage_digest: str,
    ) -> tuple[ExperimentPlan, ExperimentRun]:
        plan = self.session.scalar(
            select(ExperimentPlan).where(ExperimentPlan.id == plan_id).with_for_update()
        )
        if plan is None:
            raise ExperimentServiceError(
                "EXPERIMENT_NOT_FOUND", "The experiment plan does not exist.", 404
            )
        development = [item for item in self.runs(plan_id) if item.phase == "DEVELOPMENT"]
        if not development or any(item.run_status != "COMPLETED" for item in development):
            raise ExperimentServiceError(
                "EXPERIMENT_DEVELOPMENT_INCOMPLETE",
                "Validation stays locked until every development variant completes successfully.",
                409,
            )
        selected_version = self.session.get(AlgorithmVersion, selected_algorithm_version_id)
        selected_run_ids = {item.backtest_run_id for item in development}
        matching_run = self.session.scalar(
            select(BacktestRun).where(
                BacktestRun.id.in_(selected_run_ids),
                BacktestRun.algorithm_version_id == selected_algorithm_version_id,
            )
        )
        if selected_version is None or matching_run is None:
            raise ExperimentServiceError(
                "EXPERIMENT_SELECTION_INVALID",
                "Select an algorithm version tested by this experiment.",
                422,
            )
        existing = next((item for item in self.runs(plan_id) if item.phase == "VALIDATION"), None)
        if existing:
            return plan, existing
        backtests = BacktestService(self.session, self.storage, self.settings, self.publisher)
        run, _ = backtests.create(
            dataset_version_id=plan.dataset_version_id,
            algorithm_version_id=selected_algorithm_version_id,
            start_date=plan.validation_start_date,
            end_date=plan.validation_end_date,
            starting_capital=plan.starting_capital,
            warning_acknowledged=True,
            accepted_coverage_digest=accepted_coverage_digest,
            idempotency_key=f"experiment:{plan.id}:validation",
            commit=False,
        )
        record = ExperimentRun(
            experiment_plan_id=plan.id,
            backtest_run_id=run.id,
            variant_index=0,
            parameter_value=selected_version.parameters[plan.parameter_key],
            phase="VALIDATION",
            backtest_run=run,
        )
        self.session.add(record)
        plan.selected_algorithm_version_id = selected_algorithm_version_id
        plan.validation_unlocked_at = datetime.now(UTC)
        plan.status = "VALIDATION_RUNNING"
        self.session.commit()
        return plan, record
