from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.application.datasets import service as service_module
from app.application.datasets.service import DatasetService
from app.core.config import Settings
from app.db.models.datasets import Dataset, DatasetVersion
from app.domain.datasets import DatasetStatus
from app.infrastructure.datasets.storage import StagedUpload


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        for value in self.added:
            if isinstance(value, Dataset) and value.id is None:
                value.id = uuid4()
            if isinstance(value, DatasetVersion) and value.id is None:
                value.id = uuid4()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeRepository:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.dataset: Dataset | None = None
        self.version: DatasetVersion | None = None
        self.duplicate: DatasetVersion | None = None

    def find_duplicate(self, _checksum: str, _profile: str) -> DatasetVersion | None:
        return self.duplicate

    def get_dataset(self, dataset_id: UUID) -> Dataset | None:
        if self.dataset and self.dataset.id == dataset_id:
            return self.dataset
        return next(
            (
                value
                for value in self.session.added
                if isinstance(value, Dataset) and value.id == dataset_id
            ),
            None,
        )

    def get_version(self, version_id: UUID, *, lock: bool = False) -> DatasetVersion | None:
        if self.version and self.version.id == version_id:
            return self.version
        return next(
            (
                value
                for value in self.session.added
                if isinstance(value, DatasetVersion) and value.id == version_id
            ),
            None,
        )

    def next_version_number(self, _dataset_id: UUID) -> int:
        return 1


class FakeStorage:
    def __init__(self, root: Path, content: bytes) -> None:
        self.root = root
        self.content = content
        self.discarded: list[Path] = []

    async def stage_upload(self, _upload: object, _max_bytes: int) -> StagedUpload:
        temp = self.root / "staged.csv"
        temp.write_bytes(self.content)
        return StagedUpload(temp, "a" * 64, len(self.content), "nifty.csv")

    def finalize(self, staged: StagedUpload, _dataset_id: UUID, _version_id: UUID) -> str:
        return str(staged.temporary_path)

    def discard(self, path: Path) -> None:
        self.discarded.append(path)
        path.unlink(missing_ok=True)

    def resolve(self, storage_key: str) -> Path:
        return Path(storage_key)


def settings(root: Path, sync_limit: int = 5 * 1024 * 1024) -> Settings:
    return Settings(
        environment="test", storage_root=root, dataset_sync_validation_max_bytes=sync_limit
    )


def csv_bytes() -> bytes:
    return (
        b"timestamp,open,high,low,close,volume,open_interest\n"
        b"2025-07-21T09:15:00+05:30,100,102,99,101,0,0\n"
        b"2025-07-21T09:20:00+05:30,101,103,100,102,0,0\n"
    )


def setup_service(monkeypatch, tmp_path: Path, sync_limit: int = 5 * 1024 * 1024):
    session = FakeSession()
    repository = FakeRepository(session)
    monkeypatch.setattr(service_module, "DatasetRepository", lambda _session: repository)
    storage = FakeStorage(tmp_path, csv_bytes())
    return (
        DatasetService(session, storage, settings(tmp_path, sync_limit)),
        repository,
        session,
        storage,
    )


def test_service_creates_and_synchronously_validates_dataset(monkeypatch, tmp_path: Path) -> None:
    service, repository, session, _storage = setup_service(monkeypatch, tmp_path)

    async def create() -> tuple[DatasetVersion, int, bool]:
        return await service.create_dataset(
            name="NIFTY candles", description=None, source="fixture", upload=object()
        )

    version, status_code, duplicate = asyncio.run(create())
    assert status_code == 201
    assert not duplicate
    assert version.status == DatasetStatus.WARNING.value
    assert version.row_count == 2
    assert version.validation_progress == 100


def test_service_returns_duplicate_without_storing_another_copy(
    monkeypatch, tmp_path: Path
) -> None:
    service, repository, _session, storage = setup_service(monkeypatch, tmp_path)
    existing = DatasetVersion(
        id=uuid4(),
        dataset_id=uuid4(),
        version_number=1,
        schema_profile="nifty_index_candles_v1",
        original_filename="nifty.csv",
        checksum="a" * 64,
        storage_key="existing",
        status="VALID",
        session_dates=[],
        instruments=[],
        capabilities={},
        warnings=[],
        errors=[],
        validation_progress=100,
        created_at=datetime.now(),
    )
    repository.duplicate = existing

    version, status_code, duplicate = asyncio.run(
        service.create_dataset(name="ignored", description=None, source=None, upload=object())
    )

    assert version is existing
    assert status_code == 200
    assert duplicate
    assert storage.discarded


def test_service_preflight_reports_available_and_out_of_range_dates(
    monkeypatch, tmp_path: Path
) -> None:
    service, repository, _session, _storage = setup_service(monkeypatch, tmp_path)
    version = DatasetVersion(
        id=uuid4(),
        dataset_id=uuid4(),
        version_number=1,
        schema_profile="nifty_index_candles_v1",
        original_filename="nifty.csv",
        checksum="b" * 64,
        storage_key="existing",
        status="WARNING",
        row_count=150,
        session_count=2,
        session_dates=["2025-07-21", "2025-07-22"],
        coverage_start=datetime.fromisoformat("2025-07-21T09:15:00+05:30"),
        coverage_end=datetime.fromisoformat("2025-07-22T15:25:00+05:30"),
        instruments=[],
        capabilities={"price_candles": True},
        warnings=[],
        errors=[],
        validation_progress=100,
        created_at=datetime.now(),
    )
    repository.version = version

    from datetime import date

    allowed = service.range_preflight(version.id, date(2025, 7, 21), date(2025, 7, 21))
    unavailable = service.range_preflight(version.id, date(2025, 7, 20), date(2025, 7, 21))

    assert allowed["selection_allowed"] is True
    assert allowed["acknowledgement_required"] is True
    assert unavailable["selection_allowed"] is False
    assert unavailable["issues"][0]["code"] == "DATASET_RANGE_UNAVAILABLE"
