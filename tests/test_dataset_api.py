from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from io import BytesIO
from uuid import UUID, uuid4

from fastapi import UploadFile
from starlette.requests import Request

from app.api.v1 import datasets as routes
from app.application.datasets.service import DatasetServiceError
from app.db.models.datasets import Dataset, DatasetVersion


def request() -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "dataset-api-test"
    return request


def version(status: str = "WARNING") -> DatasetVersion:
    return DatasetVersion(
        id=uuid4(),
        dataset_id=uuid4(),
        version_number=1,
        schema_profile="nifty_index_candles_v1",
        original_filename="nifty.csv",
        checksum="a" * 64,
        storage_key="datasets/example/original.csv",
        status=status,
        row_count=75,
        session_count=1,
        session_dates=["2025-07-21"],
        coverage_start=datetime(2025, 7, 21, 9, 15, tzinfo=UTC),
        coverage_end=datetime(2025, 7, 21, 15, 25, tzinfo=UTC),
        instruments=[{"code": "NIFTY50", "segment": "INDEX"}],
        capabilities={"price_candles": True},
        warnings=[{"code": "ZERO_VOLUME", "message": "No volume."}],
        errors=[],
        validation_progress=100,
        created_at=datetime.now(UTC),
    )


class FakeRepository:
    def __init__(self, _session: object) -> None:
        self.dataset: Dataset | None = None
        self.version: DatasetVersion | None = None
        self.items: list[Dataset] = []

    def list_datasets(
        self, _status: str | None, _cursor: UUID | None, _limit: int, _search: str | None = None
    ):
        return self.items, None

    def count_datasets(self, _status: str | None, _search: str | None = None) -> int:
        return len(self.items)

    def get_dataset(self, _dataset_id: UUID) -> Dataset | None:
        return self.dataset

    def get_version(self, _version_id: UUID) -> DatasetVersion | None:
        return self.version


def upload_session() -> object:
    return type(
        "UploadSession",
        (),
        {
            "id": uuid4(),
            "status": "STAGED",
            "checksum": "b" * 64,
            "size_bytes": 42,
            "original_filename": "nifty.csv",
            "schema_profile": "nifty_index_candles_v1",
            "expires_at": datetime.now(UTC),
            "preview": {"headers": ["timestamp"], "sample_rows": [], "issues": []},
            "profile": {"rows": 2},
            "committed_dataset_id": None,
            "committed_version_id": None,
        },
    )()


def test_dataset_route_serializers_and_reads(monkeypatch) -> None:
    fake = FakeRepository(object())
    item = version()
    dataset = Dataset(
        id=item.dataset_id,
        name="NIFTY",
        description="five-minute candles",
        source="fixture",
        created_at=datetime.now(UTC),
    )
    dataset.versions = [item]
    fake.dataset = dataset
    fake.version = item
    fake.items = [dataset]
    monkeypatch.setattr(routes, "DatasetRepository", lambda _session: fake)

    listed = routes.list_datasets(session=object())
    detail = routes.get_dataset(item.dataset_id, session=object())
    received = routes.get_dataset_version(item.id, session=object())
    coverage = routes.get_coverage(item.id, session=object())
    validation = routes.get_validation(item.id, session=object())

    assert listed.items[0].latest_version is not None
    assert listed.total_count == 1
    assert listed.items[0].latest_version.validation_progress == 100
    assert detail.versions[0].id == item.id
    assert received.capabilities["price_candles"] is True
    assert coverage.session_dates == [date(2025, 7, 21)]
    assert validation.warnings[0].code == "ZERO_VOLUME"


def test_dataset_route_not_found_and_upload_error(monkeypatch) -> None:
    fake = FakeRepository(object())
    monkeypatch.setattr(routes, "DatasetRepository", lambda _session: fake)

    from fastapi import HTTPException

    try:
        routes.get_dataset(uuid4(), session=object())
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("missing dataset must be a 404")

    class FailedService:
        async def create_dataset(self, **_kwargs: object):
            raise DatasetServiceError("DATASET_TOO_LARGE", "too large", 413)

    monkeypatch.setattr(routes, "_service", lambda _session, _request: FailedService())
    response = asyncio.run(
        routes._create(
            request(),
            object(),
            "NIFTY",
            None,
            None,
            UploadFile(BytesIO(), filename="nifty.csv"),
            None,
        )
    )
    assert response.status_code == 413
    assert b"dataset-api-test" in response.body


def test_range_preflight_route_uses_service_and_returns_stable_error(monkeypatch) -> None:
    value = version()

    class WorkingService:
        def range_preflight(self, _id: UUID, _start: date, _end: date) -> dict[str, object]:
            return {
                "dataset_version_id": value.id,
                "status": value.status,
                "selection_allowed": True,
                "acknowledgement_required": True,
                "requested_start_date": date(2025, 7, 21),
                "requested_end_date": date(2025, 7, 21),
                "available_start_date": date(2025, 7, 21),
                "available_end_date": date(2025, 7, 21),
                "usable_session_count": 1,
                "estimated_row_count": 75,
                "warnings": value.warnings,
                "issues": [],
                "capabilities": value.capabilities,
            }

    monkeypatch.setattr(routes, "_service", lambda _session, _request: WorkingService())
    result = routes.range_preflight(
        value.id,
        routes.RangePreflightRequest(start_date=date(2025, 7, 21), end_date=date(2025, 7, 21)),
        request(),
        object(),
    )
    assert result.selection_allowed is True


def test_upload_session_and_explorer_routes_forward_immutable_service_data(monkeypatch) -> None:
    item = version()
    record = upload_session()
    calls: list[str] = []

    class Service:
        async def create_dataset(self, **_kwargs):
            return item, 201, False

        async def create_upload_session(self, **_kwargs):
            calls.append("create")
            return record

        def get_upload_session(self, _id, _owner):
            calls.append("get")
            return record

        def commit_upload_session(self, **_kwargs):
            calls.append("commit")
            return item, 201, False

        def discard_upload_session(self, _id, _owner):
            calls.append("discard")

        def profile(self, _id):
            return {
                "dataset_version_id": item.id,
                "schema_profile": item.schema_profile,
                "profile": {"rows": 2},
                "capabilities": item.capabilities,
                "warnings": item.warnings,
                "coverage_start": item.coverage_start,
                "coverage_end": item.coverage_end,
            }

        def candles(self, _id, **_kwargs):
            return {
                "dataset_version_id": item.id,
                "purpose": "chart",
                "granularity": "5m",
                "range_start": item.coverage_start,
                "range_end": item.coverage_end,
                "candles": [
                    {
                        "timestamp": "2025-07-21T09:15:00+00:00",
                        "open": "100",
                        "high": "102",
                        "low": "99",
                        "close": "101",
                        "volume": "0",
                        "open_interest": "0",
                    }
                ],
                "next_cursor": None,
                "truncated": False,
                "quality_markers": [],
                "capabilities": item.capabilities,
            }

    monkeypatch.setattr(routes, "_service", lambda _session, _request: Service())
    response = asyncio.run(
        routes.create_dataset(
            request(), "NIFTY", UploadFile(BytesIO(), filename="nifty.csv"), object()
        )
    )
    version_response = asyncio.run(
        routes.create_dataset_version(
            item.dataset_id,
            request(),
            "NIFTY",
            UploadFile(BytesIO(), filename="nifty.csv"),
            object(),
        )
    )
    created = asyncio.run(
        routes.create_upload_session(
            request(), UploadFile(BytesIO(), filename="nifty.csv"), object()
        )
    )
    fetched = routes.get_upload_session(record.id, request(), object())
    committed = routes.commit_upload_session(
        record.id,
        routes.DatasetUploadSessionCommitRequest(name="NIFTY"),
        request(),
        object(),
    )
    discarded = routes.discard_upload_session(record.id, request(), object())
    profile = routes.get_profile(item.id, request(), object())
    candles = routes.get_candles(item.id, request(), object())

    assert response.status_code == version_response.status_code == 201
    assert created.id == fetched.id == record.id
    assert committed.dataset_version.id == item.id
    assert discarded.status_code == 204
    assert profile.profile == {"rows": 2}
    assert candles.candles[0].close == "101"
    assert calls == ["create", "get", "commit", "discard"]


def test_upload_session_and_explorer_routes_preserve_service_errors(monkeypatch) -> None:
    class FailedService:
        async def create_upload_session(self, **_kwargs):
            raise DatasetServiceError("PREVIEW_BLOCKED", "blocked", 422)

        def profile(self, _id):
            raise DatasetServiceError("DATASET_VERSION_NOT_READY", "not ready", 409)

        def candles(self, _id, **_kwargs):
            raise DatasetServiceError("DATASET_CANDLE_RANGE_INVALID", "bad range", 422)

    monkeypatch.setattr(routes, "_service", lambda _session, _request: FailedService())
    created = asyncio.run(
        routes.create_upload_session(
            request(), UploadFile(BytesIO(), filename="nifty.csv"), object()
        )
    )
    profile = routes.get_profile(uuid4(), request(), object())
    candles = routes.get_candles(uuid4(), request(), object())

    assert created.status_code == 422
    assert profile.status_code == 409
    assert candles.status_code == 422
