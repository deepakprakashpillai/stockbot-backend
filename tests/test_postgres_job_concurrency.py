"""Opt-in real PostgreSQL checks. CI supplies the isolated test database URL."""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.application.jobs import dispatch_pending_jobs, enqueue_job
from app.db.models.jobs import JobDispatch
from app.workers.celery_app import celery_app
from tests.conftest import make_test_settings


def test_two_dispatchers_do_not_publish_one_locked_intent_twice(postgres_factory, monkeypatch):
    with postgres_factory() as session:
        job = enqueue_job(session, "DATASET_VALIDATION", uuid4())
        session.commit()
        identity = job.id
    publishing = threading.Event()
    release = threading.Event()
    sent = []

    def send(_name, **kwargs):
        sent.append(kwargs)
        publishing.set()
        assert release.wait(10), "Test dispatcher was not released"

    monkeypatch.setattr(celery_app, "send_task", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            dispatch_pending_jobs, make_test_settings(), session_factory=postgres_factory
        )
        try:
            assert publishing.wait(10)
            second = pool.submit(
                dispatch_pending_jobs, make_test_settings(), session_factory=postgres_factory
            )
            assert second.result(timeout=10) == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    assert len(sent) == 1
    with postgres_factory() as session:
        assert session.get(JobDispatch, identity).status == "DELIVERED"


def test_rolled_back_work_has_no_deliverable_intent_and_resource_intent_is_unique(postgres_factory):
    resource_id = uuid4()
    with postgres_factory() as session:
        enqueue_job(session, "DATASET_VALIDATION", resource_id)
        session.flush()
        session.rollback()
    with postgres_factory() as session:
        assert (
            session.scalar(select(JobDispatch).where(JobDispatch.resource_id == resource_id))
            is None
        )
        enqueue_job(session, "DATASET_VALIDATION", resource_id)
        session.commit()
        enqueue_job(session, "DATASET_VALIDATION", resource_id)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
