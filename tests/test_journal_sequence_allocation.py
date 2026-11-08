from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.orm import Session

from app.db.repositories.backtests import BacktestRepository


def test_journal_sequence_uses_one_lookup_per_attempt_and_resets_after_rollback():
    session = MagicMock(spec=Session)
    session.scalar.side_effect = [7, 7]
    repository = BacktestRepository(session)
    run_id, attempt_id = uuid4(), uuid4()
    values = {
        "timestamp": datetime(2025, 1, 1, tzinfo=UTC),
        "event_type": "SIGNAL_EVALUATED",
        "payload": {},
    }

    first = repository.append_journal(run_id, attempt_id, **values, flush=False)
    second = repository.append_journal(run_id, attempt_id, **values, flush=False)
    assert (first.sequence, second.sequence) == (8, 9)
    assert session.scalar.call_count == 1
    session.flush.assert_not_called()

    repository.reset_journal_sequence_cache()
    after_rollback = repository.append_journal(run_id, attempt_id, **values)
    assert after_rollback.sequence == 8
    assert session.scalar.call_count == 2
    session.flush.assert_called_once()
