from __future__ import annotations

from hashlib import sha256
from typing import Protocol

import redis.asyncio as async_redis

from app.core.config import Settings


class RateLimiter(Protocol):
    async def allow(self, bucket: str, subject: str, limit: int) -> bool: ...


class RedisRateLimiter:
    """Fixed-window limiter; keys contain only hashes of client/session identifiers."""

    def __init__(self, settings: Settings) -> None:
        self.client = async_redis.Redis.from_url(
            settings.realtime_redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
        )
        self.prefix = f"{settings.realtime_key_prefix}:rate:"
        self.window = settings.security_rate_limit_window_seconds

    async def allow(self, bucket: str, subject: str, limit: int) -> bool:
        digest = sha256(subject.encode()).hexdigest()
        key = f"{self.prefix}{bucket}:{digest}"
        count = await self.client.eval(
            "local n=redis.call('INCR',KEYS[1]); "
            "if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            key,
            self.window,
        )
        return bool(count <= limit)

    async def close(self) -> None:
        await self.client.aclose()


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_PATHS = frozenset({"/health", "/ready"})
PUBLIC_API_PATHS = frozenset({"/api/v1/auth/session"})


def is_public_request(path: str, method: str) -> bool:
    return method == "OPTIONS" or path in PUBLIC_PATHS or path in PUBLIC_API_PATHS


def rate_limit_policy(path: str, method: str, settings: Settings) -> tuple[str, int] | None:
    if method == "POST" and path == "/api/v1/auth/session":
        return "login", settings.security_login_limit
    if method == "POST" and (
        path == "/api/v1/datasets"
        or path == "/api/v1/dataset-upload-sessions"
        or path.endswith("/versions")
    ):
        if path.startswith("/api/v1/datasets"):
            return "dataset_upload", settings.security_upload_limit
        if path.startswith("/api/v1/dataset-upload-sessions"):
            return "dataset_upload", settings.security_upload_limit
        if path.startswith("/api/v1/algorithms"):
            return "algorithm_version", settings.security_command_limit
    if path.startswith("/api/v1/backtests"):
        if method == "GET" and (
            "/exports/" in path or path.endswith("/download") or path.endswith("/export")
        ):
            return "export", settings.security_export_limit
        if method == "POST":
            return "backtest_command", settings.security_command_limit
    if path.startswith("/api/v1/experiments") and method == "POST":
        return "experiment_command", settings.security_command_limit
    if path == "/api/v1/realtime/tickets" and method == "POST":
        return "realtime_ticket", settings.security_command_limit
    return None
