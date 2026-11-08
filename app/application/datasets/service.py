from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.application.jobs import enqueue_job
from app.core.config import Settings
from app.db.models.datasets import Dataset, DatasetUploadSession, DatasetVersion
from app.db.repositories.datasets import DatasetRepository
from app.domain.dataset_explorer import (
    Granularity,
    preview_csv,
    profile_csv,
    quality_markers,
    read_candles,
)
from app.domain.dataset_validation import NIFTY_INDEX_CANDLES_V1, validate_nifty_index_candles
from app.domain.datasets import TERMINAL_DATASET_STATUSES, DatasetStatus
from app.infrastructure.datasets.storage import DatasetStorage, DatasetTooLargeError, StagedUpload


class DatasetServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class DatasetService:
    def __init__(self, session: Session, storage: DatasetStorage, settings: Settings) -> None:
        self.session = session
        self.repository = DatasetRepository(session)
        self.storage = storage
        self.settings = settings

    async def create_dataset(
        self,
        *,
        name: str,
        description: str | None,
        source: str | None,
        upload: UploadFile,
        dataset_id: UUID | None = None,
    ) -> tuple[DatasetVersion, int, bool]:
        try:
            staged = await self.storage.stage_upload(upload, self.settings.dataset_upload_max_bytes)
        except DatasetTooLargeError as exc:
            raise DatasetServiceError(
                "DATASET_TOO_LARGE", "The uploaded file exceeds the configured size limit.", 413
            ) from exc
        return self._create_from_staged(
            staged=staged,
            name=name,
            description=description,
            source=source,
            dataset_id=dataset_id,
        )

    def _create_from_staged(
        self,
        *,
        staged: StagedUpload,
        name: str,
        description: str | None,
        source: str | None,
        dataset_id: UUID | None,
    ) -> tuple[DatasetVersion, int, bool]:
        checksum = staged.checksum
        temporary_path = staged.temporary_path
        duplicate = self.repository.find_duplicate(checksum, NIFTY_INDEX_CANDLES_V1)
        if duplicate is not None:
            self.storage.discard(temporary_path)
            return duplicate, 200, True

        dataset = self.repository.get_dataset(dataset_id) if dataset_id else None
        if dataset_id is not None and dataset is None:
            self.storage.discard(temporary_path)
            raise DatasetServiceError(
                "DATASET_NOT_FOUND", "The requested dataset does not exist.", 404
            )
        if dataset is None:
            dataset = Dataset(name=name.strip(), description=description, source=source)
            self.session.add(dataset)
            self.session.flush()

        version = DatasetVersion(
            dataset_id=dataset.id,
            version_number=self.repository.next_version_number(dataset.id),
            schema_profile=NIFTY_INDEX_CANDLES_V1,
            original_filename=staged.original_filename,
            checksum=checksum,
            storage_key="pending",
            status=DatasetStatus.PENDING.value,
            instruments=[{"code": "NIFTY50", "segment": "INDEX"}],
            timezone="Asia/Kolkata",
            resolution_minutes=5,
        )
        self.session.add(version)
        self.session.flush()
        try:
            version.storage_key = self.storage.finalize(staged, dataset.id, version.id)
            if staged.size_bytes > self.settings.dataset_sync_validation_max_bytes:
                intent = enqueue_job(self.session, "DATASET_VALIDATION", version.id)
                version.task_id = str(intent.id)
            self.session.commit()
        except Exception:
            self.session.rollback()
            self.storage.discard(
                self.storage.resolve(f"datasets/{dataset.id}/{version.id}/original.csv")
            )
            raise

        if staged.size_bytes <= self.settings.dataset_sync_validation_max_bytes:
            self.validate_version(version.id)
            refreshed = self.repository.get_version(version.id)
            assert refreshed is not None
            return refreshed, 201, False
        return version, 202, False

    async def create_upload_session(
        self, *, upload: UploadFile, owner_key: str
    ) -> DatasetUploadSession:
        filename = upload.filename or "upload.csv"
        if not filename.lower().endswith(".csv"):
            await upload.close()
            raise DatasetServiceError(
                "PREVIEW_UNSUPPORTED_SCHEMA", "Only CSV uploads can be analyzed.", 422
            )
        try:
            staged = await self.storage.stage_upload(upload, self.settings.dataset_upload_max_bytes)
        except DatasetTooLargeError as exc:
            raise DatasetServiceError(
                "DATASET_TOO_LARGE", "The uploaded file exceeds the configured size limit.", 413
            ) from exc
        preview = preview_csv(staged.temporary_path)
        record = DatasetUploadSession(
            owner_key=owner_key,
            status="STAGED",
            staging_key=self.storage.staging_key(staged),
            checksum=staged.checksum,
            size_bytes=staged.size_bytes,
            original_filename=staged.original_filename,
            schema_profile=NIFTY_INDEX_CANDLES_V1 if preview["status"] == "committable" else None,
            preview=preview,
            profile=profile_csv(staged.temporary_path),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.session.add(record)
        self.session.commit()
        return record

    def get_upload_session(self, session_id: UUID, owner_key: str) -> DatasetUploadSession:
        record = self.repository.get_upload_session(session_id, owner_key)
        if record is None:
            raise DatasetServiceError("UPLOAD_SESSION_NOT_FOUND", "Upload session not found.", 404)
        self._raise_if_expired(record)
        return record

    def commit_upload_session(
        self,
        *,
        session_id: UUID,
        owner_key: str,
        name: str,
        description: str | None,
        source: str | None,
        dataset_id: UUID | None,
    ) -> tuple[DatasetVersion, int, bool]:
        record = self.repository.get_upload_session(session_id, owner_key, lock=True)
        if record is None:
            raise DatasetServiceError("UPLOAD_SESSION_NOT_FOUND", "Upload session not found.", 404)
        self._raise_if_expired(record)
        if record.status == "DISCARDED":
            raise DatasetServiceError("UPLOAD_SESSION_NOT_FOUND", "Upload session not found.", 404)
        if record.status == "COMMITTED":
            version = (
                self.repository.get_version(record.committed_version_id)
                if record.committed_version_id
                else None
            )
            if version is None:
                raise DatasetServiceError(
                    "UPLOAD_SESSION_COMMITTED", "The session was already committed.", 409
                )
            return (
                version,
                200,
                bool(self.repository.find_duplicate(record.checksum, NIFTY_INDEX_CANDLES_V1)),
            )
        if record.preview.get("status") != "committable":
            code = str(record.preview.get("issues", [{}])[0].get("code", "PREVIEW_MALFORMED_CSV"))
            raise DatasetServiceError(code, "Fix the preview blockers before committing.", 422)
        staged = StagedUpload(
            temporary_path=self.storage.resolve(record.staging_key),
            checksum=record.checksum,
            size_bytes=record.size_bytes,
            original_filename=record.original_filename,
        )
        version, status_code, duplicate = self._create_from_staged(
            staged=staged,
            name=name,
            description=description,
            source=source,
            dataset_id=dataset_id,
        )
        record = self.repository.get_upload_session(session_id, owner_key, lock=True)
        assert record is not None
        record.status = "COMMITTED"
        record.state_version += 1
        record.committed_dataset_id = version.dataset_id
        record.committed_version_id = version.id
        self.session.commit()
        return version, status_code, duplicate

    def discard_upload_session(self, session_id: UUID, owner_key: str) -> None:
        record = self.repository.get_upload_session(session_id, owner_key, lock=True)
        if record is None:
            raise DatasetServiceError("UPLOAD_SESSION_NOT_FOUND", "Upload session not found.", 404)
        self._raise_if_expired(record)
        if record.status == "COMMITTED":
            raise DatasetServiceError(
                "UPLOAD_SESSION_COMMITTED", "Committed uploads cannot be discarded.", 409
            )
        record.status = "DISCARDED"
        record.state_version += 1
        self.session.commit()
        self.storage.discard(self.storage.resolve(record.staging_key))

    def cleanup_expired_upload_sessions(self, limit: int = 100) -> int:
        records = self.repository.expired_upload_sessions(datetime.now(UTC), limit)
        for record in records:
            record.status = "EXPIRED"
            record.state_version += 1
        self.session.commit()
        for record in records:
            self.storage.discard(self.storage.resolve(record.staging_key))
        return len(records)

    def _raise_if_expired(self, record: DatasetUploadSession) -> None:
        if record.status == "EXPIRED" or record.expires_at <= datetime.now(UTC):
            if record.status == "STAGED":
                record.status = "EXPIRED"
                record.state_version += 1
                self.session.commit()
                self.storage.discard(self.storage.resolve(record.staging_key))
            raise DatasetServiceError(
                "UPLOAD_SESSION_EXPIRED",
                "This upload preview expired. Analyze the file again.",
                410,
            )

    def validate_version(self, version_id: UUID) -> DatasetVersion | None:
        version = self.repository.get_version(version_id, lock=True)
        if version is None or DatasetStatus(version.status) in TERMINAL_DATASET_STATUSES:
            return version
        version.status = DatasetStatus.VALIDATING.value
        version.validation_progress = 10
        self.session.commit()
        try:
            outcome = validate_nifty_index_candles(self.storage.resolve(version.storage_key))
            version = self.repository.get_version(version_id, lock=True)
            if version is None or DatasetStatus(version.status) in TERMINAL_DATASET_STATUSES:
                return version
            version.status = outcome.status.value
            version.row_count = outcome.row_count
            version.session_count = outcome.session_count
            version.session_dates = outcome.session_dates
            version.coverage_start = outcome.coverage_start
            version.coverage_end = outcome.coverage_end
            version.capabilities = outcome.capabilities
            version.profile = profile_csv(self.storage.resolve(version.storage_key))
            version.warnings = [issue.as_dict() for issue in outcome.warnings]
            version.errors = [issue.as_dict() for issue in outcome.errors]
            version.validation_progress = 100
            version.completed_at = datetime.now(UTC)
            self.session.commit()
            return version
        except (csv.Error, UnicodeDecodeError) as exc:
            self.session.rollback()
            version = self.repository.get_version(version_id, lock=True)
            if version is not None:
                version.status = DatasetStatus.INVALID.value
                version.errors = [
                    {
                        "code": "MALFORMED_CSV",
                        "message": "The uploaded CSV is structurally malformed.",
                        "details": {"reason": str(exc)},
                    }
                ]
                version.validation_progress = 100
                version.completed_at = datetime.now(UTC)
                self.session.commit()
            return version
        except Exception as exc:
            self.session.rollback()
            version = self.repository.get_version(version_id, lock=True)
            if version is not None:
                version.status = DatasetStatus.FAILED.value
                version.errors = [
                    {
                        "code": "VALIDATION_FAILED",
                        "message": "Dataset validation could not complete.",
                        "details": {"reason": str(exc)},
                    }
                ]
                version.validation_progress = 100
                version.completed_at = datetime.now(UTC)
                self.session.commit()
            return version

    def range_preflight(
        self, version_id: UUID, start_date: date, end_date: date
    ) -> dict[str, object]:
        version = self.repository.get_version(version_id)
        if version is None:
            raise DatasetServiceError(
                "DATASET_VERSION_NOT_FOUND", "The requested dataset version does not exist.", 404
            )
        available_dates = [date.fromisoformat(value) for value in version.session_dates]
        usable = [value for value in available_dates if start_date <= value <= end_date]
        eligible = version.status in {
            DatasetStatus.VALID.value,
            DatasetStatus.WARNING.value,
        } and bool(usable)
        issues: list[dict[str, object]] = []
        if not version.coverage_start or not version.coverage_end or start_date > end_date:
            eligible = False
            issues.append(
                {
                    "code": "INVALID_RANGE",
                    "message": "The start date must not be after the end date.",
                }
            )
        elif start_date < version.coverage_start.date() or end_date > version.coverage_end.date():
            eligible = False
            issues.append(
                {
                    "code": "DATASET_RANGE_UNAVAILABLE",
                    "message": "The selected range extends beyond dataset coverage.",
                }
            )
        elif not usable:
            issues.append(
                {
                    "code": "NO_TRADING_SESSIONS",
                    "message": "The selected range contains no dataset sessions.",
                }
            )
        return {
            "dataset_version_id": version.id,
            "status": version.status,
            "selection_allowed": eligible,
            "acknowledgement_required": version.status == DatasetStatus.WARNING.value,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "available_start_date": version.coverage_start.date()
            if version.coverage_start
            else None,
            "available_end_date": version.coverage_end.date() if version.coverage_end else None,
            "usable_session_count": len(usable),
            "session_dates": [value.isoformat() for value in usable],
            "estimated_row_count": round(
                (version.row_count or 0) * len(usable) / max(version.session_count or 1, 1)
            ),
            "warnings": version.warnings,
            "issues": issues,
            "capabilities": version.capabilities,
        }

    def profile(self, version_id: UUID) -> dict[str, object]:
        version = self.repository.get_version(version_id)
        if version is None:
            raise DatasetServiceError(
                "DATASET_VERSION_NOT_FOUND", "Dataset version not found.", 404
            )
        if version.status not in {DatasetStatus.VALID.value, DatasetStatus.WARNING.value}:
            raise DatasetServiceError(
                "DATASET_VERSION_NOT_READY", "Dataset version is not ready.", 409
            )
        return {
            "dataset_version_id": version.id,
            "schema_profile": version.schema_profile,
            "profile": version.profile,
            "capabilities": version.capabilities,
            "warnings": version.warnings,
            "coverage_start": version.coverage_start,
            "coverage_end": version.coverage_end,
        }

    def candles(
        self,
        version_id: UUID,
        *,
        start: datetime | None,
        end: datetime | None,
        granularity: Granularity,
        purpose: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        version = self.repository.get_version(version_id)
        if version is None:
            raise DatasetServiceError(
                "DATASET_VERSION_NOT_FOUND", "Dataset version not found.", 404
            )
        if version.status not in {DatasetStatus.VALID.value, DatasetStatus.WARNING.value}:
            raise DatasetServiceError(
                "DATASET_VERSION_NOT_READY", "Dataset version is not ready.", 409
            )
        actual_start = start or version.coverage_start
        actual_end = end or version.coverage_end
        if actual_start is None or actual_end is None or actual_start > actual_end:
            raise DatasetServiceError(
                "DATASET_CANDLE_RANGE_INVALID", "The candle range is invalid.", 422
            )
        if limit > 1000:
            raise DatasetServiceError(
                "DATASET_CANDLE_LIMIT_EXCEEDED", "Candle requests are limited to 1,000 rows.", 422
            )
        try:
            values, next_cursor, truncated = read_candles(
                self.storage.resolve(version.storage_key),
                start=actual_start,
                end=actual_end,
                granularity=granularity,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise DatasetServiceError(
                "DATASET_CANDLE_RANGE_INVALID", "The candle cursor is invalid.", 422
            ) from exc
        return {
            "dataset_version_id": version.id,
            "purpose": purpose,
            "granularity": granularity,
            "range_start": actual_start,
            "range_end": actual_end,
            "candles": values,
            "next_cursor": next_cursor,
            "truncated": truncated,
            "quality_markers": quality_markers(version.warnings, actual_start, actual_end),
            "capabilities": version.capabilities,
        }


def dataset_file_path(storage: DatasetStorage, version: DatasetVersion) -> Path:
    return storage.resolve(version.storage_key)
