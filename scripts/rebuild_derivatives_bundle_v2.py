from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.market_data_time import completed_candle_timestamp  # noqa: E402
from app.infrastructure.datasets.derivatives_bundle import (  # noqa: E402
    LEGACY_BUNDLE_SCHEMA_VERSION,
    build_derivatives_bundle,
    load_derivatives_bundle,
)


def _completed(value: object) -> str:
    return completed_candle_timestamp(
        datetime.fromisoformat(str(value)),
        source_semantics="interval_start",
        interval_minutes=5,
    ).isoformat()


def _rewrite_parquet(source: Path, destination: Path) -> None:
    table = pq.read_table(source)
    timestamp_index = table.schema.get_field_index("timestamp")
    if timestamp_index < 0:
        raise ValueError(f"Missing timestamp column: {source}")
    canonical = pa.array(
        [_completed(value) for value in table.column(timestamp_index).to_pylist()],
        type=pa.string(),
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table.set_column(timestamp_index, "timestamp", canonical),
        destination,
        compression="zstd",
    )


def _rewrite_spot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if not reader.fieldnames or "timestamp" not in reader.fieldnames:
            raise ValueError("The spot file is missing its timestamp column.")
        with destination.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                row["timestamp"] = _completed(row["timestamp"])
                writer.writerow(row)


def rebuild(source: Path, destination: Path) -> dict[str, object]:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    source_manifest = load_derivatives_bundle(source)
    if source_manifest.get("schema_version") != LEGACY_BUNDLE_SCHEMA_VERSION:
        raise ValueError("This migration accepts only the timing-affected v1 bundle.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-building-", dir=destination.parent)
    )
    try:
        shutil.copy2(source / "contracts_master.json", temporary / "contracts_master.json")
        _rewrite_spot(
            source / "spot" / "nifty_spot_5min.csv",
            temporary / "spot" / "nifty_spot_5min.csv",
        )
        _rewrite_parquet(
            source / "futures" / "nifty_futures_5min.parquet",
            temporary / "futures" / "nifty_futures_5min.parquet",
        )
        _rewrite_parquet(
            source / "vix" / "india_vix_5min.parquet",
            temporary / "vix" / "india_vix_5min.parquet",
        )
        for option_source in sorted((source / "options").glob("*/*.parquet")):
            _rewrite_parquet(option_source, temporary / option_source.relative_to(source))

        manifest = build_derivatives_bundle(
            temporary, temporary / "spot" / "nifty_spot_5min.csv", source_semantics="interval_end"
        )
        lineage = {
            "source_bundle": source.as_posix(),
            "source_bundle_checksum": source_manifest["bundle_checksum"],
            "transformation": "upstox_interval_start_plus_5_minutes_to_completed_candle",
            "source_files_preserved": True,
        }
        (temporary / "lineage.json").write_text(
            json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.rename(destination)
        return {
            "destination": destination.as_posix(),
            "bundle_checksum": manifest["bundle_checksum"],
            "option_file_count": manifest["components"]["option_ohlc"]["file_count"],
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a new immutable completed-candle bundle from a legacy Upstox bundle."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(rebuild(args.source, args.destination), indent=2))


if __name__ == "__main__":
    main()
