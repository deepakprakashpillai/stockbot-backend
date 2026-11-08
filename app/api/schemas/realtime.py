from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LocalSessionRequest(BaseModel):
    access_key: str


class LocalSessionResponse(BaseModel):
    authenticated: bool
    principal: str | None = None
    expires_at: datetime | None = None


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_at: datetime
    websocket_path: str = "/api/v1/ws"
