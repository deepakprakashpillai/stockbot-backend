from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PositiveFloat, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration for the backend foundation."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="STOCKBOT_ENV"
    )
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://stockbot:stockbot@localhost:5432/stockbot"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    realtime_redis_url: str = "redis://localhost:6379/2"
    realtime_key_prefix: str = "stockbot:realtime"
    realtime_ticket_ttl_seconds: PositiveInt = 60
    realtime_outbound_queue_size: PositiveInt = 256
    realtime_replay_limit: PositiveInt = 500
    realtime_session_ttl_seconds: PositiveInt = 8 * 60 * 60
    stockbot_local_access_key: str = "stockbot-local-development-only"
    authentication_required: bool | None = None
    security_rate_limit_window_seconds: PositiveInt = 60
    security_login_limit: PositiveInt = 10
    security_upload_limit: PositiveInt = 10
    security_command_limit: PositiveInt = 60
    security_export_limit: PositiveInt = 30
    storage_root: Path = Path("data")
    cors_origins: list[str] = ["http://localhost:5173"]
    dependency_timeout_seconds: PositiveFloat = 1.0
    dataset_sync_validation_max_bytes: PositiveInt = 5 * 1024 * 1024
    dataset_upload_max_bytes: PositiveInt = 250 * 1024 * 1024

    @model_validator(mode="after")
    def validate_production_connections(self) -> Settings:
        if self.authentication_required is None:
            self.authentication_required = self.environment != "test"
        if self.environment == "production" and "localhost" in self.database_url:
            raise ValueError("DATABASE_URL must not point to localhost in production.")
        if self.environment == "production" and not self.cors_origins:
            raise ValueError("CORS_ORIGINS must contain at least one production origin.")
        if (
            self.environment == "production"
            and self.stockbot_local_access_key == "stockbot-local-development-only"
        ):
            raise ValueError("STOCKBOT_LOCAL_ACCESS_KEY must be changed in production.")
        if self.environment == "production" and len(self.stockbot_local_access_key) < 32:
            raise ValueError(
                "STOCKBOT_LOCAL_ACCESS_KEY must be at least 32 characters in production."
            )
        if self.environment == "production" and self.authentication_required is not True:
            raise ValueError("AUTHENTICATION_REQUIRED cannot be disabled in production.")
        if self.environment == "production" and any(
            "localhost" in url or "127.0.0.1" in url
            for url in (
                self.celery_broker_url,
                self.celery_result_backend,
                self.realtime_redis_url,
            )
        ):
            raise ValueError("Redis URLs must not point to localhost in production.")
        if self.environment == "production" and any(
            not origin.startswith("https://") for origin in self.cors_origins
        ):
            raise ValueError("CORS_ORIGINS must use HTTPS in production.")
        return self

    @property
    def api_version(self) -> str:
        return "v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
