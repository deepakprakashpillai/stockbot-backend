from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


def error_response(
    *, status_code: int, code: str, message: str, request: Request, details: Any = None
) -> JSONResponse:
    from fastapi.encoders import jsonable_encoder

    body: dict[str, Any] = {
        "error": {"code": code, "message": message, "details": jsonable_encoder(details)},
        "request_id": getattr(request.state, "request_id", "unknown"),
    }
    return JSONResponse(status_code=status_code, content=body)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    message = exc.detail if isinstance(exc.detail, str) else "Request could not be completed."
    return error_response(
        status_code=exc.status_code,
        code="NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
        message=message,
        request=request,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    return error_response(
        status_code=422,
        code="REQUEST_VALIDATION_ERROR",
        message="The request is invalid.",
        details=exc.errors(),
        request=request,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled request error", exc_info=exc)
    return error_response(
        status_code=500,
        code="INTERNAL_SERVER_ERROR",
        message="An unexpected server error occurred.",
        request=request,
    )
