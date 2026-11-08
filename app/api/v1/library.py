from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.datasets import LibraryCapabilityCountResponse, LibrarySummaryResponse
from app.db.models.algorithms import Algorithm, AlgorithmVersion
from app.db.models.datasets import Dataset, DatasetVersion

router = APIRouter(prefix="/library", tags=["library"])


@router.get("/summary", response_model=LibrarySummaryResponse)
def get_library_summary(session: Session = Depends(get_db_session)) -> LibrarySummaryResponse:
    versions = list(session.scalars(select(DatasetVersion)))
    status_counts = Counter(version.status for version in versions)
    capabilities: Counter[str] = Counter(
        key for version in versions for key, available in version.capabilities.items() if available
    )
    coverage_start = min(
        (version.coverage_start for version in versions if version.coverage_start), default=None
    )
    coverage_end = max(
        (version.coverage_end for version in versions if version.coverage_end), default=None
    )
    return LibrarySummaryResponse(
        dataset_count=int(session.scalar(select(func.count()).select_from(Dataset)) or 0),
        dataset_version_count=len(versions),
        status_counts=dict(status_counts),
        usable_dataset_count=status_counts["VALID"] + status_counts["WARNING"],
        warning_dataset_count=status_counts["WARNING"],
        invalid_dataset_count=status_counts["INVALID"],
        failed_dataset_count=status_counts["FAILED"],
        validating_dataset_count=status_counts["PENDING"] + status_counts["VALIDATING"],
        total_rows=sum(version.row_count or 0 for version in versions),
        total_sessions=sum(version.session_count or 0 for version in versions),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        active_algorithm_count=int(
            session.scalar(
                select(func.count()).select_from(Algorithm).where(Algorithm.active.is_(True))
            )
            or 0
        ),
        saved_algorithm_version_count=int(
            session.scalar(select(func.count()).select_from(AlgorithmVersion)) or 0
        ),
        archived_algorithm_version_count=int(
            session.scalar(
                select(func.count())
                .select_from(AlgorithmVersion)
                .where(AlgorithmVersion.archived.is_(True))
            )
            or 0
        ),
        capabilities=[
            LibraryCapabilityCountResponse(capability=key, available_versions=count)
            for key, count in sorted(capabilities.items())
        ],
    )
