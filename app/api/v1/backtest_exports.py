"""Backtest export job creation, status, and verified download routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.backtests import ExportJobCreateRequest, ExportJobResponse
from app.api.v1.backtest_helpers import _error, _export_job, _not_found
from app.application.backtests import BacktestServiceError
from app.application.exports import BacktestExportService, ExportServiceError
from app.core.config import Settings
from app.core.errors import error_response
from app.db.repositories.backtests import BacktestRepository

job_router = APIRouter()
run_router = APIRouter()


@job_router.get("/export-jobs/{job_id}", response_model=ExportJobResponse)
def get_export_job(
    job_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> ExportJobResponse | JSONResponse:
    value = BacktestRepository(session).get_export_job(job_id)
    if value is None:
        return _not_found(request)
    return _export_job(value)


@job_router.get("/export-jobs/{job_id}/download", response_class=FileResponse, response_model=None)
def download_export_job(
    job_id: UUID, request: Request, session: Session = Depends(get_db_session)
) -> FileResponse | JSONResponse:
    value = BacktestRepository(session).get_export_job(job_id)
    if value is None:
        return _not_found(request)
    if value.status != "COMPLETED" or not value.artifact_path:
        return _error(
            request,
            BacktestServiceError(
                "EXPORT_NOT_READY", "The complete export is not ready for download.", 409
            ),
        )
    settings: Settings = request.app.state.settings
    root = settings.storage_root.resolve()
    path = (root / value.artifact_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return _error(
            request,
            BacktestServiceError(
                "EXPORT_ARTIFACT_UNAVAILABLE", "The export artifact is unavailable.", 404
            ),
        )
    from app.infrastructure.datasets.derivatives_bundle import sha256_file

    if not value.sha256 or sha256_file(path) != value.sha256:
        return _error(
            request,
            BacktestServiceError(
                "EXPORT_ARTIFACT_CORRUPT",
                "The export checksum no longer matches. Create a fresh export.",
                409,
            ),
        )
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if value.format == "XLSX"
            else "application/zip"
        ),
        filename=f"stockbot-backtest-{value.run_id}.{value.format.lower()}",
    )


@run_router.post("/{run_id}/export-jobs", response_model=ExportJobResponse, status_code=202)
def create_export_job(
    run_id: UUID,
    _body: ExportJobCreateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ExportJobResponse | JSONResponse:
    settings: Settings = request.app.state.settings
    try:
        job = BacktestExportService(session, settings).create(run_id, format=_body.format)
    except ExportServiceError as exc:
        return error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=None,
            request=request,
        )
    return _export_job(job)
