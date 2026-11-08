from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db_session
from app.api.schemas.algorithms import (
    AlgorithmDocumentationResponse,
    AlgorithmListResponse,
    AlgorithmResponse,
    AlgorithmTemplateResponse,
    AlgorithmUsageSummaryResponse,
    AlgorithmVersionCreateRequest,
    AlgorithmVersionListResponse,
    AlgorithmVersionResponse,
    CompatibilityRequest,
    CompatibilityResponse,
    FieldValidationError,
    ParameterSchemaResponse,
    ParameterValidationRequest,
    ParameterValidationResponse,
)
from app.application.algorithms import (
    AlgorithmService,
    AlgorithmServiceError,
    parameter_schema,
    schema_hash,
)
from app.core.errors import error_response
from app.db.models.algorithms import AlgorithmVersion
from app.db.repositories.algorithms import AlgorithmRepository
from app.domain.algorithms import ALGORITHM_REGISTRY, AlgorithmDefinition, get_algorithm

router = APIRouter(prefix="/algorithms", tags=["algorithms"])
versions_router = APIRouter(prefix="/algorithm-versions", tags=["algorithms"])


def _definition(value: AlgorithmDefinition) -> AlgorithmResponse:
    return AlgorithmResponse(
        key=value.key,
        display_name=value.display_name,
        description=value.description,
        code_version=value.code_version,
        schema_version=value.schema_version,
        active=value.active,
        supported_modes=list(value.supported_modes),
        required_capabilities=list(value.required_capabilities),
        fixed_rules=value.fixed_rules,
        templates=[AlgorithmTemplateResponse(**template.__dict__) for template in value.templates],
        documentation=AlgorithmDocumentationResponse.model_validate(asdict(value.documentation)),
    )


def _version(value: AlgorithmVersion) -> AlgorithmVersionResponse:
    return AlgorithmVersionResponse(
        id=value.id,
        algorithm_key=value.algorithm_key,
        version_number=value.version_number,
        name=value.name,
        notes=value.notes,
        parent_version_id=value.parent_version_id,
        code_version=value.code_version,
        schema_version=value.schema_version,
        parameters=value.parameters,
        creator_context=value.creator_context,
        archived=value.archived,
        archived_at=value.archived_at,
        created_at=value.created_at,
    )


def _error(request: Request, exc: AlgorithmServiceError) -> JSONResponse:
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request=request,
    )


@router.get("", response_model=AlgorithmListResponse)
def list_algorithms() -> AlgorithmListResponse:
    return AlgorithmListResponse(
        items=[_definition(value) for value in ALGORITHM_REGISTRY.values() if value.active]
    )


@router.get("/{algorithm_key}", response_model=AlgorithmResponse)
def get_algorithm_detail(algorithm_key: str) -> AlgorithmResponse:
    definition = get_algorithm(algorithm_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Algorithm not found.")
    return _definition(definition)


@router.get("/{algorithm_key}/schema", response_model=ParameterSchemaResponse)
def get_algorithm_schema(algorithm_key: str) -> ParameterSchemaResponse:
    definition = get_algorithm(algorithm_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Algorithm not found.")
    return ParameterSchemaResponse(
        algorithm_key=algorithm_key,
        code_version=definition.code_version,
        schema_version=definition.schema_version,
        schema=parameter_schema(definition),
        schema_hash=schema_hash(definition),
    )


@router.post("/{algorithm_key}/validate", response_model=ParameterValidationResponse)
def validate_parameters(
    algorithm_key: str,
    body: ParameterValidationRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> ParameterValidationResponse | JSONResponse:
    try:
        valid, normalized, errors = AlgorithmService(session).validate(
            algorithm_key, body.parameters
        )
    except AlgorithmServiceError as exc:
        return _error(request, exc)
    return ParameterValidationResponse(
        valid=valid,
        normalized_parameters=normalized,
        errors=[FieldValidationError.model_validate(error) for error in errors],
    )


@router.get("/{algorithm_key}/versions", response_model=AlgorithmVersionListResponse)
def list_versions(
    algorithm_key: str,
    include_archived: bool = False,
    session: Session = Depends(get_db_session),
) -> AlgorithmVersionListResponse:
    if get_algorithm(algorithm_key) is None:
        raise HTTPException(status_code=404, detail="Algorithm not found.")
    values = AlgorithmRepository(session).list_versions(algorithm_key, include_archived)
    return AlgorithmVersionListResponse(items=[_version(value) for value in values])


@router.post("/{algorithm_key}/versions", response_model=AlgorithmVersionResponse, status_code=201)
def create_version(
    algorithm_key: str,
    body: AlgorithmVersionCreateRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> AlgorithmVersionResponse | JSONResponse:
    try:
        value = AlgorithmService(session).create_version(
            algorithm_key, body.name, body.notes, body.parameters, body.parent_version_id
        )
    except AlgorithmServiceError as exc:
        return _error(request, exc)
    return _version(value)


@router.post("/{algorithm_key}/compatibility", response_model=CompatibilityResponse)
def compatibility(
    algorithm_key: str,
    body: CompatibilityRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> CompatibilityResponse | JSONResponse:
    try:
        value = AlgorithmService(session).compatibility(algorithm_key, body.dataset_version_id)
    except AlgorithmServiceError as exc:
        return _error(request, exc)
    return CompatibilityResponse.model_validate(value)


@versions_router.get("/{version_id}", response_model=AlgorithmVersionResponse)
def get_version(
    version_id: UUID, session: Session = Depends(get_db_session)
) -> AlgorithmVersionResponse:
    value = AlgorithmRepository(session).get_version(version_id)
    if value is None:
        raise HTTPException(status_code=404, detail="Algorithm version not found.")
    return _version(value)


@versions_router.get("/{version_id}/usage-summary", response_model=AlgorithmUsageSummaryResponse)
def usage_summary(
    version_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
    cursor: str | None = None,
    limit: int = 25,
) -> AlgorithmUsageSummaryResponse | JSONResponse:
    try:
        return AlgorithmUsageSummaryResponse.model_validate(
            AlgorithmService(session).usage_summary(version_id, cursor=cursor, limit=limit)
        )
    except AlgorithmServiceError as exc:
        return _error(request, exc)


@versions_router.post("/{version_id}/archive", response_model=AlgorithmVersionResponse)
def archive_version(
    version_id: UUID,
    request: Request,
    session: Session = Depends(get_db_session),
) -> AlgorithmVersionResponse | JSONResponse:
    try:
        value = AlgorithmService(session).archive(version_id)
    except AlgorithmServiceError as exc:
        return _error(request, exc)
    return _version(value)
