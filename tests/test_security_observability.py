from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.core.metrics import MetricsRegistry
from app.core.security import RedisRateLimiter, rate_limit_policy
from app.main import create_app
from tests.conftest import make_test_settings


class FakeAuthStore:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.tickets: dict[str, str] = {}

    async def issue_session(self, principal: str):
        self.sessions["session-token"] = principal
        return "session-token", datetime.now(UTC) + timedelta(hours=8)

    async def authenticate_session(self, token: str):
        return self.sessions.get(token)

    async def delete_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    async def issue(self, principal: str):
        self.tickets["ticket-token"] = principal
        return "ticket-token", datetime.now(UTC) + timedelta(seconds=60)

    async def consume(self, ticket: str):
        return self.tickets.pop(ticket, None)


class FakeRateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str, int]] = []

    async def allow(self, bucket: str, subject: str, limit: int) -> bool:
        self.calls.append((bucket, subject, limit))
        return self.allowed


def secured_client(*, allowed: bool = True) -> tuple[TestClient, FakeAuthStore, FakeRateLimiter]:
    settings = make_test_settings()
    settings.authentication_required = True
    app = create_app(settings=settings)
    store = FakeAuthStore()
    limiter = FakeRateLimiter(allowed)
    app.state.realtime_ticket_store = store
    app.state.rate_limiter = limiter
    return TestClient(app), store, limiter


def test_only_health_readiness_and_session_bootstrap_are_public() -> None:
    client, _, _ = secured_client()
    with client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code in {200, 503}
        assert client.get("/api/v1/auth/session").status_code == 200
        assert client.get("/api/v1/system").status_code == 401
        assert client.get("/metrics").status_code == 401


def test_session_protects_business_routes_and_origin_protects_cookie_commands() -> None:
    client, _, _ = secured_client()
    with client:
        login = client.post(
            "/api/v1/auth/session",
            json={"access_key": "stockbot-local-development-only"},
        )
        assert login.status_code == 200
        assert client.get("/api/v1/system").status_code == 200

        rejected = client.post("/api/v1/realtime/tickets")
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "UNTRUSTED_ORIGIN"

        accepted = client.post(
            "/api/v1/realtime/tickets", headers={"Origin": "http://localhost:5173"}
        )
        assert accepted.status_code == 200
        assert accepted.json()["ticket"] == "ticket-token"


def test_sensitive_command_rate_limit_is_bounded_and_has_retry_header() -> None:
    client, _, limiter = secured_client(allowed=False)
    with client:
        response = client.post(
            "/api/v1/auth/session",
            json={"access_key": "stockbot-local-development-only"},
        )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert limiter.calls[0][0] == "login"


def test_security_headers_and_prometheus_metrics_are_emitted() -> None:
    client, _, _ = secured_client()
    with client:
        client.post(
            "/api/v1/auth/session",
            json={"access_key": "stockbot-local-development-only"},
        )
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "stockbot_http_requests_total" in response.text


def test_openapi_declares_cookie_security_and_public_exceptions() -> None:
    schema = create_app(settings=make_test_settings()).openapi()
    assert schema["components"]["securitySchemes"]["LocalSessionCookie"]["in"] == "cookie"
    assert schema["security"] == [{"LocalSessionCookie": []}]
    assert schema["paths"]["/health"]["get"]["security"] == []
    assert schema["paths"]["/api/v1/auth/session"]["post"]["security"] == []


def test_production_security_cannot_be_disabled_or_use_local_dependencies() -> None:
    base = {
        "environment": "production",
        "database_url": "postgresql+psycopg://user:pass@db.internal/stockbot",
        "celery_broker_url": "redis://redis.internal:6379/0",
        "celery_result_backend": "redis://redis.internal:6379/1",
        "realtime_redis_url": "redis://redis.internal:6379/2",
        "cors_origins": ["https://stockbot.example"],
        "stockbot_local_access_key": "a-strong-production-key-with-32-chars",
    }
    assert Settings(**base).authentication_required is True
    with pytest.raises(ValidationError, match="AUTHENTICATION_REQUIRED"):
        Settings(**base, authentication_required=False)


def test_structured_log_formatter_preserves_supported_context_only() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "done", (), None)
    record.run_id = "run-1"
    record.task_id = "task-1"
    record.password = "must-not-appear"
    payload = json.loads(formatter.format(record))
    assert payload["run_id"] == "run-1"
    assert payload["task_id"] == "task-1"
    assert "password" not in payload


def test_metrics_registry_exports_counter_gauge_and_summary() -> None:
    registry = MetricsRegistry()
    registry.increment("example_total", {"kind": "ok"})
    registry.set_gauge("example_active", 2)
    registry.observe("example_seconds", 0.25)
    rendered = registry.render()
    assert 'example_total{kind="ok"} 1' in rendered
    assert "example_active 2" in rendered
    assert "example_seconds_count 1" in rendered
    assert "example_seconds_sum 0.25" in rendered


def test_rate_limit_policy_covers_expensive_command_families() -> None:
    settings = make_test_settings()
    assert rate_limit_policy("/api/v1/auth/session", "POST", settings)[0] == "login"
    assert rate_limit_policy("/api/v1/datasets", "POST", settings)[0] == "dataset_upload"
    assert (
        rate_limit_policy("/api/v1/algorithms/platform_demo/versions", "POST", settings)[0]
        == "algorithm_version"
    )
    assert (
        rate_limit_policy("/api/v1/backtests/comparisons", "POST", settings)[0]
        == "backtest_command"
    )
    assert rate_limit_policy("/api/v1/backtests/run-id/export", "GET", settings)[0] == "export"
    assert rate_limit_policy("/api/v1/datasets", "GET", settings) is None


@pytest.mark.asyncio
async def test_redis_rate_limiter_hashes_subject_and_uses_atomic_expiry() -> None:
    settings = make_test_settings()
    limiter = RedisRateLimiter(settings)

    class Client:
        args: tuple[object, ...] | None = None

        async def eval(self, *args: object) -> int:
            self.args = args
            return 1

        async def aclose(self) -> None:
            return None

    client = Client()
    await limiter.client.aclose()
    limiter.client = client  # type: ignore[assignment]
    assert await limiter.allow("login", "raw-client-identity", 10) is True
    assert client.args is not None
    key = str(client.args[2])
    assert "raw-client-identity" not in key
    assert "login" in key
    await limiter.close()
