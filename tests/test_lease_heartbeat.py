from __future__ import annotations

from threading import Event

import pytest

from app.application.lease_heartbeat import LeaseHeartbeat


def test_preparation_heartbeat_renews_until_context_finishes() -> None:
    renewed = Event()
    calls = 0

    def renew() -> bool:
        nonlocal calls
        calls += 1
        renewed.set()
        return True

    with LeaseHeartbeat(renew, interval_seconds=0.005):
        assert renewed.wait(1)

    count_at_exit = calls
    assert count_at_exit >= 1


def test_preparation_heartbeat_fences_lost_lease() -> None:
    attempted = Event()

    def lost_lease() -> bool:
        attempted.set()
        return False

    with pytest.raises(RuntimeError, match="lost its worker lease"):
        with LeaseHeartbeat(lost_lease, interval_seconds=0.005):
            assert attempted.wait(1)
