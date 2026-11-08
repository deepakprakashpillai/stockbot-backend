from __future__ import annotations

import asyncio
import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.application.datasets import service as service_module
from app.application.datasets.service import DatasetService, DatasetServiceError
from app.core.config import Settings
from app.db.models.datasets import Dataset, DatasetUploadSession, DatasetVersion
from app.domain.datasets import DatasetStatus
from app.infrastructure.datasets.storage import DatasetTooLargeError, StagedUpload


class Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, (Dataset, DatasetVersion, DatasetUploadSession)) and value.id is None:
            value.id = uuid4()

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.datasets: dict[UUID, Dataset] = {}
        self.versions: dict[UUID, DatasetVersion] = {}
        self.uploads: dict[UUID, DatasetUploadSession] = {}
        self.duplicate: DatasetVersion | None = None

    def find_duplicate(self, _checksum: str, _profile: str) -> DatasetVersion | None:
        return self.duplicate

    def get_dataset(self, dataset_id: UUID) -> Dataset | None:
        return self.datasets.get(dataset_id)

    def get_version(self, version_id: UUID | None, *, lock: bool = False) -> DatasetVersion | None:
        if version_id is None:
            return None
        return self.versions.get(version_id)

    def next_version_number(self, _dataset_id: UUID) -> int:
        return 1

    def get_upload_session(
        self, session_id: UUID, owner_key: str, *, lock: bool = False
    ) -> DatasetUploadSession | None:
        value = self.uploads.get(session_id)
        return value if value and value.owner_key == owner_key else None

    def expired_upload_sessions(self, _now: datetime, limit: int) -> list[DatasetUploadSession]:
        return list(self.uploads.values())[:limit]


class Storage:
    def __init__(self, root: Path, *, content: bytes, size: int | None = None) -> None:
        self.root = root
        self.content = content
        self.size = size if size is not None else len(content)
        self.raise_large = False
        self.raise_finalize = False
        self.discarded: list[Path] = []

    async def stage_upload(self, _upload: object, _limit: int) -> StagedUpload:
        if self.raise_large:
            raise DatasetTooLargeError("too large")
        path = self.root / "staged.csv"
        path.write_bytes(self.content)
        return StagedUpload(path, "b" * 64, self.size, "nifty.csv")

    def finalize(self, staged: StagedUpload, dataset_id: UUID, version_id: UUID) -> str:
        if self.raise_finalize:
            raise OSError("storage unavailable")
        return str(staged.temporary_path)

    def staging_key(self, staged: StagedUpload) -> str:
        return str(staged.temporary_path)

    def resolve(self, storage_key: str) -> Path:
        return Path(storage_key)

    def discard(self, path: Path) -> None:
        self.discarded.append(path)
        path.unlink(missing_ok=True)


class Upload:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def candle_csv() -> bytes:
    return (
        b"timestamp,open,high,low,close,volume,open_interest\n"
        b"2025-07-21T09:15:00+05:30,100,102,99,101,0,0\n"
        b"2025-07-21T09:20:00+05:30,101,103,100,102,0,0\n"
    )


def ready_version(*, status: str = "VALID") -> DatasetVersion:
    return DatasetVersion(
        id=uuid4(),
        dataset_id=uuid4(),
        version_number=1,
        schema_profile="nifty_index_candles_v1",
        original_filename="nifty.csv",
        checksum="c" * 64,
        storage_key="fixture.csv",
        status=status,
        row_count=2,
        session_count=1,
        session_dates=["2025-07-21"],
        timezone="Asia/Kolkata",
        resolution_minutes=5,
        coverage_start=datetime.fromisoformat("2025-07-21T09:15:00+05:30"),
        coverage_end=datetime.fromisoformat("2025-07-21T09:20:00+05:30"),
        instruments=[],
        capabilities={"price_candles": True},
        profile={"rows": 2},
        warnings=[],
        errors=[],
        validation_progress=100,
        created_at=datetime.now(UTC),
    )


def service(monkeypatch, tmp_path: Path, *, size: int | None = None):
    session = Session()
    repository = Repository(session)
    storage = Storage(tmp_path, content=candle_csv(), size=size)
    monkeypatch.setattr(service_module, "DatasetRepository", lambda _session: repository)
    settings = Settings(
        environment="test",
        storage_root=tmp_path,
        dataset_sync_validation_max_bytes=1024,
        dataset_upload_max_bytes=4096,
    )
    return DatasetService(session, storage, settings), repository, session, storage


def test_create_dataset_handles_size_dataset_identity_and_storage_failures(
    monkeypatch, tmp_path: Path
) -> None:
    value, repository, session, storage = service(monkeypatch, tmp_path)
    storage.raise_large = True

    with pytest.raises(DatasetServiceError, match="configured size limit") as too_large:
        asyncio.run(
            value.create_dataset(
                name="NIFTY", description=None, source=None, upload=Upload("n.csv")
            )
        )
    assert too_large.value.code == "DATASET_TOO_LARGE"

    storage.raise_large = False
    missing_id = uuid4()
    with pytest.raises(DatasetServiceError, match="requested dataset does not exist"):
        asyncio.run(
            value.create_dataset(
                name="NIFTY",
                description=None,
                source=None,
                upload=Upload("n.csv"),
                dataset_id=missing_id,
            )
        )
    assert storage.discarded

    known = Dataset(id=uuid4(), name="Known")
    repository.datasets[known.id] = known
    storage.raise_finalize = True
    with pytest.raises(OSError, match="storage unavailable"):
        asyncio.run(
            value.create_dataset(
                name="Known",
                description=None,
                source=None,
                upload=Upload("n.csv"),
                dataset_id=known.id,
            )
        )
    assert session.rollbacks == 1


def test_large_dataset_queues_validation_after_durable_upload(monkeypatch, tmp_path: Path) -> None:
    value, repository, session, _storage = service(monkeypatch, tmp_path, size=2048)
    monkeypatch.setattr(
        "app.workers.tasks.validate_dataset_version",
        SimpleNamespace(delay=lambda _value: SimpleNamespace(id="validation-task")),
    )

    version, status_code, duplicate = asyncio.run(
        value.create_dataset(name="NIFTY", description=None, source=None, upload=Upload("n.csv"))
    )
    repository.versions[version.id] = version

    assert (status_code, duplicate) == (202, False)
    from app.db.models.jobs import JobDispatch

    intent = next(item for item in session.added if isinstance(item, JobDispatch))
    assert version.task_id == str(intent.id)
    assert intent.resource_id == version.id
    assert intent.kind == "DATASET_VALIDATION"
    assert session.commits == 1


def test_upload_preview_lifecycle_handles_schema_and_terminal_states(
    monkeypatch, tmp_path: Path
) -> None:
    value, repository, session, storage = service(monkeypatch, tmp_path)
    unsupported = Upload("notes.txt")
    with pytest.raises(DatasetServiceError, match="Only CSV"):
        asyncio.run(value.create_upload_session(upload=unsupported, owner_key="owner"))
    assert unsupported.closed is True

    record = asyncio.run(value.create_upload_session(upload=Upload("nifty.csv"), owner_key="owner"))
    record.state_version = 1
    repository.uploads[record.id] = record
    assert record.status == "STAGED"
    assert record.schema_profile == "nifty_index_candles_v1"

    record.preview = {"status": "blocked", "issues": [{"code": "PREVIEW_BAD"}]}
    with pytest.raises(DatasetServiceError, match="Fix the preview") as blocked:
        value.commit_upload_session(
            session_id=record.id,
            owner_key="owner",
            name="NIFTY",
            description=None,
            source=None,
            dataset_id=None,
        )
    assert blocked.value.code == "PREVIEW_BAD"

    record.status = "DISCARDED"
    with pytest.raises(DatasetServiceError, match="not found"):
        value.commit_upload_session(
            session_id=record.id,
            owner_key="owner",
            name="NIFTY",
            description=None,
            source=None,
            dataset_id=None,
        )

    record.status = "STAGED"
    value.discard_upload_session(record.id, "owner")
    assert record.status == "DISCARDED"
    assert storage.discarded

    record.status = "COMMITTED"
    with pytest.raises(DatasetServiceError, match="cannot be discarded"):
        value.discard_upload_session(record.id, "owner")
    assert session.commits >= 2


def test_upload_commit_is_idempotent_and_expiry_cleanup_is_bounded(
    monkeypatch, tmp_path: Path
) -> None:
    value, repository, _session, storage = service(monkeypatch, tmp_path)
    version = ready_version()
    repository.versions[version.id] = version
    record = DatasetUploadSession(
        id=uuid4(),
        owner_key="owner",
        status="COMMITTED",
        state_version=2,
        staging_key="staged.csv",
        checksum="c" * 64,
        size_bytes=2,
        original_filename="nifty.csv",
        preview={"status": "committable"},
        profile={},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        committed_version_id=version.id,
    )
    repository.uploads[record.id] = record

    result, status_code, duplicate = value.commit_upload_session(
        session_id=record.id,
        owner_key="owner",
        name="ignored",
        description=None,
        source=None,
        dataset_id=None,
    )
    assert (result, status_code, duplicate) == (version, 200, False)

    expired = DatasetUploadSession(
        id=uuid4(),
        owner_key="owner",
        status="STAGED",
        state_version=1,
        staging_key="expired.csv",
        checksum="d" * 64,
        size_bytes=2,
        original_filename="nifty.csv",
        preview={},
        profile={},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    repository.uploads = {expired.id: expired}
    assert value.cleanup_expired_upload_sessions(limit=1) == 1
    assert expired.status == "EXPIRED"
    assert storage.discarded


def test_validation_profile_and_candle_reads_keep_error_boundaries(
    monkeypatch, tmp_path: Path
) -> None:
    value, repository, session, storage = service(monkeypatch, tmp_path)
    version = ready_version(status="PENDING")
    storage.resolve(version.storage_key).write_bytes(candle_csv())
    repository.versions[version.id] = version

    monkeypatch.setattr(
        service_module,
        "validate_nifty_index_candles",
        lambda _path: (_ for _ in ()).throw(csv.Error("bad")),
    )
    assert value.validate_version(version.id) is version
    assert version.status == DatasetStatus.INVALID.value
    assert version.errors[0]["code"] == "MALFORMED_CSV"

    version.status = "PENDING"
    monkeypatch.setattr(
        service_module,
        "validate_nifty_index_candles",
        lambda _path: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert value.validate_version(version.id) is version
    assert version.status == DatasetStatus.FAILED.value

    version = ready_version()
    repository.versions[version.id] = version
    profile = value.profile(version.id)
    assert profile["profile"] == {"rows": 2}
    with pytest.raises(DatasetServiceError, match="limited to 1,000"):
        value.candles(
            version.id,
            start=None,
            end=None,
            granularity="raw",
            purpose="explore",
            cursor=None,
            limit=1001,
        )
    monkeypatch.setattr(
        service_module,
        "read_candles",
        lambda *_args, **_kwargs: ([{"timestamp": "2025-07-21T09:15:00+05:30"}], None, False),
    )
    payload = value.candles(
        version.id,
        start=None,
        end=None,
        granularity="raw",
        purpose="explore",
        cursor=None,
        limit=10,
    )
    assert payload["candles"]
    assert payload["quality_markers"] == []
    assert session.commits >= 2


def test_range_preflight_rejects_unknown_invalid_and_sessionless_ranges(
    monkeypatch, tmp_path: Path
) -> None:
    value, repository, _session, _storage = service(monkeypatch, tmp_path)
    with pytest.raises(DatasetServiceError, match="requested dataset version"):
        value.range_preflight(uuid4(), date(2025, 7, 21), date(2025, 7, 21))

    version = ready_version()
    version.coverage_end = datetime.fromisoformat("2025-07-22T15:20:00+05:30")
    repository.versions[version.id] = version
    invalid = value.range_preflight(version.id, date(2025, 7, 22), date(2025, 7, 21))
    empty = value.range_preflight(version.id, date(2025, 7, 22), date(2025, 7, 22))
    assert invalid["issues"][0]["code"] == "INVALID_RANGE"
    assert empty["selection_allowed"] is False
    assert empty["issues"][0]["code"] == "NO_TRADING_SESSIONS"
