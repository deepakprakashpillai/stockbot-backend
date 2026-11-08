from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_settings_reject_non_positive_dependency_timeout() -> None:
    with pytest.raises(ValidationError):
        Settings(dependency_timeout_seconds=0)


def test_settings_expose_independent_redis_urls() -> None:
    settings = Settings(
        celery_broker_url="redis://localhost:6379/0",
        realtime_redis_url="redis://localhost:6379/2",
    )

    assert settings.celery_broker_url != settings.realtime_redis_url
    assert settings.api_version == "v1"


def test_production_settings_reject_localhost_database() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(environment="production")
