from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AlgorithmTemplateResponse(BaseModel):
    key: str
    name: str
    description: str
    experimental: bool
    parameters: dict[str, Any]


class AlgorithmDocumentationInputResponse(BaseModel):
    key: str
    label: str
    capability: str
    unit: str | None
    required: bool
    description: str


class AlgorithmLogicStepResponse(BaseModel):
    id: str
    order: int
    kind: str
    label: str
    description: str
    depends_on: list[str]
    outcome_labels: list[str]
    fixed_rule_keys: list[str]
    parameter_keys: list[str]
    experimental: bool


class AlgorithmRuleResponse(BaseModel):
    key: str
    statement: str
    source_document: str
    source_reference: str
    status: Literal["APPROVED", "PENDING_PARTNER", "EXPERIMENT_ONLY", "TECHNICAL_PLATFORM_RULE"]
    implemented_code_version: str | None = None
    parameter_key: str | None = None
    test_references: list[str] = Field(default_factory=list)
    effective_date: date | None = None


class AlgorithmDocumentationResponse(BaseModel):
    purpose: str
    lifecycle_status: str
    safety_notice: str
    limitations: list[str]
    inputs: list[AlgorithmDocumentationInputResponse]
    logic_steps: list[AlgorithmLogicStepResponse]
    outputs: list[str]
    diagnostic_schema_version: str
    rule_register_version: str = "stockbot.rule-register.v1"
    rules: list[AlgorithmRuleResponse] = Field(default_factory=list)


class AlgorithmResponse(BaseModel):
    key: str
    display_name: str
    description: str
    code_version: str
    schema_version: int
    active: bool
    supported_modes: list[str]
    required_capabilities: list[str]
    fixed_rules: dict[str, Any]
    templates: list[AlgorithmTemplateResponse]
    documentation: AlgorithmDocumentationResponse


class AlgorithmListResponse(BaseModel):
    items: list[AlgorithmResponse]


class ParameterSchemaResponse(BaseModel):
    algorithm_key: str
    code_version: str
    schema_version: int
    schema_hash: str
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")


class ParameterValidationRequest(BaseModel):
    parameters: dict[str, Any]


class FieldValidationError(BaseModel):
    field: str
    message: str
    type: str


class ParameterValidationResponse(BaseModel):
    valid: bool
    normalized_parameters: dict[str, Any] | None
    errors: list[FieldValidationError]


class AlgorithmVersionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    parent_version_id: UUID | None = None
    parameters: dict[str, Any]


class AlgorithmVersionResponse(BaseModel):
    id: UUID
    algorithm_key: str
    version_number: int
    name: str
    notes: str | None
    parent_version_id: UUID | None
    code_version: str
    schema_version: int
    parameters: dict[str, Any]
    creator_context: dict[str, str]
    archived: bool
    archived_at: datetime | None
    created_at: datetime


class AlgorithmVersionListResponse(BaseModel):
    items: list[AlgorithmVersionResponse]


class CompatibilityRequest(BaseModel):
    dataset_version_id: UUID


class CompatibilityResponse(BaseModel):
    algorithm_key: str
    dataset_version_id: UUID
    required_capabilities: list[str]
    available_capabilities: list[str]
    missing_capabilities: list[str]
    compatible: bool


class AlgorithmUsageRunResponse(BaseModel):
    id: UUID
    status: str
    mode: str
    created_at: datetime
    completed_at: datetime | None
    synthetic: bool
    unit: str
    financial_pnl_available: bool


class AlgorithmUsageSummaryResponse(BaseModel):
    algorithm_version_id: UUID
    total_runs: int
    status_counts: dict[str, int]
    most_recent_run_at: datetime | None
    items: list[AlgorithmUsageRunResponse]
    next_cursor: str | None
