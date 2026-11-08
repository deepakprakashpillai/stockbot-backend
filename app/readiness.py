from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import redis
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import create_database_engine

ReadinessProbe = Callable[[Settings], Awaitable[dict[str, str]]]


def _database_check(settings: Settings) -> str:
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "ok"
    finally:
        engine.dispose()


def _redis_check(url: str, timeout: float) -> str:
    client = redis.Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
    try:
        client.ping()
        return "ok"
    finally:
        client.close()


async def probe_dependencies(settings: Settings) -> dict[str, str]:
    checks = {
        "database": asyncio.to_thread(_database_check, settings),
        "celery_broker": asyncio.to_thread(
            _redis_check, settings.celery_broker_url, settings.dependency_timeout_seconds
        ),
        "realtime_bus": asyncio.to_thread(
            _redis_check, settings.realtime_redis_url, settings.dependency_timeout_seconds
        ),
    }
    results = await asyncio.gather(*checks.values(), return_exceptions=True)
    return {
        name: "ok" if result == "ok" else "error"
        for name, result in zip(checks, results, strict=True)
    }
