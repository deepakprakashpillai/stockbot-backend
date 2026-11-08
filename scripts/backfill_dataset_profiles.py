"""Build missing immutable dataset profiles in bounded, idempotent batches."""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models.datasets import DatasetVersion
from app.db.session import create_session_factory
from app.domain.dataset_explorer import profile_csv
from app.domain.datasets import DatasetStatus
from app.infrastructure.datasets.storage import LocalDatasetStorage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    settings = get_settings()
    storage = LocalDatasetStorage(settings.storage_root)
    session = create_session_factory(settings)()
    try:
        versions = list(
            session.scalars(
                select(DatasetVersion)
                .where(
                    DatasetVersion.status.in_(
                        [DatasetStatus.VALID.value, DatasetStatus.WARNING.value]
                    ),
                    DatasetVersion.profile == {},
                )
                .limit(args.limit)
            )
        )
        for version in versions:
            version.profile = profile_csv(storage.resolve(version.storage_key))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    print(f"Backfilled {len(versions)} dataset profile(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
