from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ValidationIssueResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DatasetVersionSummaryResponse(BaseModel):
    id: UUID
    version_number: int
    status: str
    checksum: str
    row_count: int | None
    session_count: int | None
    coverage_start: datetime | None
    coverage_end: datetime | None
    created_at: datetime
    original_filename: str
    validation_progress: int
    warning_count: int
    error_count: int
    timezone: str | None
    resolution_minutes: int | None
    capabilities: dict[str, bool]


class DatasetSummaryResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    source: str | None
    created_at: datetime
    latest_version: DatasetVersionSummaryResponse | None
    version_count: int


class DatasetListResponse(BaseModel):
    items: list[DatasetSummaryResponse]
    next_cursor: UUID | None
    total_count: int


class LibraryCapabilityCountResponse(BaseModel):
    capability: str
    available_versions: int


class LibrarySummaryResponse(BaseModel):
    dataset_count: int
    dataset_version_count: int
    status_counts: dict[str, int]
    usable_dataset_count: int
    warning_dataset_count: int
    invalid_dataset_count: int
    failed_dataset_count: int
    validating_dataset_count: int
    total_rows: int
    total_sessions: int
    coverage_start: datetime | None
    coverage_end: datetime | None
    active_algorithm_count: int
    saved_algorithm_version_count: int
    archived_algorithm_version_count: int
    capabilities: list[LibraryCapabilityCountResponse]


class DatasetVersionResponse(DatasetVersionSummaryResponse):
    dataset_id: UUID
    schema_profile: str
    original_filename: str
    timezone: str | None
    resolution_minutes: int | None
    instruments: list[dict[str, str]]
    capabilities: dict[str, bool]
    warnings: list[ValidationIssueResponse]
    errors: list[ValidationIssueResponse]
    validation_progress: int
    task_id: str | None
    completed_at: datetime | None


class DatasetDetailResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    source: str | None
    created_at: datetime
    versions: list[DatasetVersionResponse]


class DatasetUploadResponse(BaseModel):
    duplicate: bool
    dataset_version: DatasetVersionResponse


class DatasetPreviewIssueResponse(ValidationIssueResponse):
    pass


class DatasetUploadSessionResponse(BaseModel):
    id: UUID
    status: str
    checksum: str
    size_bytes: int
    original_filename: str
    schema_profile: str | None
    expires_at: datetime
    headers: list[str]
    sample_rows: list[dict[str, str]]
    issues: list[DatasetPreviewIssueResponse]
    profile: dict[str, Any]
    committed_dataset_id: UUID | None = None
    committed_version_id: UUID | None = None


class DatasetUploadSessionCommitRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    source: str | None = Field(default=None, max_length=500)
    dataset_id: UUID | None = None


class DatasetProfileResponse(BaseModel):
    dataset_version_id: UUID
    schema_profile: str
    profile: dict[str, Any]
    capabilities: dict[str, bool]
    warnings: list[ValidationIssueResponse]
    coverage_start: datetime | None
    coverage_end: datetime | None


class CandleResponse(BaseModel):
    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    open_interest: str


class DatasetQualityMarkerResponse(ValidationIssueResponse):
    pass


class DatasetCandlesResponse(BaseModel):
    dataset_version_id: UUID
    purpose: str
    granularity: str
    range_start: datetime
    range_end: datetime
    candles: list[CandleResponse]
    next_cursor: str | None
    truncated: bool
    quality_markers: list[DatasetQualityMarkerResponse]
    capabilities: dict[str, bool]


class DatasetCoverageResponse(BaseModel):
    dataset_version_id: UUID
    coverage_start: datetime | None
    coverage_end: datetime | None
    session_count: int | None
    session_dates: list[date]
    timezone: str | None
    resolution_minutes: int | None


class DatasetValidationResponse(BaseModel):
    dataset_version_id: UUID
    status: str
    progress: int
    warnings: list[ValidationIssueResponse]
    errors: list[ValidationIssueResponse]
    capabilities: dict[str, bool]


class RangePreflightRequest(BaseModel):
    start_date: date
    end_date: date


class RangePreflightResponse(BaseModel):
    dataset_version_id: UUID
    status: str
    selection_allowed: bool
    acknowledgement_required: bool
    requested_start_date: date
    requested_end_date: date
    available_start_date: date | None
    available_end_date: date | None
    usable_session_count: int
    estimated_row_count: int
    warnings: list[ValidationIssueResponse]
    issues: list[dict[str, Any]]
    capabilities: dict[str, bool]
