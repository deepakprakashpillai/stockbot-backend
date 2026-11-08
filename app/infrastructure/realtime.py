from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID

import redis
import redis.asyncio as async_redis

from app.core.config import Settings

logger = logging.getLogger(__name__)


class TicketStore(Protocol):
    async def issue(self, principal: str) -> tuple[str, datetime]: ...

    async def consume(self, ticket: str) -> str | None: ...


class SessionStore(Protocol):
    async def issue_session(self, principal: str) -> tuple[str, datetime]: ...

    async def authenticate_session(self, token: str) -> str | None: ...

    async def delete_session(self, token: str) -> None: ...


class RedisTicketStore:
    """Short-lived tickets consumed atomically with Redis GETDEL."""

    def __init__(self, settings: Settings) -> None:
        self.client = async_redis.Redis.from_url(
            settings.realtime_redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
        )
        self.prefix = f"{settings.realtime_key_prefix}:ticket:"
        self.session_prefix = f"{settings.realtime_key_prefix}:session:"
        self.ttl = settings.realtime_ticket_ttl_seconds
        self.session_ttl = settings.realtime_session_ttl_seconds

    def _session_key(self, token: str) -> str:
        return f"{self.session_prefix}{sha256(token.encode()).hexdigest()}"

    def _ticket_key(self, ticket: str) -> str:
        return f"{self.prefix}{sha256(ticket.encode()).hexdigest()}"

    async def issue_session(self, principal: str) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.session_ttl)
        await self.client.set(self._session_key(token), principal, ex=self.session_ttl, nx=True)
        return token, expires_at

    async def authenticate_session(self, token: str) -> str | None:
        value = await self.client.get(self._session_key(token))
        return str(value) if value is not None else None

    async def delete_session(self, token: str) -> None:
        await self.client.delete(self._session_key(token))

    async def issue(self, principal: str) -> tuple[str, datetime]:
        ticket = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.ttl)
        await self.client.set(self._ticket_key(ticket), principal, ex=self.ttl, nx=True)
        return ticket, expires_at

    async def consume(self, ticket: str) -> str | None:
        value = await self.client.getdel(self._ticket_key(ticket))
        return str(value) if value is not None else None

    async def close(self) -> None:
        await self.client.aclose()


class RunEventPublisher(Protocol):
    def publish(self, run_id: UUID, sequence: int) -> None: ...


class NullRunEventPublisher:
    def publish(self, run_id: UUID, sequence: int) -> None:
        del run_id, sequence


class RedisRunEventPublisher:
    """Best-effort wake-up only; PostgreSQL contains the event itself."""

    def __init__(self, settings: Settings) -> None:
        self.client = redis.Redis.from_url(
            settings.realtime_redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
        )
        self.prefix = f"{settings.realtime_key_prefix}:run:"

    def publish(self, run_id: UUID, sequence: int) -> None:
        try:
            self.client.publish(
                f"{self.prefix}{run_id}",
                json.dumps({"run_id": str(run_id), "sequence": sequence}),
            )
        except redis.RedisError:
            logger.warning(
                "real-time notification failed; durable replay remains available",
                extra={"run_id": str(run_id), "sequence": sequence},
                exc_info=True,
            )

    def close(self) -> None:
        self.client.close()


class RedisNotificationStream:
    """One Redis pub/sub consumer per gateway connection for the backtesting MVP."""

    def __init__(self, settings: Settings) -> None:
        self.client = async_redis.Redis.from_url(
            settings.realtime_redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
        )
        self.pattern = f"{settings.realtime_key_prefix}:run:*"

    async def notifications(self) -> AsyncIterator[tuple[UUID, int]]:
        pubsub = self.client.pubsub()
        await pubsub.psubscribe(self.pattern)
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                try:
                    payload = json.loads(message["data"])
                    yield UUID(payload["run_id"]), int(payload["sequence"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    logger.warning("ignored malformed real-time notification")
        finally:
            await pubsub.aclose()
            await self.client.aclose()
