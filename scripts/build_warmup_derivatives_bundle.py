#!/usr/bin/env python3
"""Create a new immutable bundle by adding one prior spot session as warm-up data."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.market_data_time import completed_candle_timestamp  # noqa: E402
from app.infrastructure.datasets.derivatives_bundle import (  # noqa: E402
    BUNDLE_SCHEMA_VERSION,
    build_derivatives_bundle,
    load_derivatives_bundle,
    sha256_file,
    verify_derivatives_bundle_files,
)


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "timestamp" not in reader.fieldnames:
            raise ValueError(f"Spot file has no timestamp column: {path}")
        return list(reader.fieldnames), list(reader)


def build_warmup_bundle(
    source_bundle: Path,
    destination: Path,
    warmup_source: Path,
    warmup_session: date,
) -> dict[str, Any]:
    source_bundle = source_bundle.resolve()
    destination = destination.resolve()
    warmup_source = warmup_source.resolve()
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")

    source_manifest = load_derivatives_bundle(source_bundle)
    if source_manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError("Warm-up extension requires a completed-candle v2 source bundle.")
    verify_derivatives_bundle_files(source_bundle, source_manifest)

    source_spot = source_bundle / source_manifest["components"]["spot"]["path"]
    source_fields, source_rows = _read_rows(source_spot)
    warmup_fields, candidate_rows = _read_rows(warmup_source)
    if warmup_fields != source_fields:
        raise ValueError("Warm-up and bundle spot columns must match exactly.")

    warmup_rows: list[dict[str, str]] = []
    for row in candidate_rows:
        timestamp = datetime.fromisoformat(row["timestamp"])
        if timestamp.date() != warmup_session:
            continue
        converted = dict(row)
        converted["timestamp"] = completed_candle_timestamp(
            timestamp,
            source_semantics="interval_start",
            interval_minutes=5,
        ).isoformat()
        warmup_rows.append(converted)
    if not warmup_rows:
        raise ValueError(f"No warm-up candles found for {warmup_session.isoformat()}.")

    first_source_timestamp = min(datetime.fromisoformat(row["timestamp"]) for row in source_rows)
    if (
        max(datetime.fromisoformat(row["timestamp"]) for row in warmup_rows)
        >= first_source_timestamp
    ):
        raise ValueError("Warm-up candles must precede the source bundle's first spot candle.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-building-", dir=destination.parent)
    )
    try:
        shutil.copy2(source_bundle / "contracts_master.json", temporary / "contracts_master.json")
        shutil.copytree(source_bundle / "futures", temporary / "futures")
        shutil.copytree(source_bundle / "vix", temporary / "vix")
        shutil.copytree(source_bundle / "options", temporary / "options")

        target_spot = temporary / "spot" / "nifty_spot_5min.csv"
        target_spot.parent.mkdir(parents=True, exist_ok=True)
        combined = sorted(
            [*warmup_rows, *source_rows], key=lambda row: datetime.fromisoformat(row["timestamp"])
        )
        timestamps = [row["timestamp"] for row in combined]
        if len(timestamps) != len(set(timestamps)):
            raise ValueError("The combined spot series contains duplicate timestamps.")
        with target_spot.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=source_fields)
            writer.writeheader()
            writer.writerows(combined)

        manifest = build_derivatives_bundle(temporary, target_spot, source_semantics="interval_end")
        lineage = {
            "source_bundle": source_bundle.as_posix(),
            "source_bundle_checksum": source_manifest["bundle_checksum"],
            "source_spot_checksum": source_manifest["components"]["spot"]["checksum"],
            "warmup_source": warmup_source.as_posix(),
            "warmup_source_checksum": sha256_file(warmup_source),
            "warmup_session": warmup_session.isoformat(),
            "warmup_row_count": len(warmup_rows),
            "warmup_source_timestamp_semantics": "interval_start",
            "warmup_canonical_timestamp_semantics": "completed_candle",
            "warmup_timestamp_transformation": "interval_start_plus_5_minutes",
            "source_derivatives_files_preserved": True,
        }
        (temporary / "lineage.json").write_text(
            json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.rename(destination)
        return {
            "destination": destination.as_posix(),
            "bundle_checksum": manifest["bundle_checksum"],
            "spot_checksum": manifest["components"]["spot"]["checksum"],
            "warmup_session": warmup_session.isoformat(),
            "warmup_row_count": len(warmup_rows),
            "spot_row_count": manifest["components"]["spot"]["row_count"],
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extend an immutable derivatives bundle with one prior spot warm-up session."
    )
    parser.add_argument("source_bundle", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("warmup_source", type=Path)
    parser.add_argument("warmup_session", type=date.fromisoformat)
    args = parser.parse_args()
    print(
        json.dumps(
            build_warmup_bundle(
                args.source_bundle,
                args.destination,
                args.warmup_source,
                args.warmup_session,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
