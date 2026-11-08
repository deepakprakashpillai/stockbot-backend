from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.api.v1.library import get_library_summary
from app.db.models.datasets import DatasetVersion


def test_library_summary_aggregates_readiness_and_capabilities() -> None:
    versions = [
        DatasetVersion(
            id=uuid4(),
            dataset_id=uuid4(),
            version_number=1,
            schema_profile="fixture",
            original_filename="a.csv",
            checksum="a" * 64,
            storage_key="a.csv",
            status="WARNING",
            row_count=10,
            session_count=2,
            session_dates=[],
            coverage_start=datetime(2026, 1, 1, tzinfo=UTC),
            coverage_end=datetime(2026, 1, 2, tzinfo=UTC),
            instruments=[],
            capabilities={"price_candles": True, "india_vix": False},
            warnings=[],
            errors=[],
            validation_progress=100,
            created_at=datetime.now(UTC),
        )
    ]

    class Session:
        def __init__(self) -> None:
            self.values = iter([1, 2, 1, 0])

        def scalars(self, _statement):
            return versions

        def scalar(self, _statement):
            return next(self.values)

    summary = get_library_summary(Session())  # type: ignore[arg-type]

    assert summary.usable_dataset_count == 1
    assert summary.total_rows == 10
    assert [(item.capability, item.available_versions) for item in summary.capabilities] == [
        ("price_candles", 1)
    ]
