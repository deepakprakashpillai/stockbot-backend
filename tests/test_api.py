from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import Settings
from app.core.errors import unhandled_exception_handler
from app.main import create_app
from tests.conftest import make_test_settings

ReadinessProbe = Callable[[Settings], Awaitable[dict[str, str]]]


def client_for(probe: ReadinessProbe) -> TestClient:
    return TestClient(create_app(settings=make_test_settings(), readiness_probe=probe))


def test_health_is_alive_without_dependency_probe() -> None:
    called = False

    async def probe(_: Settings) -> dict[str, str]:
        nonlocal called
        called = True
        return {"database": "ok", "celery_broker": "ok", "realtime_bus": "ok"}

    with client_for(probe) as client:
        response = client.get("/health", headers={"X-Request-ID": "health-check"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "health-check"
    assert not called


def test_cors_allows_idempotent_browser_commands() -> None:
    async def probe(_: Settings) -> dict[str, str]:
        return {}

    with client_for(probe) as client:
        response = client.options(
            "/api/v1/backtests",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,idempotency-key",
            },
        )

    assert response.status_code == 200
    assert "idempotency-key" in response.headers["Access-Control-Allow-Headers"].lower()


def test_ready_reports_all_dependencies() -> None:
    async def probe(_: Settings) -> dict[str, str]:
        return {"database": "ok", "celery_broker": "ok", "realtime_bus": "ok"}

    with client_for(probe) as client:
        response = client.get("/ready", headers={"X-Request-ID": "ready-check"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "celery_broker": "ok", "realtime_bus": "ok"},
    }
    assert response.headers["X-Request-ID"] == "ready-check"


def test_ready_returns_503_when_a_dependency_is_unavailable() -> None:
    async def probe(_: Settings) -> dict[str, str]:
        return {"database": "ok", "celery_broker": "error", "realtime_bus": "ok"}

    with client_for(probe) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["celery_broker"] == "error"


def test_system_information_is_versioned() -> None:
    async def probe(_: Settings) -> dict[str, str]:
        return {"database": "ok", "celery_broker": "ok", "realtime_bus": "ok"}

    with client_for(probe) as client:
        response = client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.json() == {
        "service": "stockbot-backend",
        "api_version": "v1",
        "environment": "test",
    }


def test_missing_route_uses_the_stable_error_envelope() -> None:
    async def probe(_: Settings) -> dict[str, str]:
        return {"database": "ok", "celery_broker": "ok", "realtime_bus": "ok"}

    with client_for(probe) as client:
        response = client.get("/api/v1/unknown", headers={"X-Request-ID": "missing-route"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["request_id"] == "missing-route"


def test_unhandled_errors_use_the_stable_error_envelope() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "unexpected-error"

    response = asyncio.run(unhandled_exception_handler(request, RuntimeError("boom")))

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected server error occurred.",
            "details": None,
        },
        "request_id": "unexpected-error",
    }
