from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import UploadFile


class DatasetTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class StagedUpload:
    temporary_path: Path
    checksum: str
    size_bytes: int
    original_filename: str


class DatasetStorage(Protocol):
    async def stage_upload(self, upload: UploadFile, max_bytes: int) -> StagedUpload: ...
    def finalize(self, staged: StagedUpload, dataset_id: UUID, version_id: UUID) -> str: ...
    def discard(self, path: Path) -> None: ...
    def resolve(self, storage_key: str) -> Path: ...
    def staging_key(self, staged: StagedUpload) -> str: ...


class LocalDatasetStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def stage_upload(self, upload: UploadFile, max_bytes: int) -> StagedUpload:
        temporary_dir = self.root / ".staging"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = temporary_dir / str(uuid4())
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with temporary_path.open("wb") as destination:
                while chunk := await upload.read(1024 * 1024):
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        raise DatasetTooLargeError
                    digest.update(chunk)
                    destination.write(chunk)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        return StagedUpload(
            temporary_path, digest.hexdigest(), size_bytes, upload.filename or "upload.csv"
        )

    def finalize(self, staged: StagedUpload, dataset_id: UUID, version_id: UUID) -> str:
        storage_key = f"datasets/{dataset_id}/{version_id}/original.csv"
        destination = self.resolve(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged.temporary_path.replace(destination)
        return storage_key

    def discard(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def staging_key(self, staged: StagedUpload) -> str:
        return str(staged.temporary_path.resolve().relative_to(self.root)).replace("\\", "/")

    def resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("Storage key escapes the configured dataset root.")
        return candidate
