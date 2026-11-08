from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import backtests as backtest_routes
from app.api.v1 import realtime
from app.api.v1.realtime import GatewayConnection
from app.application.backtests import BacktestService
from app.infrastructure import realtime as realtime_infra
from app.main import create_app
from tests.conftest import make_test_settings


class FakeAuthStore:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.tickets: dict[str, str] = {}
        self.next = 0

    async def issue_session(self, principal: str):
        self.next += 1
        token = f"session-{self.next}"
        self.sessions[token] = principal
        return token, datetime.now(UTC) + timedelta(hours=8)

    async def authenticate_session(self, token: str):
        return self.sessions.get(token)

    async def delete_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    async def issue(self, principal: str):
        self.next += 1
        ticket = f"ticket-{self.next}"
        self.tickets[ticket] = principal
        return ticket, datetime.now(UTC) + timedelta(seconds=60)

    async def consume(self, ticket: str):
        return self.tickets.pop(ticket, None)


class QuietNotifications:
    async def notifications(self):
        await asyncio.Event().wait()
        if False:
            yield uuid4(), 1


def test_active_socket_guard_closes_revoked_session():
    store = FakeAuthStore()
    closed = []

    async def close(**kwargs):
        closed.append(kwargs)

    socket = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=make_test_settings(), realtime_ticket_store=store)
        ),
        close=close,
    )
    asyncio.run(GatewayConnection(socket, "local-mvp-user", "revoked-token").session_guard())
    assert closed[0]["code"] == 4401


def test_issued_ticket_is_unusable_after_logout_and_from_untrusted_origin():
    settings = make_test_settings().model_copy(update={"authentication_required": True})
    app = create_app(settings=settings)
    store = FakeAuthStore()
    app.state.realtime_ticket_store = store

    class Limiter:
        async def allow(self, *_args):
            return True

    app.state.rate_limiter = Limiter()
    with TestClient(app) as client:
        client.post("/api/v1/auth/session", json={"access_key": settings.stockbot_local_access_key})
        ticket = client.post(
            "/api/v1/realtime/tickets", headers={"Origin": settings.cors_origins[0]}
        ).json()["ticket"]
        client.delete("/api/v1/auth/session", headers={"Origin": settings.cors_origins[0]})
        with pytest.raises(WebSocketDisconnect) as expired:
            with client.websocket_connect(
                f"/api/v1/ws?ticket={ticket}", headers={"Origin": settings.cors_origins[0]}
            ):
                pass
        assert expired.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as origin:
            with client.websocket_connect(
                "/api/v1/ws?ticket=unused", headers={"Origin": "https://untrusted.example"}
            ):
                pass
        assert origin.value.code == 4403


def test_http_only_session_authenticates_ticket_and_logout() -> None:
    settings = make_test_settings()
    app = create_app(settings=settings)
    store = FakeAuthStore()
    app.state.realtime_ticket_store = store
    app.state.realtime_notification_stream_factory = QuietNotifications

    with TestClient(app) as client:
        assert client.get("/api/v1/auth/session").json() == {
            "authenticated": False,
            "principal": None,
            "expires_at": None,
        }
        assert client.post("/api/v1/auth/session", json={"access_key": "wrong"}).status_code == 401
        login = client.post(
            "/api/v1/auth/session",
            json={"access_key": settings.stockbot_local_access_key},
        )
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]

        ticket = client.post("/api/v1/realtime/tickets")
        assert ticket.status_code == 200
        assert ticket.json()["websocket_path"] == "/api/v1/ws"
        assert client.get("/api/v1/auth/session").json()["authenticated"] is True
        assert client.delete("/api/v1/auth/session").status_code == 204
        assert client.post("/api/v1/realtime/tickets").status_code == 401


def test_ticket_is_single_use_and_invalid_ticket_is_rejected() -> None:
    app = create_app(settings=make_test_settings())
    store = FakeAuthStore()
    store.tickets["one-use"] = "local-mvp-user"
    app.state.realtime_ticket_store = store
    app.state.realtime_notification_stream_factory = QuietNotifications

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws?ticket=one-use") as socket:
            socket.send_json({"action": "ping"})
            assert socket.receive_json()["type"] == "pong"
        with pytest.raises(WebSocketDisconnect) as reused:
            with client.websocket_connect("/api/v1/ws?ticket=one-use"):
                pass
        assert reused.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as invalid:
            with client.websocket_connect("/api/v1/ws?ticket=expired"):
                pass
        assert invalid.value.code == 4401


def test_subscribe_resume_replays_in_order_without_duplicates(monkeypatch) -> None:
    app = create_app(settings=make_test_settings())
    store = FakeAuthStore()
    store.tickets["replay"] = "local-mvp-user"
    app.state.realtime_ticket_store = store
    app.state.realtime_notification_stream_factory = QuietNotifications
    run_id = uuid4()
    run = SimpleNamespace(id=run_id, mode="PLATFORM_DEMO")
    events = [
        SimpleNamespace(
            id=uuid4(),
            run_id=run_id,
            sequence=1,
            event_type="BACKTEST_STARTED",
            payload={},
            created_at=datetime.now(UTC),
        ),
        SimpleNamespace(
            id=uuid4(),
            run_id=run_id,
            sequence=2,
            event_type="BACKTEST_TRADE_CREATED",
            payload={"trade_sequence": 1},
            created_at=datetime.now(UTC),
        ),
        SimpleNamespace(
            id=uuid4(),
            run_id=run_id,
            sequence=3,
            event_type="BACKTEST_COMPLETED",
            payload={},
            created_at=datetime.now(UTC),
        ),
    ]

    class Session:
        def close(self):
            pass

    class Repository:
        def get_run(self, requested):
            return run if requested == run_id else None

        def list_events(self, requested, cursor, _limit):
            return [
                event for event in events if requested == run_id and event.sequence > cursor
            ], None

        def latest_event_sequence(self, _requested):
            return 3

    monkeypatch.setattr(GatewayConnection, "_repository", lambda _self: (Session(), Repository()))

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws?ticket=replay") as socket:
            channel = f"backtest:{run_id}"
            socket.send_json({"action": "subscribe", "channels": [channel]})
            assert socket.receive_json() == {
                "type": "subscribed",
                "channel": channel,
                "after_sequence": 0,
            }
            received = [socket.receive_json() for _ in events]
            assert [item["sequence"] for item in received] == [1, 2, 3]
            assert received[1]["type"] == "backtest.trade_created"
            assert received[1]["data"]["synthetic"] is True
            assert received[1]["data"]["unit"] == "DEMO_UNITS"
            assert received[1]["data"]["financial_pnl_available"] is False

            socket.send_json({"action": "resume", "channel": channel, "after_sequence": 2})
            resumed = socket.receive_json()
            assert resumed["type"] == "subscribed"
            socket.send_json({"action": "ping"})
            assert socket.receive_json()["type"] == "pong"


def test_unauthorized_channels_do_not_reveal_existence(monkeypatch) -> None:
    app = create_app(settings=make_test_settings())
    store = FakeAuthStore()
    store.tickets["authz"] = "local-mvp-user"
    app.state.realtime_ticket_store = store
    app.state.realtime_notification_stream_factory = QuietNotifications

    class Session:
        def close(self):
            pass

    class Repository:
        def get_run(self, _requested):
            return None

    monkeypatch.setattr(GatewayConnection, "_repository", lambda _self: (Session(), Repository()))

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws?ticket=authz") as socket:
            socket.send_json({"action": "subscribe", "channels": [f"backtest:{uuid4()}"]})
            missing = socket.receive_json()
            socket.send_json({"action": "resume", "channel": "not-a-channel", "after_sequence": 0})
            malformed = socket.receive_json()
            assert missing["code"] == malformed["code"] == "SUBSCRIPTION_NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_bounded_queue_marks_slow_connection_for_disconnect() -> None:
    class Socket:
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(realtime_outbound_queue_size=1, realtime_replay_limit=5)
            )
        )

    connection = GatewayConnection(Socket(), "local-mvp-user")  # type: ignore[arg-type]
    await connection.send_control({"type": "first"})
    await connection.send_control({"type": "terminal"})
    assert connection.queue.qsize() == 1
    assert connection.overloaded.is_set()


def test_event_envelope_ignores_non_public_events_and_enforces_demo_safeguards() -> None:
    run = SimpleNamespace(id=uuid4(), mode="PLATFORM_DEMO")
    hidden = SimpleNamespace(event_type="BACKTEST_QUEUED")
    assert realtime._event_envelope(run, hidden) is None
    event = SimpleNamespace(
        id=uuid4(),
        sequence=7,
        event_type="BACKTEST_COMPLETED",
        payload={"synthetic": False, "unit": "INR", "financial_pnl_available": True},
        created_at=datetime.now(UTC),
    )
    envelope = realtime._event_envelope(run, event)
    assert envelope is not None
    assert envelope["contract_version"] == "1.2.0"
    assert envelope["data"]["synthetic"] is True
    assert envelope["data"]["unit"] == "DEMO_UNITS"
    assert envelope["data"]["financial_pnl_available"] is False


@pytest.mark.asyncio
async def test_redis_auth_store_hashes_keys_and_consumes_ticket_once(monkeypatch) -> None:
    class Client:
        def __init__(self):
            self.values = {}
            self.expirations = {}
            self.closed = False

        async def set(self, key, value, **kwargs):
            self.values[key] = value
            self.expirations[key] = kwargs.get("ex")
            return True

        async def get(self, key):
            return self.values.get(key)

        async def getdel(self, key):
            return self.values.pop(key, None)

        async def delete(self, key):
            self.values.pop(key, None)

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(realtime_infra.async_redis.Redis, "from_url", lambda *_a, **_k: client)
    store = realtime_infra.RedisTicketStore(make_test_settings())
    session, session_expiry = await store.issue_session("local-mvp-user")
    assert session_expiry > datetime.now(UTC)
    assert all(session not in key for key in client.values)
    assert (
        list(client.expirations.values())[-1] == make_test_settings().realtime_session_ttl_seconds
    )
    assert await store.authenticate_session(session) == "local-mvp-user"
    await store.delete_session(session)
    assert await store.authenticate_session(session) is None

    ticket, ticket_expiry = await store.issue("local-mvp-user")
    assert ticket_expiry > datetime.now(UTC)
    assert all(ticket not in key for key in client.values)
    assert list(client.expirations.values())[-1] == make_test_settings().realtime_ticket_ttl_seconds
    assert await store.consume(ticket) == "local-mvp-user"
    assert await store.consume(ticket) is None
    await store.close()
    assert client.closed is True


def test_redis_publisher_uses_realtime_prefix_and_publish_failure_is_safe(monkeypatch) -> None:
    class Client:
        def __init__(self):
            self.messages = []
            self.closed = False
            self.fail = False

        def publish(self, channel, payload):
            if self.fail:
                raise realtime_infra.redis.RedisError("offline")
            self.messages.append((channel, json.loads(payload)))

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(realtime_infra.redis.Redis, "from_url", lambda *_a, **_k: client)
    publisher = realtime_infra.RedisRunEventPublisher(make_test_settings())
    run_id = uuid4()
    publisher.publish(run_id, 4)
    assert client.messages == [
        (f"stockbot:realtime:run:{run_id}", {"run_id": str(run_id), "sequence": 4})
    ]
    client.fail = True
    publisher.publish(run_id, 5)
    publisher.close()
    assert client.closed is True
    realtime_infra.NullRunEventPublisher().publish(run_id, 6)


@pytest.mark.asyncio
async def test_notification_stream_ignores_malformed_payload_and_closes(monkeypatch) -> None:
    run_id = uuid4()

    class PubSub:
        def __init__(self):
            self.messages = [
                {"data": "not-json"},
                {"data": json.dumps({"run_id": str(run_id), "sequence": 9})},
            ]
            self.closed = False

        async def psubscribe(self, _pattern):
            pass

        async def get_message(self, **_kwargs):
            return self.messages.pop(0)

        async def aclose(self):
            self.closed = True

    class Client:
        def __init__(self):
            self.subscription = PubSub()
            self.closed = False

        def pubsub(self):
            return self.subscription

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(realtime_infra.async_redis.Redis, "from_url", lambda *_a, **_k: client)
    stream = realtime_infra.RedisNotificationStream(make_test_settings())
    iterator = stream.notifications()
    assert await anext(iterator) == (run_id, 9)
    await iterator.aclose()
    assert client.subscription.closed is client.closed is True


def test_api_publisher_is_app_scoped_and_closed_at_shutdown(monkeypatch) -> None:
    class Publisher:
        instances = 0

        def __init__(self, _settings):
            Publisher.instances += 1
            self.closed = False

        def publish(self, _run_id, _sequence):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(backtest_routes, "RedisRunEventPublisher", Publisher)
    app = create_app(settings=make_test_settings())
    with TestClient(app):
        first = backtest_routes._service(SimpleNamespace(app=app), SimpleNamespace()).publisher
        second = backtest_routes._service(SimpleNamespace(app=app), SimpleNamespace()).publisher
        assert first is second
        assert Publisher.instances == 1
    assert first.closed is True


def test_publish_failure_cannot_undo_durable_commit() -> None:
    class Session:
        committed = False

        def commit(self):
            self.committed = True

    class Publisher:
        def publish(self, _run_id, _sequence):
            raise RuntimeError("redis unavailable")

    session = Session()
    service = object.__new__(BacktestService)
    service.session = session  # type: ignore[assignment]
    service.publisher = Publisher()  # type: ignore[assignment]
    service._commit_and_publish(SimpleNamespace(run_id=uuid4(), sequence=2))
    assert session.committed is True
