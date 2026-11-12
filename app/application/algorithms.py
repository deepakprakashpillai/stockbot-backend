from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.models.algorithms import Algorithm, AlgorithmVersion
from app.db.repositories.algorithms import AlgorithmRepository
from app.db.repositories.datasets import DatasetRepository
from app.domain.algorithms import (
    ALGORITHM_REGISTRY,
    PLATFORM_DEMO,
    AlgorithmDefinition,
    get_algorithm,
)
from app.domain.algorithms import (
    FUTURES_BULL_CALL_SPREAD as BULL_CALL_SPREAD,
)


class AlgorithmServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int, details: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code, self.details = (
            code,
            message,
            status_code,
            details,
        )


def parameter_schema(
    definition: AlgorithmDefinition = BULL_CALL_SPREAD,
) -> dict[str, Any]:
    schema = definition.parameter_model.model_json_schema(mode="validation")
    score_property = schema.get("properties", {}).get("score_model")
    score_definition = schema.get("$defs", {}).get("ScoreModel")
    if score_property is not None and score_definition is not None:
        score_property.pop("$ref", None)
        score_property.update(score_definition)
    schema["x-stockbot"] = {
        "experimental": all(template.experimental for template in definition.templates),
        "schema_version": definition.schema_version,
        "no_approved_default": definition.documentation.lifecycle_status
        == "pending_partner_confirmation_research",
        "synthetic": bool(definition.fixed_rules.get("synthetic", False)),
        "live_trading_approved": bool(definition.fixed_rules.get("live_trading_approved", False)),
        "pricing_quality": definition.fixed_rules.get("pricing_quality"),
        "executable_quotes": definition.fixed_rules.get("executable_quotes"),
    }
    return schema


def schema_hash(definition: AlgorithmDefinition = BULL_CALL_SPREAD) -> str:
    content = json.dumps(parameter_schema(definition), sort_keys=True, separators=(",", ":"))
    return sha256(content.encode()).hexdigest()


def sync_registry(session: Session) -> int:
    changed = 0
    repository = AlgorithmRepository(session)
    for definition in ALGORITHM_REGISTRY.values():
        record = repository.get_algorithm(definition.key)
        values = {
            "display_name": definition.display_name,
            "description": definition.description,
            "code_version": definition.code_version,
            "schema_version": definition.schema_version,
            "schema_hash": schema_hash(definition),
            "active": definition.active,
            "supported_modes": list(definition.supported_modes),
            "required_capabilities": list(definition.required_capabilities),
        }
        if record is None:
            session.add(Algorithm(key=definition.key, **values))
            changed += 1
        elif any(getattr(record, key) != value for key, value in values.items()):
            for key, value in values.items():
                setattr(record, key, value)
            record.synced_at = datetime.now(UTC)
            changed += 1

    demo_versions = repository.list_versions(PLATFORM_DEMO.key, include_archived=True)
    if not demo_versions:
        session.add(
            AlgorithmVersion(
                algorithm_key=PLATFORM_DEMO.key,
                version_number=repository.next_version_number(PLATFORM_DEMO.key),
                name="Default platform demo",
                notes="System fixture for end-to-end platform verification; not a trading strategy.",
                code_version=PLATFORM_DEMO.code_version,
                schema_version=PLATFORM_DEMO.schema_version,
                parameters=PLATFORM_DEMO.templates[0].parameters,
                creator_context={"type": "system_fixture", "synthetic": "true"},
            )
        )
        changed += 1
    else:
        system_fixture = next(
            (
                version
                for version in demo_versions
                if version.creator_context.get("type") == "system_fixture"
            ),
            None,
        )
        if system_fixture and system_fixture.creator_context.get("synthetic") != "true":
            system_fixture.creator_context = {"type": "system_fixture", "synthetic": "true"}
            changed += 1

    bull_versions = repository.list_versions(BULL_CALL_SPREAD.key, include_archived=True)
    if not any(version.code_version == BULL_CALL_SPREAD.code_version for version in bull_versions):
        session.add(
            AlgorithmVersion(
                algorithm_key=BULL_CALL_SPREAD.key,
                version_number=repository.next_version_number(BULL_CALL_SPREAD.key),
                name=BULL_CALL_SPREAD.templates[0].name,
                notes=(
                    "Q10/L04 correction: futures close versus futures VWAP at entry and trend reversal. "
                    "Existing V1.2 scores, thresholds and other policies retained; not the full November specification. "
                    "Historical option fills remain estimated candle-close outcomes."
                ),
                code_version=BULL_CALL_SPREAD.code_version,
                schema_version=BULL_CALL_SPREAD.schema_version,
                parameters=BULL_CALL_SPREAD.templates[0].parameters,
                creator_context={"type": "system_default", "estimated": "true"},
            )
        )
        changed += 1
    session.commit()
    return changed


class AlgorithmService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = AlgorithmRepository(session)

    def definition(self, key: str) -> AlgorithmDefinition:
        definition = get_algorithm(key)
        if definition is None:
            raise AlgorithmServiceError("ALGORITHM_NOT_FOUND", "Algorithm not found.", 404)
        return definition

    def validate(
        self, key: str, parameters: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None, list[dict[str, Any]]]:
        definition = self.definition(key)
        try:
            configured = definition.configure(parameters)
        except ValidationError as exc:
            errors = [
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "message": error["msg"],
                    "type": error["type"],
                }
                for error in exc.errors(include_url=False)
            ]
            return False, None, errors
        return True, configured.parameters.model_dump(mode="json"), []

    def create_version(
        self,
        key: str,
        name: str,
        notes: str | None,
        parameters: dict[str, Any],
        parent_version_id: UUID | None,
        *,
        commit: bool = True,
        code_version: str | None = None,
    ) -> AlgorithmVersion:
        definition = get_algorithm(key, code_version=code_version, include_inactive=True)
        if definition is None:
            raise AlgorithmServiceError("ALGORITHM_NOT_FOUND", "Algorithm not found.", 404)
        try:
            normalized = definition.configure(parameters).parameters.model_dump(mode="json")
        except ValidationError as exc:
            raise AlgorithmServiceError(
                "ALGORITHM_PARAMETERS_INVALID",
                "Parameters are invalid.",
                422,
                [
                    {
                        "field": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                        "type": error["type"],
                    }
                    for error in exc.errors(include_url=False)
                ],
            ) from exc
        parent = self.repository.get_version(parent_version_id) if parent_version_id else None
        if parent_version_id and (parent is None or parent.algorithm_key != key):
            raise AlgorithmServiceError("PARENT_VERSION_INVALID", "Parent version is invalid.", 422)
        if self.repository.get_algorithm(key) is None:
            raise AlgorithmServiceError(
                "ALGORITHM_CATALOG_NOT_SYNCED", "Run the registry sync command.", 409
            )
        record = AlgorithmVersion(
            algorithm_key=key,
            version_number=self.repository.next_version_number(key),
            name=name.strip(),
            notes=notes,
            parent_version_id=parent_version_id,
            code_version=definition.code_version,
            schema_version=definition.schema_version,
            parameters=normalized,
            creator_context={"type": "authenticated_local", "principal": "local-mvp-user"},
        )
        self.session.add(record)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return record

    def archive(self, version_id: UUID) -> AlgorithmVersion:
        record = self.repository.get_version(version_id)
        if record is None:
            raise AlgorithmServiceError("ALGORITHM_VERSION_NOT_FOUND", "Version not found.", 404)
        if not record.archived:
            record.archived = True
            record.archived_at = datetime.now(UTC)
            self.session.commit()
        return record

    def compatibility(self, key: str, dataset_version_id: UUID) -> dict[str, Any]:
        definition = self.definition(key)
        version = DatasetRepository(self.session).get_version(dataset_version_id)
        if version is None:
            raise AlgorithmServiceError(
                "DATASET_VERSION_NOT_FOUND", "Dataset version not found.", 404
            )
        available = sorted(
            capability for capability, enabled in version.capabilities.items() if enabled
        )
        missing = sorted(set(definition.required_capabilities) - set(available))
        return {
            "algorithm_key": key,
            "dataset_version_id": dataset_version_id,
            "required_capabilities": list(definition.required_capabilities),
            "available_capabilities": available,
            "missing_capabilities": missing,
            "compatible": not missing,
        }

    def usage_summary(self, version_id: UUID, *, cursor: str | None, limit: int) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise AlgorithmServiceError(
                "ALGORITHM_USAGE_LIMIT_EXCEEDED", "Usage requests are limited to 100 runs.", 422
            )
        try:
            total, status_counts, most_recent, runs, next_cursor = self.repository.usage_summary(
                version_id, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            raise AlgorithmServiceError(
                "ALGORITHM_USAGE_CURSOR_INVALID", "Usage cursor is invalid.", 422
            ) from exc
        if self.repository.get_version(version_id) is None:
            raise AlgorithmServiceError(
                "ALGORITHM_VERSION_NOT_FOUND", "Algorithm version not found.", 404
            )
        return {
            "algorithm_version_id": version_id,
            "total_runs": total,
            "status_counts": status_counts,
            "most_recent_run_at": most_recent,
            "items": [
                {
                    "id": run.id,
                    "status": run.status,
                    "mode": run.mode,
                    "created_at": run.created_at,
                    "completed_at": run.completed_at,
                    "synthetic": run.mode == "PLATFORM_DEMO",
                    "unit": "INR" if run.mode == "OPTION_SPREAD_SIMULATION" else "DEMO_UNITS",
                    "financial_pnl_available": run.mode == "OPTION_SPREAD_SIMULATION",
                }
                for run in runs
            ],
            "next_cursor": next_cursor,
        }
