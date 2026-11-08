from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class DatasetStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    VALID = "VALID"
    WARNING = "WARNING"
    INVALID = "INVALID"
    FAILED = "FAILED"


TERMINAL_DATASET_STATUSES = {
    DatasetStatus.VALID,
    DatasetStatus.WARNING,
    DatasetStatus.INVALID,
    DatasetStatus.FAILED,
}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class ValidationOutcome:
    status: DatasetStatus
    row_count: int
    session_dates: list[str]
    coverage_start: datetime | None
    coverage_end: datetime | None
    warnings: list[ValidationIssue]
    errors: list[ValidationIssue]
    capabilities: dict[str, bool]

    @property
    def session_count(self) -> int:
        return len(self.session_dates)
