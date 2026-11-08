from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.backtests import (
    ExperimentBatchResponse,
    ExperimentPlanCreateRequest,
    ExperimentPlanResponse,
    ExperimentPreflightResponse,
    ExperimentRunResponse,
    ExperimentValidationUnlockRequest,
)
from app.application.algorithms import AlgorithmServiceError
from app.application.backtests import BacktestServiceError
from app.application.experiments import ExperimentService, ExperimentServiceError
from app.core.config import Settings
from app.core.errors import error_response
from app.infrastructure.datasets.storage import LocalDatasetStorage
from app.infrastructure.realtime import RedisRunEventPublisher

router = APIRouter(prefix="/experiments", tags=["experiments"])


def _service(request: Request, session: Session) -> ExperimentService:
    settings: Settings = request.app.state.settings
    publisher = getattr(request.app.state, "realtime_publisher", None)
    if publisher is None:
        publisher = RedisRunEventPublisher(settings)
        request.app.state.realtime_publisher = publisher
    return ExperimentService(
        session,
        LocalDatasetStorage(settings.storage_root),
        settings,
        publisher,
    )


def _error(
    request: Request, exc: ExperimentServiceError | BacktestServiceError | AlgorithmServiceError
) -> JSONResponse:
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=None,
        request=request,
    )


def _plan(value: object) -> ExperimentPlanResponse:
    return ExperimentPlanResponse.model_validate(value, from_attributes=True)


def _run(value: object) -> ExperimentRunResponse:
    return ExperimentRunResponse.model_validate(value, from_attributes=True)


@router.post("/preflight", response_model=ExperimentPreflightResponse)
def preflight_experiment(
    body: ExperimentPlanCreateRequest, request: Request, session: Session = Depends(get_db_session)
) -> ExperimentPreflightResponse | JSONResponse:
    try:
        return ExperimentPreflightResponse.model_validate(
            _service(request, session).preview(**body.model_dump())
        )
    except (ExperimentServiceError, BacktestServiceError, AlgorithmServiceError) as exc:
        session.rollback()
        return _error(request, exc)


@router.post("", response_model=ExperimentPlanResponse, status_code=201)
def create_experiment(
    body: ExperimentPlanCreateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ExperimentPlanResponse | JSONResponse:
    try:
        value = _service(request, session).create(**body.model_dump())
    except (ExperimentServiceError, BacktestServiceError, AlgorithmServiceError) as exc:
        session.rollback()
        return _error(request, exc)
    return _plan(value)


@router.get("/{plan_id}", response_model=ExperimentBatchResponse)
def get_experiment(
    plan_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ExperimentBatchResponse | JSONResponse:
    service = _service(request, session)
    value = service.get(plan_id)
    if value is None:
        return _error(
            request,
            ExperimentServiceError(
                "EXPERIMENT_NOT_FOUND", "The experiment plan does not exist.", 404
            ),
        )
    return ExperimentBatchResponse(
        plan=_plan(value), runs=[_run(item) for item in service.runs(plan_id)]
    )


@router.post("/{plan_id}/start", response_model=ExperimentBatchResponse, status_code=202)
def start_experiment(
    plan_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ExperimentBatchResponse | JSONResponse:
    try:
        plan, values = _service(request, session).start(plan_id)
    except (ExperimentServiceError, BacktestServiceError, AlgorithmServiceError) as exc:
        session.rollback()
        return _error(request, exc)
    return ExperimentBatchResponse(plan=_plan(plan), runs=[_run(item) for item in values])


@router.post(
    "/{plan_id}/unlock-validation",
    response_model=ExperimentBatchResponse,
    status_code=202,
)
def unlock_experiment_validation(
    plan_id: UUID,
    body: ExperimentValidationUnlockRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ExperimentBatchResponse | JSONResponse:
    service = _service(request, session)
    try:
        plan, value = service.unlock_validation(
            plan_id,
            body.selected_algorithm_version_id,
            body.accepted_coverage_digest,
        )
    except (ExperimentServiceError, BacktestServiceError, AlgorithmServiceError) as exc:
        session.rollback()
        return _error(request, exc)
    return ExperimentBatchResponse(
        plan=_plan(plan), runs=[_run(item) for item in service.runs(plan_id)]
    )
