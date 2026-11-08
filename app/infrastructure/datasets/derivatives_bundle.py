from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from app.domain.market_data_time import CANONICAL_TIMESTAMP_SEMANTICS, completed_candle_timestamp

BUNDLE_SCHEMA_VERSION = "stockbot.derivatives-bundle.v2"
LEGACY_BUNDLE_SCHEMA_VERSION = "stockbot.derivatives-bundle.v1"
MANIFEST_FILENAME = "bundle_manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_candle(
    row: dict[str, Any], previous: datetime | None, *, allow_zero: bool = False
) -> datetime:
    timestamp = datetime.fromisoformat(str(row["timestamp"]))
    from datetime import timedelta

    if timestamp.utcoffset() != timedelta(hours=5, minutes=30):
        raise ValueError("Bundle candles must have an explicit +05:30 timezone.")
    if timestamp.second or timestamp.microsecond or timestamp.minute % 5:
        raise ValueError("Bundle candles must align to completed five-minute boundaries.")
    if previous is not None and timestamp <= previous:
        raise ValueError("Bundle candle timestamps must be unique and strictly ordered.")
    try:
        values = {
            key: Decimal(str(row[key]))
            for key in ("open", "high", "low", "close", "volume", "open_interest")
            if key in row
        }
        if not {"open", "high", "low", "close"}.issubset(values):
            raise ValueError("Bundle candles require OHLC values.")
        if any(not value.is_finite() for value in values.values()):
            raise ValueError("Bundle candle values must be finite.")
        if any(
            values[key] < 0 or (not allow_zero and values[key] == 0)
            for key in ("open", "high", "low", "close")
        ):
            raise ValueError("Bundle prices are outside the supported non-negative range.")
        if (
            not values["low"]
            <= min(values["open"], values["close"])
            <= max(values["open"], values["close"])
            <= values["high"]
        ):
            raise ValueError("Bundle candle OHLC relationships are invalid.")
        if any(values[key] < 0 for key in ("volume", "open_interest") if key in values):
            raise ValueError("Bundle volume and open interest must be non-negative.")
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("Bundle candles contain invalid numeric values.") from exc
    return timestamp


def _csv_profile(path: Path) -> dict[str, Any]:
    timestamps: list[datetime] = []
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            timestamps.append(_validate_candle(row, timestamps[-1] if timestamps else None))
    if not timestamps:
        raise ValueError("The spot candle file is empty.")
    timestamps.sort()
    return {
        "path": path.as_posix(),
        "checksum": sha256_file(path),
        "row_count": len(timestamps),
        "coverage_start": timestamps[0].isoformat(),
        "coverage_end": timestamps[-1].isoformat(),
        "session_dates": sorted({value.date().isoformat() for value in timestamps}),
    }


def _parquet_profile(path: Path, *, allow_zero: bool = False) -> dict[str, Any]:
    rows = pq.read_table(path).to_pylist()
    timestamps: list[datetime] = []
    for row in rows:
        timestamps.append(
            _validate_candle(row, timestamps[-1] if timestamps else None, allow_zero=allow_zero)
        )
    if not timestamps:
        raise ValueError(f"The candle file is empty: {path}")
    return {
        "path": path.as_posix(),
        "checksum": sha256_file(path),
        "row_count": len(timestamps),
        "coverage_start": timestamps[0].isoformat(),
        "coverage_end": timestamps[-1].isoformat(),
    }


def _bundle_checksum(components: dict[str, Any]) -> str:
    payload = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_canonical_spot(source_path: Path, destination_path: Path) -> None:
    with source_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "timestamp" not in reader.fieldnames:
            raise ValueError("The spot candle file must contain a timestamp column.")
        rows = list(reader)
        fieldnames = list(reader.fieldnames)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            source_timestamp = datetime.fromisoformat(row["timestamp"])
            row["timestamp"] = completed_candle_timestamp(
                source_timestamp,
                source_semantics="interval_start",
                interval_minutes=5,
            ).isoformat()
            writer.writerow(row)


def build_derivatives_bundle(
    bundle_dir: Path, spot_source: Path, *, source_semantics: str = "interval_start"
) -> dict[str, Any]:
    """Create the immutable manifest used to register and replay one data bundle."""

    bundle_dir = bundle_dir.resolve()
    spot_source = spot_source.resolve()
    if (bundle_dir / MANIFEST_FILENAME).exists():
        existing = load_derivatives_bundle(bundle_dir)
        verify_derivatives_bundle_files(bundle_dir, existing)
        canonical = (bundle_dir / existing["components"]["spot"]["path"]).resolve()
        if spot_source != canonical:
            raise ValueError(
                "An immutable bundle already exists. Reuse its canonical spot path or build in a new directory."
            )
        return existing
    if source_semantics not in {"interval_start", "interval_end"}:
        raise ValueError("Explicit supported spot timestamp semantics are required.")
    required = {
        "futures": bundle_dir / "futures" / "nifty_futures_5min.parquet",
        "india_vix": bundle_dir / "vix" / "india_vix_5min.parquet",
        "contracts": bundle_dir / "contracts_master.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    option_files = sorted((bundle_dir / "options").glob("*/*.parquet"))
    if not option_files:
        missing.append("option_ohlc")
    if missing:
        raise ValueError("Bundle inputs are missing: " + ", ".join(missing))

    spot_path = bundle_dir / "spot" / "nifty_spot_5min.csv"
    spot_path.parent.mkdir(parents=True, exist_ok=True)
    if spot_source == spot_path and source_semantics != "interval_end":
        raise ValueError("An already canonical spot file requires explicit interval_end semantics.")
    if spot_source != spot_path:
        if source_semantics == "interval_end":
            import shutil

            shutil.copyfile(spot_source, spot_path)
        else:
            _write_canonical_spot(spot_source, spot_path)

    with required["contracts"].open("r", encoding="utf-8") as source:
        contracts = json.load(source)
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("Contract master must contain at least one option contract.")

    def relative(path: Path) -> str:
        return path.relative_to(bundle_dir).as_posix()

    spot_profile = _csv_profile(spot_path)
    spot_profile["path"] = relative(spot_path)
    futures_profile = _parquet_profile(required["futures"])
    futures_profile["path"] = relative(required["futures"])
    vix_profile = _parquet_profile(required["india_vix"])
    vix_profile["path"] = relative(required["india_vix"])

    option_checksums = {
        relative(path): _parquet_profile(path, allow_zero=True)["checksum"] for path in option_files
    }
    timestamp_contract = {
        "provider": "upstox",
        "source_semantics": "interval_start",
        "canonical_semantics": CANONICAL_TIMESTAMP_SEMANTICS,
        "interval_minutes": 5,
        "canonicalized_at_ingestion": True,
    }
    components: dict[str, Any] = {
        "spot": {
            **spot_profile,
            "timestamp_contract": {**timestamp_contract, "source_semantics": source_semantics},
        },
        "futures": {**futures_profile, "timestamp_contract": timestamp_contract},
        "india_vix": {**vix_profile, "timestamp_contract": timestamp_contract},
        "contracts": {
            "path": relative(required["contracts"]),
            "checksum": sha256_file(required["contracts"]),
            "contract_count": len(contracts),
        },
        "option_ohlc": {
            "file_count": len(option_files),
            "files": option_checksums,
            "timestamp_contract": timestamp_contract,
        },
    }
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "canonical_timestamp_semantics": CANONICAL_TIMESTAMP_SEMANTICS,
        "bundle_checksum": _bundle_checksum(components),
        "capabilities": {
            "price_candles": True,
            "futures_vwap": True,
            "india_vix": True,
            "option_ohlc": True,
            "option_contracts": True,
            "executable_bid_ask": False,
        },
        "components": components,
        "limitations": [
            "Option candles contain OHLC, volume and open interest but no executable bid/ask quotes.",
            "Historical option fills must be labelled estimated_historical_ohlc.",
        ],
    }
    manifest_path = bundle_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return cast(dict[str, Any], manifest)


def load_derivatives_bundle(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir.resolve() / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {
        BUNDLE_SCHEMA_VERSION,
        LEGACY_BUNDLE_SCHEMA_VERSION,
    }:
        raise ValueError("Unsupported derivatives bundle schema version.")
    expected = manifest.get("bundle_checksum")
    actual = _bundle_checksum(manifest.get("components") or {})
    if expected != actual:
        raise ValueError("Derivatives bundle manifest checksum is invalid.")
    if manifest.get("schema_version") == BUNDLE_SCHEMA_VERSION:
        if manifest.get("canonical_timestamp_semantics") != CANONICAL_TIMESTAMP_SEMANTICS:
            raise ValueError("Unknown canonical candle timestamp semantics.")
        for key in ("spot", "futures", "india_vix", "option_ohlc"):
            contract = (manifest.get("components", {}).get(key, {}) or {}).get("timestamp_contract")
            if not isinstance(contract, dict):
                raise ValueError(f"Missing timestamp contract for {key}.")
            if contract.get("canonical_semantics") != CANONICAL_TIMESTAMP_SEMANTICS:
                raise ValueError(f"Unsupported canonical timestamp semantics for {key}.")
    return cast(dict[str, Any], manifest)


def verify_derivatives_bundle_files(bundle_dir: Path, manifest: dict[str, Any]) -> None:
    """Fail when any snapshotted component is absent or has changed bytes."""

    bundle_dir = bundle_dir.resolve()
    components = manifest["components"]
    canonical_paths = {
        "spot": "spot/nifty_spot_5min.csv",
        "futures": "futures/nifty_futures_5min.parquet",
        "india_vix": "vix/india_vix_5min.parquet",
        "contracts": "contracts_master.json",
    }
    for key in ("spot", "futures", "india_vix", "contracts"):
        component = components[key]
        path = (bundle_dir / component["path"]).resolve()
        if not path.is_relative_to(bundle_dir) or component["path"] != canonical_paths[key]:
            raise ValueError(f"Invalid or noncanonical derivatives component path: {key}.")
        if not path.is_file() or sha256_file(path) != component["checksum"]:
            raise ValueError(f"Derivatives bundle component changed: {key}.")
    for relative_path, checksum in components["option_ohlc"]["files"].items():
        path = (bundle_dir / relative_path).resolve()
        if not path.is_relative_to(bundle_dir / "options") or path.suffix != ".parquet":
            raise ValueError("Invalid derivatives option component path.")
        if not path.is_file() or sha256_file(path) != checksum:
            raise ValueError(f"Derivatives option component changed: {relative_path}.")
