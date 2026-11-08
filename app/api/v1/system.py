from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas.system import SystemInformationResponse
from app.core.config import Settings

router = APIRouter(tags=["system"])


@router.get("/system", response_model=SystemInformationResponse)
def system_information(request: Request) -> SystemInformationResponse:
    settings: Settings = request.app.state.settings
    return SystemInformationResponse(
        service="stockbot-backend",
        api_version=settings.api_version,
        environment=settings.environment,
    )
