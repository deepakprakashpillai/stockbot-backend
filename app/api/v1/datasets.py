from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.datasets import (
    DatasetCandlesResponse,
    DatasetCoverageResponse,
    DatasetDetailResponse,
    DatasetListResponse,
    DatasetProfileResponse,
    DatasetSummaryResponse,
    DatasetUploadResponse,
    DatasetUploadSessionCommitRequest,
    DatasetUploadSessionResponse,
    DatasetValidationResponse,
    DatasetVersionResponse,
    DatasetVersionSummaryResponse,
    RangePreflightRequest,
    RangePreflightResponse,
    ValidationIssueResponse,
)
from app.application.datasets.service import DatasetService, DatasetServiceError
from app.core.config import Settings
from app.core.errors import error_response
from app.db.models.datasets import Dataset, DatasetUploadSession, DatasetVersion
from app.db.repositories.datasets import DatasetRepository
from app.infrastructure.datasets.storage import LocalDatasetStorage

router = APIRouter(prefix="/datasets", tags=["datasets"])
dataset_versions_router = APIRouter(prefix="/dataset-versions", tags=["datasets"])
dataset_upload_sessions_router = APIRouter(prefix="/dataset-upload-sessions", tags=["datasets"])


def _version_summary(version: DatasetVersion) -> DatasetVersionSummaryResponse:
    return DatasetVersionSummaryResponse(
        id=version.id,
        version_number=version.version_number,
        status=version.status,
        checksum=version.checksum,
        row_count=version.row_count,
        session_count=version.session_count,
        coverage_start=version.coverage_start,
        coverage_end=version.coverage_end,
        created_at=version.created_at,
        original_filename=version.original_filename,
        validation_progress=version.validation_progress,
        warning_count=len(version.warnings),
        error_count=len(version.errors),
        timezone=version.timezone,
        resolution_minutes=version.resolution_minutes,
        capabilities=version.capabilities,
    )


def _version(version: DatasetVersion) -> DatasetVersionResponse:
    return DatasetVersionResponse(
        **_version_summary(version).model_dump(),
        dataset_id=version.dataset_id,
        schema_profile=version.schema_profile,
        instruments=version.instruments,
        warnings=[ValidationIssueResponse.model_validate(item) for item in version.warnings],
        errors=[ValidationIssueResponse.model_validate(item) for item in version.errors],
        task_id=version.task_id,
        completed_at=version.completed_at,
    )


def _summary(dataset: Dataset) -> DatasetSummaryResponse:
    latest = dataset.versions[-1] if dataset.versions else None
    return DatasetSummaryResponse(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        source=dataset.source,
        created_at=dataset.created_at,
        latest_version=_version_summary(latest) if latest else None,
        version_count=len(dataset.versions),
    )


def _detail(dataset: Dataset) -> DatasetDetailResponse:
    return DatasetDetailResponse(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        source=dataset.source,
        created_at=dataset.created_at,
        versions=[_version(version) for version in dataset.versions],
    )


def _service(session: Session, request: Request) -> DatasetService:
    settings: Settings = request.app.state.settings
    return DatasetService(session, LocalDatasetStorage(settings.storage_root), settings)


def _owner_key(request: Request) -> str:
    return str(getattr(request.state, "principal", "local-development"))


def _upload_session(record: DatasetUploadSession) -> DatasetUploadSessionResponse:
    preview = record.preview
    return DatasetUploadSessionResponse(
        id=record.id,
        status=record.status,
        checksum=record.checksum,
        size_bytes=record.size_bytes,
        original_filename=record.original_filename,
        schema_profile=record.schema_profile,
        expires_at=record.expires_at,
        headers=preview.get("headers", []),
        sample_rows=preview.get("sample_rows", []),
        issues=preview.get("issues", []),
        profile=record.profile,
        committed_dataset_id=record.committed_dataset_id,
        committed_version_id=record.committed_version_id,
    )


@router.get("", response_model=DatasetListResponse)
def list_datasets(
    status: str | None = None,
    search: str | None = None,
    cursor: UUID | None = None,
    limit: int = 25,
    session: Session = Depends(get_db_session),
) -> DatasetListResponse:
    normalized_search = search.strip()[:200] if search and search.strip() else None
    repository = DatasetRepository(session)
    datasets, next_cursor = repository.list_datasets(
        status, cursor, min(max(limit, 1), 100), normalized_search
    )
    return DatasetListResponse(
        items=[_summary(dataset) for dataset in datasets],
        next_cursor=next_cursor,
        total_count=repository.count_datasets(status, normalized_search),
    )


async def _create(
    request: Request,
    session: Session,
    name: str,
    description: str | None,
    source: str | None,
    file: UploadFile,
    dataset_id: UUID | None,
) -> JSONResponse:
    try:
        version, status_code, duplicate = await _service(session, request).create_dataset(
            name=name, description=description, source=source, upload=file, dataset_id=dataset_id
        )
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            DatasetUploadResponse(duplicate=duplicate, dataset_version=_version(version))
        ),
    )


@router.post("", response_model=DatasetUploadResponse, status_code=201)
async def create_dataset(
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=200)],
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
    description: Annotated[str | None, Form(max_length=5000)] = None,
    source: Annotated[str | None, Form(max_length=500)] = None,
) -> JSONResponse:
    return await _create(request, session, name, description, source, file, None)


@router.post("/{dataset_id}/versions", response_model=DatasetUploadResponse, status_code=201)
async def create_dataset_version(
    dataset_id: UUID,
    request: Request,
    name: Annotated[str, Form(min_length=1, max_length=200)],
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
    description: Annotated[str | None, Form(max_length=5000)] = None,
    source: Annotated[str | None, Form(max_length=500)] = None,
) -> JSONResponse:
    return await _create(request, session, name, description, source, file, dataset_id)


@dataset_upload_sessions_router.post(
    "", response_model=DatasetUploadSessionResponse, status_code=201
)
async def create_upload_session(
    request: Request,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
) -> DatasetUploadSessionResponse | JSONResponse:
    try:
        record = await _service(session, request).create_upload_session(
            upload=file, owner_key=_owner_key(request)
        )
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )
    return _upload_session(record)


@dataset_upload_sessions_router.get("/{session_id}", response_model=DatasetUploadSessionResponse)
def get_upload_session(
    session_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> DatasetUploadSessionResponse | JSONResponse:
    try:
        return _upload_session(
            _service(session, request).get_upload_session(session_id, _owner_key(request))
        )
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )


@dataset_upload_sessions_router.post("/{session_id}/commit", response_model=DatasetUploadResponse)
def commit_upload_session(
    session_id: UUID,
    body: DatasetUploadSessionCommitRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> DatasetUploadResponse | JSONResponse:
    try:
        version, _status_code, duplicate = _service(session, request).commit_upload_session(
            session_id=session_id,
            owner_key=_owner_key(request),
            name=body.name,
            description=body.description,
            source=body.source,
            dataset_id=body.dataset_id,
        )
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )
    return DatasetUploadResponse(duplicate=duplicate, dataset_version=_version(version))


@dataset_upload_sessions_router.delete(
    "/{session_id}", status_code=204, response_model=None, response_class=Response
)
def discard_upload_session(
    session_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> Response:
    try:
        _service(session, request).discard_upload_session(session_id, _owner_key(request))
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )
    return Response(status_code=204)


@router.get("/{dataset_id}", response_model=DatasetDetailResponse)
def get_dataset(
    dataset_id: UUID, session: Session = Depends(get_db_session)
) -> DatasetDetailResponse:
    dataset = DatasetRepository(session).get_dataset(dataset_id)
    if dataset is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Dataset not found.")
    return _detail(dataset)


@dataset_versions_router.get("/{version_id}", response_model=DatasetVersionResponse)
def get_dataset_version(
    version_id: UUID, session: Session = Depends(get_db_session)
) -> DatasetVersionResponse:
    version = DatasetRepository(session).get_version(version_id)
    if version is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Dataset version not found.")
    return _version(version)


@dataset_versions_router.get("/{version_id}/coverage", response_model=DatasetCoverageResponse)
def get_coverage(
    version_id: UUID, session: Session = Depends(get_db_session)
) -> DatasetCoverageResponse:
    version = DatasetRepository(session).get_version(version_id)
    if version is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Dataset version not found.")
    from datetime import date

    return DatasetCoverageResponse(
        dataset_version_id=version.id,
        coverage_start=version.coverage_start,
        coverage_end=version.coverage_end,
        session_count=version.session_count,
        session_dates=[date.fromisoformat(value) for value in version.session_dates],
        timezone=version.timezone,
        resolution_minutes=version.resolution_minutes,
    )


@dataset_versions_router.get("/{version_id}/validation", response_model=DatasetValidationResponse)
def get_validation(
    version_id: UUID, session: Session = Depends(get_db_session)
) -> DatasetValidationResponse:
    version = DatasetRepository(session).get_version(version_id)
    if version is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Dataset version not found.")
    return DatasetValidationResponse(
        dataset_version_id=version.id,
        status=version.status,
        progress=version.validation_progress,
        warnings=[ValidationIssueResponse.model_validate(item) for item in version.warnings],
        errors=[ValidationIssueResponse.model_validate(item) for item in version.errors],
        capabilities=version.capabilities,
    )


@dataset_versions_router.get("/{version_id}/profile", response_model=DatasetProfileResponse)
def get_profile(
    version_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> DatasetProfileResponse | JSONResponse:
    try:
        return DatasetProfileResponse.model_validate(_service(session, request).profile(version_id))
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )


@dataset_versions_router.get("/{version_id}/candles", response_model=DatasetCandlesResponse)
def get_candles(
    version_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    start: datetime | None = None,
    end: datetime | None = None,
    granularity: Literal["5m", "15m", "30m", "1h", "1d"] = "5m",
    purpose: Literal["chart", "table"] = "chart",
    cursor: str | None = None,
    limit: int = 500,
) -> DatasetCandlesResponse | JSONResponse:
    try:
        return DatasetCandlesResponse.model_validate(
            _service(session, request).candles(
                version_id,
                start=start,
                end=end,
                granularity=granularity,
                purpose=purpose,
                cursor=cursor,
                limit=limit,
            )
        )
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )


@dataset_versions_router.post(
    "/{version_id}/range-preflight", response_model=RangePreflightResponse
)
def range_preflight(
    version_id: UUID,
    body: RangePreflightRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> RangePreflightResponse | JSONResponse:
    try:
        result = _service(session, request).range_preflight(
            version_id, body.start_date, body.end_date
        )
    except DatasetServiceError as exc:
        return error_response(
            status_code=exc.status_code, code=exc.code, message=exc.message, request=request
        )
    return RangePreflightResponse.model_validate(result)
