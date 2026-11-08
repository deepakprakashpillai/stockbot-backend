from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import UploadFile

from app.infrastructure.datasets.storage import DatasetTooLargeError, LocalDatasetStorage


def test_local_storage_stages_finalizes_and_rejects_path_escape(tmp_path: Path) -> None:
    storage = LocalDatasetStorage(tmp_path)
    upload = UploadFile(filename="../../nifty.csv", file=BytesIO(b"timestamp\n"))

    staged = asyncio.run(storage.stage_upload(upload, max_bytes=1024))
    key = storage.finalize(staged, uuid4(), uuid4())

    assert storage.resolve(key).read_bytes() == b"timestamp\n"
    with pytest.raises(ValueError):
        storage.resolve("../../outside.csv")


def test_local_storage_enforces_upload_limit(tmp_path: Path) -> None:
    storage = LocalDatasetStorage(tmp_path)
    upload = UploadFile(filename="large.csv", file=BytesIO(b"abcdef"))

    with pytest.raises(DatasetTooLargeError):
        asyncio.run(storage.stage_upload(upload, max_bytes=5))
