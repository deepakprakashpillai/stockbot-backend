"""Renew a worker lease while a synchronous preparation step is running."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.repositories.backtests import BacktestRepository
from app.db.session import create_session_factory

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    def __init__(
        self,
        renew: Callable[[], bool],
        *,
        interval_seconds: float = 15,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        self._renew = renew
        self._interval = interval_seconds
        self._cleanup = cleanup
        self._stop = Event()
        self._failed: BaseException | None = None
        self._thread = Thread(target=self._run, name="backtest-preparation-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._renew():
                    raise RuntimeError("The worker lost its run lease during preparation.")
            except Exception as exc:
                self._failed = exc
                logger.warning("backtest preparation heartbeat failed", exc_info=True)
                self._stop.set()
                return

    def __enter__(self) -> LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        self._thread.join()
        if self._cleanup is not None:
            self._cleanup()
        if exc_type is None and self._failed is not None:
            raise RuntimeError("Run preparation lost its worker lease.") from self._failed


def preparation_heartbeat(settings: Settings, run_id: UUID, owner: str) -> LeaseHeartbeat:
    """Use a separate transaction so long input reads cannot expire ownership."""

    factory: sessionmaker[Session] | None = None

    def renew() -> bool:
        nonlocal factory
        if factory is None:
            factory = create_session_factory(settings)
        with factory() as session:
            if BacktestRepository(session).renew_lease(run_id, owner) is None:
                return False
            session.commit()
            return True

    def cleanup() -> None:
        if factory is not None:
            factory.kw["bind"].dispose()

    return LeaseHeartbeat(renew, cleanup=cleanup)
