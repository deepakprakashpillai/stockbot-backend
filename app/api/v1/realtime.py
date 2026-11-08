from __future__ import annotations

import asyncio
import hmac
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Cookie,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from app.api.schemas.realtime import (
    LocalSessionRequest,
    LocalSessionResponse,
    WebSocketTicketResponse,
)
from app.core.config import Settings
from app.db.models.backtests import BacktestRun, RunEvent
from app.db.repositories.backtests import BacktestRepository
from app.infrastructure.realtime import (
    RedisNotificationStream,
    RedisTicketStore,
    SessionStore,
    TicketStore,
)

router = APIRouter(tags=["realtime"])
SESSION_COOKIE = "stockbot_session"
WEBSOCKET_CONTRACT_VERSION = "1.2.0"

EVENT_TYPES = {
    "BACKTEST_STARTED": "backtest.started",
    "BACKTEST_PROGRESS": "backtest.progress",
    "BACKTEST_TRADE_CREATED": "backtest.trade_created",
    "BACKTEST_COMPLETED": "backtest.completed",
    "BACKTEST_FAILED": "backtest.failed",
    "BACKTEST_CANCELLED": "backtest.cancelled",
    "BACKTEST_JOURNAL_UPDATED": "backtest.journal_updated",
}


def _ticket_store(request: Request) -> TicketStore:
    configured: TicketStore | None = getattr(request.app.state, "realtime_ticket_store", None)
    if configured is None:
        configured = RedisTicketStore(request.app.state.settings)
        request.app.state.realtime_ticket_store = configured
    return configured


def _session_store(request: Request) -> SessionStore:
    # The concrete Redis adapter implements both protocols and tests inject the same shape.
    return cast(SessionStore, _ticket_store(request))


@router.post("/auth/session", response_model=LocalSessionResponse)
async def create_local_session(
    body: LocalSessionRequest, request: Request, response: Response
) -> LocalSessionResponse:
    """Bootstrap the single-user local MVP through a server-side HttpOnly session."""

    settings: Settings = request.app.state.settings
    if not hmac.compare_digest(body.access_key, settings.stockbot_local_access_key):
        raise HTTPException(status_code=401, detail="Invalid local session credentials.")
    token, expires_at = await _session_store(request).issue_session("local-mvp-user")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        max_age=settings.realtime_session_ttl_seconds,
        path="/",
    )
    return LocalSessionResponse(
        authenticated=True, principal="local-mvp-user", expires_at=expires_at
    )


@router.get("/auth/session", response_model=LocalSessionResponse)
async def get_local_session(
    request: Request, stockbot_session: str | None = Cookie(default=None)
) -> LocalSessionResponse:
    principal = (
        await _session_store(request).authenticate_session(stockbot_session)
        if stockbot_session
        else None
    )
    return LocalSessionResponse(authenticated=principal is not None, principal=principal)


@router.delete("/auth/session", status_code=204)
async def delete_local_session(
    request: Request,
    response: Response,
    stockbot_session: str | None = Cookie(default=None),
) -> None:
    if stockbot_session:
        await _session_store(request).delete_session(stockbot_session)
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")


@router.post("/realtime/tickets", response_model=WebSocketTicketResponse)
async def create_websocket_ticket(
    request: Request,
    stockbot_session: str | None = Cookie(default=None),
    local_access_key: str | None = Header(default=None, alias="X-StockBOT-Local-Access-Key"),
) -> WebSocketTicketResponse:
    """Exchange an authenticated session for a one-use socket ticket."""

    settings: Settings = request.app.state.settings
    principal = (
        await _session_store(request).authenticate_session(stockbot_session)
        if stockbot_session
        else None
    )
    if (
        principal is None
        and local_access_key is not None
        and hmac.compare_digest(local_access_key, settings.stockbot_local_access_key)
    ):
        principal = "local-mvp-user"
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid local session credentials.")
    ticket, expires_at = await _ticket_store(request).issue(
        json.dumps({"principal": principal, "session_token": stockbot_session})
    )
    return WebSocketTicketResponse(ticket=ticket, expires_at=expires_at)


def _parse_backtest_channel(channel: object) -> UUID | None:
    if not isinstance(channel, str) or not channel.startswith("backtest:"):
        return None
    try:
        return UUID(channel.removeprefix("backtest:"))
    except ValueError:
        return None


def _event_envelope(run: BacktestRun, event: RunEvent) -> dict[str, Any] | None:
    event_type = EVENT_TYPES.get(event.event_type)
    if event_type is None:
        return None
    data = dict(event.payload)
    data.update({"run_id": str(run.id), "mode": run.mode})
    if run.mode == "PLATFORM_DEMO":
        data.update(
            {
                "synthetic": True,
                "unit": "DEMO_UNITS",
                "financial_pnl_available": False,
            }
        )
    return {
        "contract_version": WEBSOCKET_CONTRACT_VERSION,
        "event_id": str(event.id),
        "sequence": event.sequence,
        "type": event_type,
        "channel": f"backtest:{run.id}",
        "timestamp": event.created_at.isoformat(),
        "data": data,
    }


class GatewayConnection:
    def __init__(
        self, websocket: WebSocket, principal: str, session_token: str | None = None
    ) -> None:
        self.websocket = websocket
        self.principal = principal
        self.session_token = session_token
        settings: Settings = websocket.app.state.settings
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=settings.realtime_outbound_queue_size
        )
        self.replay_limit = settings.realtime_replay_limit
        self.subscriptions: dict[UUID, int] = {}
        self.overloaded = asyncio.Event()

    def _repository(self) -> tuple[Any, BacktestRepository]:
        session = self.websocket.app.state.session_factory()
        return session, BacktestRepository(session)

    async def send_control(self, message: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.overloaded.set()

    async def authorize(self, run_id: UUID) -> bool:
        session, repository = self._repository()
        try:
            # The current product has one authenticated local principal. Run ownership is
            # introduced with the future multi-user identity model.
            return repository.get_run(run_id) is not None and self.principal == "local-mvp-user"
        finally:
            session.close()

    async def replay(self, run_id: UUID, after_sequence: int) -> None:
        session, repository = self._repository()
        try:
            run = repository.get_run(run_id)
            if run is None:
                return
            events, next_cursor = repository.list_events(run_id, after_sequence, self.replay_limit)
            if next_cursor is not None:
                latest = repository.latest_event_sequence(run_id)
                self.subscriptions[run_id] = latest
                await self.send_control(
                    {
                        "type": "snapshot_required",
                        "channel": f"backtest:{run_id}",
                        "after_sequence": after_sequence,
                        "latest_sequence": latest,
                        "reason": "REPLAY_LIMIT_EXCEEDED",
                    }
                )
                return
            for event in events:
                if event.sequence <= self.subscriptions.get(run_id, after_sequence):
                    continue
                envelope = _event_envelope(run, event)
                self.subscriptions[run_id] = event.sequence
                if envelope is not None:
                    metrics = getattr(self.websocket.app.state, "metrics", None)
                    if metrics is not None:
                        metrics.observe(
                            "stockbot_websocket_event_lag_seconds",
                            (datetime.now(UTC) - event.created_at).total_seconds(),
                            {"event_type": envelope["type"]},
                        )
                    await self.send_control(envelope)
        finally:
            session.close()

    async def subscribe(self, channel: object, after_sequence: int = 0) -> None:
        run_id = _parse_backtest_channel(channel)
        if run_id is None or not await self.authorize(run_id):
            await self.send_control(
                {"type": "error", "code": "SUBSCRIPTION_NOT_AUTHORIZED", "channel": channel}
            )
            return
        if run_id not in self.subscriptions and len(self.subscriptions) >= 100:
            await self.send_control({"type": "error", "code": "SUBSCRIPTION_LIMIT_EXCEEDED"})
            return
        self.subscriptions[run_id] = max(self.subscriptions.get(run_id, 0), max(0, after_sequence))
        await self.send_control(
            {
                "type": "subscribed",
                "channel": f"backtest:{run_id}",
                "after_sequence": self.subscriptions[run_id],
            }
        )
        await self.replay(run_id, self.subscriptions[run_id])

    async def receiver(self) -> None:
        while True:
            message = await self.websocket.receive_json()
            if not isinstance(message, dict):
                await self.send_control({"type": "error", "code": "INVALID_MESSAGE"})
                continue
            action = message.get("action")
            if action == "subscribe":
                channels = message.get("channels")
                if not isinstance(channels, list) or not channels:
                    await self.send_control({"type": "error", "code": "INVALID_MESSAGE"})
                    continue
                if len(channels) > 100 or len(self.subscriptions) + len(channels) > 100:
                    await self.send_control(
                        {"type": "error", "code": "SUBSCRIPTION_LIMIT_EXCEEDED"}
                    )
                    continue
                for channel in channels:
                    await self.subscribe(channel)
            elif action == "resume":
                after = message.get("after_sequence")
                if not isinstance(after, int) or after < 0:
                    await self.send_control({"type": "error", "code": "INVALID_MESSAGE"})
                else:
                    await self.subscribe(message.get("channel"), after)
            elif action == "unsubscribe":
                run_id = _parse_backtest_channel(message.get("channel"))
                if run_id is not None:
                    self.subscriptions.pop(run_id, None)
                await self.send_control({"type": "unsubscribed", "channel": message.get("channel")})
            elif action == "ping":
                await self.send_control(
                    {"type": "pong", "timestamp": datetime.now(UTC).isoformat()}
                )
            elif action == "pong":
                continue
            else:
                await self.send_control({"type": "error", "code": "INVALID_MESSAGE"})

    async def sender(self) -> None:
        while True:
            message = await self.queue.get()
            await self.websocket.send_json(message)

    async def notifications(self) -> None:
        factory = getattr(self.websocket.app.state, "realtime_notification_stream_factory", None)
        stream = (
            factory() if factory else RedisNotificationStream(self.websocket.app.state.settings)
        )
        async for run_id, sequence in stream.notifications():
            if run_id in self.subscriptions and sequence > self.subscriptions[run_id]:
                await self.replay(run_id, self.subscriptions[run_id])

    async def backpressure_guard(self) -> None:
        await self.overloaded.wait()
        await self.websocket.close(code=1013, reason="Outbound queue capacity exceeded; resume.")

    async def session_guard(self) -> None:
        while True:
            if self.session_token:
                store = cast(SessionStore, self.websocket.app.state.realtime_ticket_store)
                if await store.authenticate_session(self.session_token) != self.principal:
                    await self.websocket.close(code=4401, reason="Session expired or revoked.")
                    return
            await asyncio.sleep(2)

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self.receiver()),
            asyncio.create_task(self.sender()),
            asyncio.create_task(self.notifications()),
            asyncio.create_task(self.backpressure_guard()),
            asyncio.create_task(self.session_guard()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/ws")
async def websocket_gateway(websocket: WebSocket, ticket: str) -> None:
    settings: Settings = websocket.app.state.settings
    if (
        settings.authentication_required
        and websocket.headers.get("origin") not in settings.cors_origins
    ):
        await websocket.close(code=4403, reason="Untrusted WebSocket origin.")
        return
    store: TicketStore | None = getattr(websocket.app.state, "realtime_ticket_store", None)
    if store is None:
        store = RedisTicketStore(websocket.app.state.settings)
        websocket.app.state.realtime_ticket_store = store
    principal = await store.consume(ticket)
    session_token: str | None = None
    if principal and principal.startswith("{"):
        try:
            payload = json.loads(principal)
            principal = payload.get("principal")
            session_token = payload.get("session_token")
            if not isinstance(principal, str) or (
                session_token is not None and not isinstance(session_token, str)
            ):
                principal = None
        except (ValueError, AttributeError):
            principal = None
    if settings.authentication_required and (
        not session_token
        or await cast(SessionStore, store).authenticate_session(session_token) != principal
    ):
        principal = None
    if principal is None:
        metrics = getattr(websocket.app.state, "metrics", None)
        if metrics is not None:
            metrics.increment("stockbot_websocket_authentication_failures_total")
        await websocket.close(code=4401, reason="Invalid or expired WebSocket ticket.")
        return
    await websocket.accept()
    metrics = getattr(websocket.app.state, "metrics", None)
    if metrics is not None:
        metrics.increment("stockbot_websocket_connections_total")
        metrics.add_gauge("stockbot_websocket_active_connections", 1)
    try:
        await GatewayConnection(websocket, principal, session_token).run()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        if metrics is not None:
            metrics.add_gauge("stockbot_websocket_active_connections", -1)
