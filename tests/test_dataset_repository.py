from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.db.models.datasets import Dataset, DatasetUploadSession, DatasetVersion
from app.db.repositories.datasets import DatasetRepository


class FakeSession:
    def __init__(self) -> None:
        self.scalar_values: list[object] = []
        self.scalars_values: list[Dataset] = []
        self.scalars_sequences: list[list[object]] = []
        self.statements: list[object] = []

    def scalar(self, statement: object):
        self.statements.append(statement)
        return self.scalar_values.pop(0)

    def scalars(self, statement: object):
        self.statements.append(statement)
        if self.scalars_sequences:
            return self.scalars_sequences.pop(0)
        return self.scalars_values


def test_repository_lookup_and_next_version() -> None:
    session = FakeSession()
    expected = DatasetVersion(id=uuid4())
    session.scalar_values = [expected, None, expected, 4]
    repository = DatasetRepository(session)  # type: ignore[arg-type]

    assert repository.find_duplicate("checksum", "profile") is expected
    assert repository.get_dataset(uuid4()) is None
    assert repository.get_version(uuid4(), lock=True) is expected
    assert repository.next_version_number(uuid4()) == 5
    assert len(session.statements) == 4


def test_repository_lists_with_cursor_and_status() -> None:
    session = FakeSession()
    first, second = Dataset(id=uuid4(), name="first"), Dataset(id=uuid4(), name="second")
    session.scalars_values = [first, second]

    items, cursor = DatasetRepository(session).list_datasets("WARNING", first.id, 1)  # type: ignore[arg-type]

    assert items == [first]
    assert cursor == second.id


def test_repository_search_count_and_upload_session_queries() -> None:
    session = FakeSession()
    dataset = Dataset(id=uuid4(), name="first", source="NSE")
    upload = DatasetUploadSession(id=uuid4(), owner_key="owner")
    session.scalars_sequences = [[dataset], [upload]]
    session.scalar_values = [4, upload]
    repository = DatasetRepository(session)  # type: ignore[arg-type]

    items, cursor = repository.list_datasets(None, None, 10, search="bank_50%")
    assert items == [dataset]
    assert cursor is None
    assert repository.count_datasets("READY", search="bank_50%") == 4
    assert repository.get_upload_session(upload.id, "owner", lock=True) is upload
    assert repository.expired_upload_sessions(datetime(2026, 8, 28, tzinfo=UTC), 10) == [upload]
