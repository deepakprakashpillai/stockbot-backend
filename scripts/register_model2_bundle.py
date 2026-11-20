"""Register a new immutable native Model 2 bundle; never modifies its originals."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.application.model2.datasets import register_bundle
from app.core.config import get_settings
from app.db.session import create_session_factory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--name", default="NIFTY Model 2 Historical Bundle")
    args = parser.parse_args()
    settings = get_settings()
    with create_session_factory(settings).begin() as session:
        version = register_bundle(session, args.bundle_dir, settings.storage_root, args.name)
        print(
            f"Registered Model 2 dataset version {version.id}; input checksum {version.checksum}."
        )


if __name__ == "__main__":
    main()
