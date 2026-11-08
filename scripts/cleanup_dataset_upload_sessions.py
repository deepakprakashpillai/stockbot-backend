"""Expire a bounded batch of abandoned dataset upload previews."""

from __future__ import annotations

import argparse

from app.application.datasets.service import DatasetService
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.infrastructure.datasets.storage import LocalDatasetStorage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    settings = get_settings()
    session = create_session_factory(settings)()
    try:
        count = DatasetService(
            session, LocalDatasetStorage(settings.storage_root), settings
        ).cleanup_expired_upload_sessions(args.limit)
    finally:
        session.close()
    print(f"Expired {count} dataset upload session(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
