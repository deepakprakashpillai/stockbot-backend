#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db.models.datasets import Dataset, DatasetVersion  # noqa: E402
from app.db.session import create_session_factory  # noqa: E402
from app.domain.datasets import DatasetStatus  # noqa: E402
from app.infrastructure.datasets.derivatives_bundle import (  # noqa: E402
    build_derivatives_bundle,
    load_derivatives_bundle,
    verify_derivatives_bundle_files,
)


def register_bundle(
    bundle_dir: Path, spot_csv: Path, name: str, *, source_semantics: str = "interval_start"
) -> DatasetVersion:
    settings = get_settings()
    storage_root = settings.storage_root.resolve()
    bundle_dir = bundle_dir.resolve()
    if bundle_dir != storage_root and storage_root not in bundle_dir.parents:
        raise ValueError("Bundle directory must be inside STOCKBOT storage_root.")

    if not (bundle_dir / "bundle_manifest.json").exists():
        build_derivatives_bundle(bundle_dir, spot_csv, source_semantics=source_semantics)
    manifest = load_derivatives_bundle(bundle_dir)
    verify_derivatives_bundle_files(bundle_dir, manifest)
    components = manifest["components"]
    spot = components["spot"]
    bundle_storage_key = bundle_dir.relative_to(storage_root).as_posix()
    spot_storage_key = f"{bundle_storage_key}/{spot['path']}"

    factory = create_session_factory(settings)
    with factory.begin() as session:
        dataset = session.scalar(select(Dataset).where(Dataset.name == name))
        if dataset is None:
            dataset = Dataset(
                name=name,
                description="Immutable NIFTY spot, futures, India VIX and weekly option bundle.",
                source="Upstox historical candles plus NIFTY spot CSV",
            )
            session.add(dataset)
            session.flush()

        versions = list(
            session.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id))
        )
        for version in versions:
            if version.profile.get("bundle_checksum") == manifest["bundle_checksum"]:
                return version

        version_number = (
            session.scalar(
                select(func.max(DatasetVersion.version_number)).where(
                    DatasetVersion.dataset_id == dataset.id
                )
            )
            or 0
        ) + 1
        coverage_start = datetime.fromisoformat(spot["coverage_start"])
        coverage_end = datetime.fromisoformat(spot["coverage_end"])
        version = DatasetVersion(
            dataset_id=dataset.id,
            version_number=version_number,
            schema_profile=(
                "nifty_derivatives_bundle_v2"
                if manifest["schema_version"] == "stockbot.derivatives-bundle.v2"
                else "nifty_derivatives_bundle_v1"
            ),
            original_filename=Path(spot["path"]).name,
            # A derivatives bundle can legitimately share its spot CSV with an
            # earlier bundle while supplying a different contract universe or
            # option history.  The version identity must therefore cover every
            # immutable component, not only the spot file.
            checksum=manifest["bundle_checksum"],
            storage_key=spot_storage_key,
            status=DatasetStatus.VALID.value,
            row_count=spot["row_count"],
            session_count=len(spot["session_dates"]),
            session_dates=spot["session_dates"],
            timezone="Asia/Kolkata",
            resolution_minutes=5,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            instruments=[
                {"key": "NIFTY_SPOT", "kind": "INDEX"},
                {"key": "NIFTY_FUTURES", "kind": "FUTURE"},
                {"key": "INDIA_VIX", "kind": "INDEX"},
                {"key": "NIFTY_WEEKLY_OPTIONS", "kind": "OPTION"},
            ],
            capabilities=manifest["capabilities"],
            profile={
                "bundle_schema_version": manifest["schema_version"],
                "bundle_checksum": manifest["bundle_checksum"],
                "spot_checksum": spot["checksum"],
                "derivatives_storage_key": bundle_storage_key,
                "derivatives_manifest": "bundle_manifest.json",
                "pricing_quality": "estimated_historical_ohlc",
                "executable_quotes": False,
                "canonical_timestamp_semantics": manifest.get("canonical_timestamp_semantics"),
            },
            warnings=[
                {
                    "code": "ESTIMATED_OPTION_PRICING",
                    "message": limitation,
                    "details": {},
                }
                for limitation in manifest["limitations"]
            ],
            errors=[],
            validation_progress=100,
            completed_at=datetime.now().astimezone(),
        )
        session.add(version)
        session.flush()
        return version


def main() -> None:
    parser = argparse.ArgumentParser(description="Register one immutable derivatives bundle.")
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=BACKEND_ROOT / "data" / "datasets" / "upstox_nifty_derivatives",
    )
    parser.add_argument(
        "--spot-csv",
        type=Path,
        default=BACKEND_ROOT.parent / "nifty50_5min_2025-07-19_to_2026-07-19.csv",
    )
    parser.add_argument("--name", default="NIFTY Derivatives Historical Bundle")
    parser.add_argument(
        "--spot-timestamp-semantics",
        choices=("interval_start", "interval_end"),
        default="interval_start",
        help="Declare whether the spot source is provider start-time or already completed-candle data.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Build and verify the immutable manifest without writing PostgreSQL.",
    )
    args = parser.parse_args()
    if args.manifest_only:
        manifest = build_derivatives_bundle(
            args.bundle_dir, args.spot_csv, source_semantics=args.spot_timestamp_semantics
        )
        print(f"Built derivatives bundle {manifest['bundle_checksum']}.")
        return
    version = register_bundle(
        args.bundle_dir, args.spot_csv, args.name, source_semantics=args.spot_timestamp_semantics
    )
    print(f"Registered dataset version {version.id} (v{version.version_number}).")


if __name__ == "__main__":
    main()
