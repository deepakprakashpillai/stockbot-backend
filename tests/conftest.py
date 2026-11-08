from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import Settings
from app.db.base import Base


def make_test_settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+psycopg://stockbot:stockbot@localhost:5432/stockbot_test",
        celery_broker_url="redis://localhost:6379/10",
        celery_result_backend="redis://localhost:6379/11",
        realtime_redis_url="redis://localhost:6379/12",
        cors_origins=["http://localhost:5173"],
        stockbot_local_access_key="stockbot-local-development-only",
        authentication_required=False,
    )


@pytest.fixture
def postgres_factory():
    url = os.environ.get("STOCKBOT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set STOCKBOT_TEST_DATABASE_URL to run real PostgreSQL concurrency checks.")
    schema = "stockbot_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()
