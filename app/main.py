from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas.system import ErrorResponse, HealthResponse, ReadinessResponse
from app.api.v1.router import router as api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import configure_logging, request_id_context
from app.core.metrics import MetricsRegistry
from app.core.security import (
    MUTATING_METHODS,
    RedisRateLimiter,
    is_public_request,
    rate_limit_policy,
)
from app.db.session import create_session_factory
from app.readiness import ReadinessProbe, probe_dependencies

logger = logging.getLogger(__name__)


def _bounded_metric_route(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        return str(getattr(route, "path", "unmatched"))
    path = request.url.path
    for prefix in (
        "/api/v1/backtests",
        "/api/v1/datasets",
        "/api/v1/dataset-versions",
        "/api/v1/algorithms",
        "/api/v1/algorithm-versions",
        "/api/v1/realtime",
        "/api/v1/auth",
        "/api/v1/experiments",
    ):
        if path.startswith(prefix):
            return f"{prefix}/*"
    return path if path in {"/health", "/ready", "/metrics"} else "unmatched"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("stockbot backend started")

    async def dispatch_loop() -> None:
        from app.application.jobs import dispatch_pending_jobs

        while True:
            try:
                await asyncio.to_thread(
                    dispatch_pending_jobs,
                    app.state.settings,
                    session_factory=app.state.session_factory,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable job dispatcher unavailable; pending intent retained")
            await asyncio.sleep(2)

    dispatcher = (
        asyncio.create_task(dispatch_loop()) if app.state.settings.environment != "test" else None
    )
    try:
        yield
    finally:
        if dispatcher:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        publisher = getattr(app.state, "realtime_publisher", None)
        if publisher is not None:
            publisher.close()
        ticket_store = getattr(app.state, "realtime_ticket_store", None)
        close = getattr(ticket_store, "close", None)
        if close is not None:
            await close()
        rate_limiter = getattr(app.state, "rate_limiter", None)
        close_limiter = getattr(rate_limiter, "close", None)
        if close_limiter is not None:
            await close_limiter()


def create_app(
    *, settings: Settings | None = None, readiness_probe: ReadinessProbe | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="StockBOT Backend",
        description="Platform foundation for reproducible StockBOT research.",
        version="0.1.1",
        lifespan=lifespan,
        responses={
            404: {"model": ErrorResponse, "description": "Resource not found."},
            422: {"model": ErrorResponse, "description": "Request validation failed."},
            500: {"model": ErrorResponse, "description": "Unexpected server error."},
        },
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(settings)
    app.state.readiness_probe = readiness_probe or probe_dependencies
    app.state.metrics = MetricsRegistry()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.include_router(api_v1_router)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    def security_error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {"code": code, "message": message, "details": None},
                "request_id": request.state.request_id,
            },
        )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid4()))
        if len(request_id) > 128 or not request_id.isprintable():
            request_id = str(uuid4())
        request.state.request_id = request_id
        token = request_id_context.set(request_id)
        started = monotonic()
        response: Response
        try:
            path = request.url.path
            method = request.method.upper()
            settings: Settings = request.app.state.settings
            metrics: MetricsRegistry = request.app.state.metrics

            blocked: Response | None = None
            if settings.authentication_required and path not in {"/health", "/ready"}:
                from app.api.v1.realtime import SESSION_COOKIE, _session_store

                session_token = request.cookies.get(SESSION_COOKIE)
                principal = None
                try:
                    if session_token:
                        principal = await _session_store(request).authenticate_session(
                            session_token
                        )
                except Exception:
                    logger.exception("session dependency unavailable")
                    blocked = security_error(
                        request,
                        503,
                        "SECURITY_DEPENDENCY_UNAVAILABLE",
                        "The security dependency is temporarily unavailable.",
                    )
                request.state.principal = principal

                policy = rate_limit_policy(path, method, settings)
                if policy is not None and blocked is None:
                    limiter = getattr(request.app.state, "rate_limiter", None)
                    if limiter is None:
                        limiter = RedisRateLimiter(settings)
                        request.app.state.rate_limiter = limiter
                    client = request.client.host if request.client else "unknown"
                    subject = principal or client
                    try:
                        allowed = await limiter.allow(policy[0], subject, policy[1])
                    except Exception:
                        logger.exception("rate limiter dependency unavailable")
                        metrics.increment("stockbot_rate_limit_dependency_errors_total")
                        blocked = security_error(
                            request,
                            503,
                            "SECURITY_DEPENDENCY_UNAVAILABLE",
                            "The security dependency is temporarily unavailable.",
                        )
                    if blocked is None and not allowed:
                        metrics.increment(
                            "stockbot_rate_limit_rejections_total", {"bucket": policy[0]}
                        )
                        blocked = security_error(
                            request,
                            429,
                            "RATE_LIMIT_EXCEEDED",
                            "Too many requests. Try again later.",
                        )
                        blocked.headers["Retry-After"] = str(
                            settings.security_rate_limit_window_seconds
                        )

                if blocked is None and not is_public_request(path, method) and principal is None:
                    metrics.increment("stockbot_authentication_failures_total")
                    blocked = security_error(
                        request,
                        401,
                        "AUTHENTICATION_REQUIRED",
                        "A valid local session is required.",
                    )

                needs_origin = path != "/api/v1/auth/session" or method == "DELETE"
                if (
                    blocked is None
                    and method in MUTATING_METHODS
                    and session_token
                    and needs_origin
                ):
                    origin = request.headers.get("Origin")
                    if origin not in settings.cors_origins:
                        metrics.increment("stockbot_csrf_rejections_total")
                        blocked = security_error(
                            request,
                            403,
                            "UNTRUSTED_ORIGIN",
                            "The request origin is not allowed.",
                        )
            response = blocked if blocked is not None else await call_next(request)
        finally:
            request_id_context.reset(token)
        duration = monotonic() - started
        route = _bounded_metric_route(request)
        metrics = request.app.state.metrics
        metrics.increment(
            "stockbot_http_requests_total",
            {"method": request.method, "route": route, "status": str(response.status_code)},
        )
        metrics.observe(
            "stockbot_http_request_duration_seconds",
            duration,
            {"method": request.method, "route": route},
        )
        if response.status_code >= 500:
            metrics.increment("stockbot_http_failures_total", {"route": route})
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "route": route,
                "status_code": response.status_code,
                "duration_ms": round(duration * 1000, 3),
            },
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/health", tags=["system"], response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/ready",
        tags=["system"],
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse}},
    )
    async def readiness(request: Request) -> JSONResponse:
        probe_started = monotonic()
        checks = await request.app.state.readiness_probe(settings)
        request.app.state.metrics.observe(
            "stockbot_readiness_probe_duration_seconds", monotonic() - probe_started
        )
        for dependency, status in checks.items():
            request.app.state.metrics.set_gauge(
                "stockbot_dependency_health", 1 if status == "ok" else 0, {"dependency": dependency}
            )
        ready = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @app.get("/metrics", tags=["system"], include_in_schema=False)
    async def metrics(request: Request) -> Response:
        return Response(
            request.app.state.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    original_openapi = app.openapi

    def secured_openapi() -> dict[str, object]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["LocalSessionCookie"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": "stockbot_session",
        }
        schema["security"] = [{"LocalSessionCookie": []}]
        for public_path in ("/health", "/ready", "/api/v1/auth/session"):
            if public_path in schema.get("paths", {}):
                for operation in schema["paths"][public_path].values():
                    if isinstance(operation, dict):
                        operation["security"] = []
        app.openapi_schema = schema
        return schema

    del original_openapi
    app.openapi = secured_openapi  # type: ignore[method-assign]

    return app


app = create_app()
