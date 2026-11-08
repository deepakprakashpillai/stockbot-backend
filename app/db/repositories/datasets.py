from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models.datasets import Dataset, DatasetUploadSession, DatasetVersion


class DatasetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_duplicate(self, checksum: str, schema_profile: str) -> DatasetVersion | None:
        return self.session.scalar(
            select(DatasetVersion).where(
                DatasetVersion.checksum == checksum, DatasetVersion.schema_profile == schema_profile
            )
        )

    def get_dataset(self, dataset_id: UUID) -> Dataset | None:
        return self.session.scalar(
            select(Dataset).options(selectinload(Dataset.versions)).where(Dataset.id == dataset_id)
        )

    def get_version(self, version_id: UUID, *, lock: bool = False) -> DatasetVersion | None:
        statement = (
            select(DatasetVersion)
            .options(selectinload(DatasetVersion.dataset))
            .where(DatasetVersion.id == version_id)
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_datasets(
        self, status: str | None, cursor: UUID | None, limit: int, search: str | None = None
    ) -> tuple[list[Dataset], UUID | None]:
        statement: Select[tuple[Dataset]] = (
            select(Dataset)
            .options(selectinload(Dataset.versions))
            .order_by(Dataset.id)
            .limit(limit + 1)
        )
        if cursor is not None:
            statement = statement.where(Dataset.id > cursor)
        if status is not None:
            statement = (
                statement.join(DatasetVersion).where(DatasetVersion.status == status).distinct()
            )
        if search:
            escaped = search.replace("%", r"\%").replace("_", r"\_")
            statement = statement.where(
                or_(Dataset.name.ilike(f"%{escaped}%"), Dataset.source.ilike(f"%{escaped}%"))
            )
        datasets = list(self.session.scalars(statement))
        next_cursor = datasets[limit].id if len(datasets) > limit else None
        return datasets[:limit], next_cursor

    def count_datasets(self, status: str | None, search: str | None = None) -> int:
        statement = select(func.count(func.distinct(Dataset.id)))
        if status is not None:
            statement = statement.join(DatasetVersion).where(DatasetVersion.status == status)
        if search:
            escaped = search.replace("%", r"\%").replace("_", r"\_")
            statement = statement.where(
                or_(Dataset.name.ilike(f"%{escaped}%"), Dataset.source.ilike(f"%{escaped}%"))
            )
        return int(self.session.scalar(statement) or 0)

    def next_version_number(self, dataset_id: UUID) -> int:
        highest = self.session.scalar(
            select(func.max(DatasetVersion.version_number)).where(
                DatasetVersion.dataset_id == dataset_id
            )
        )
        return (highest or 0) + 1

    def get_upload_session(
        self, session_id: UUID, owner_key: str, *, lock: bool = False
    ) -> DatasetUploadSession | None:
        statement = select(DatasetUploadSession).where(
            DatasetUploadSession.id == session_id, DatasetUploadSession.owner_key == owner_key
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def expired_upload_sessions(self, now: datetime, limit: int) -> list[DatasetUploadSession]:
        return list(
            self.session.scalars(
                select(DatasetUploadSession)
                .where(
                    DatasetUploadSession.status == "STAGED",
                    DatasetUploadSession.expires_at <= now,
                )
                .order_by(DatasetUploadSession.expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
