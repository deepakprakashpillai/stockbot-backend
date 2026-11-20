"""Assemble completed, declared Model 2 option acquisitions into a native v3 bundle.

Operator-only: no network, credentials, interpolation, pricing models or orders.
Source archives remain separate from claims of usable option history.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.infrastructure.datasets.derivatives_bundle import load_derivatives_bundle, sha256_file
from app.infrastructure.datasets.model2_bundle import (
    BundleComponent,
    ContractPayload,
    build_model2_bundle,
    decode_bar,
    price_rows,
)
from scripts.download_option_candles import COLUMNS, COMPLETED, load_plan


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n")


def normalize_contract(contract: dict[str, Any]) -> dict[str, Any]:
    key = contract.get("instrument_key") or contract.get("expired_instrument_key")
    expiry = str(contract["expiry"])[:10]
    kind = contract.get("instrument_type") or contract.get("option_type")
    if (
        not isinstance(key, str)
        or not key.startswith("NSE_FO|")
        or not key.endswith("|" + datetime.fromisoformat(expiry).strftime("%d-%m-%Y"))
        or contract.get("underlying_symbol") != "NIFTY"
        or contract.get("underlying_key") != "NSE_INDEX|Nifty 50"
    ):
        raise ValueError("Expected an actual expired NIFTY contract identity.")
    strike, lot = Decimal(str(contract["strike_price"])), Decimal(str(contract["lot_size"]))
    if not strike.is_finite() or strike != strike.to_integral_value():
        raise ValueError("Strike must be an exact integer.")
    if not lot.is_finite() or lot != lot.to_integral_value():
        raise ValueError("Lot size must be an exact integer.")
    # Upstox metadata uses paise; the existing loader also divides tick_size by 100.
    converted = {
        "contract_id": key,
        "symbol": contract["trading_symbol"],
        "expiry": expiry,
        "strike": int(strike),
        "option_type": kind,
        "lot_size": int(lot),
        "tick_size_inr": str(Decimal(str(contract["tick_size"])) / 100),
    }
    return ContractPayload.model_validate(converted).model_dump(mode="json")


def verify_complete_downloads(acquisition: Path) -> list[dict[str, Any]]:
    expected = {
        request.identity: request for request in load_plan(acquisition / "download-plan.json")
    }
    report = json.loads((acquisition / "downloaded/report.json").read_text())
    results = report["requests"]
    ids = [item["request_id"] for item in results]
    if (
        report["requested"] != len(expected)
        or report["processed"] != len(expected)
        or len(ids) != len(set(ids))
        or set(ids) != set(expected)
    ):
        raise ValueError("Downloads are incomplete or do not match the exact declared request set.")
    verified = []
    for item in results:
        identity = item["request_id"]
        if item["status"] not in COMPLETED or item["request"] != expected[identity].declaration:
            raise ValueError("Failed request or changed download declaration.")
        folder = acquisition / "downloaded/requests" / identity
        local = json.loads((folder / "report.json").read_text())
        if any(
            local.get(key) != value for key, value in item.items() if key not in {"path", "resumed"}
        ):
            raise ValueError("Aggregate/local download report mismatch.")
        if set(item["checksums"]) != {"raw.json", "candles.csv", "contract.json"}:
            raise ValueError("Expected explicit market-data artifact checksum whitelist.")
        for name, checksum in item["checksums"].items():
            if sha256_file(folder / name) != checksum:
                raise ValueError("Downloaded artifact checksum mismatch.")
        if json.loads((folder / "contract.json").read_text()) != item["request"]["contract"]:
            raise ValueError("Captured contract differs from requested metadata.")
        verified.append({**item, "verified_folder": folder})
    return verified


def merge_rows(paths: list[Path], contract_id: str) -> tuple[list[dict[str, str]], int]:
    """Keep source starts; identical numeric duplicates collapse, conflicts fail."""
    merged: dict[datetime, dict[str, str]] = {}
    duplicates = 0
    component = BundleComponent(
        kind="options",
        path="unused.csv",
        checksum="0" * 64,
        source_reference="Verified source candles",
        timestamp_semantics="interval_start",
        contract_id=contract_id,
    )
    for path in paths:
        for raw in price_rows(path):
            row = {key: str(raw.get(key, "")) for key in COLUMNS}
            bar = decode_bar(row, component)
            existing = merged.get(bar.interval_start)
            if existing is not None:
                same = all(
                    (
                        existing[key] == row[key]
                        if not existing[key] or not row[key]
                        else Decimal(existing[key]) == Decimal(row[key])
                    )
                    for key in COLUMNS[1:]
                )
                if not same:
                    raise ValueError(f"Conflicting duplicate timestamps for {contract_id}.")
                duplicates += 1
            else:
                merged[bar.interval_start] = row
    return [merged[key] for key in sorted(merged)], duplicates


def verify_reuse(record: dict[str, Any], source_root: Path) -> tuple[Path, dict[str, Any]]:
    root = source_root.resolve()
    source = Path(record["source_file"]).resolve()
    if not source.is_relative_to(root) or record["timestamp_semantics"] != "interval_end":
        raise ValueError("Reused file must be inside the declared interval-end bundle.")
    manifest = load_derivatives_bundle(root)
    if manifest["bundle_checksum"] != record["source_bundle_checksum"]:
        raise ValueError("Reused bundle identity changed.")
    relative = source.relative_to(root).as_posix()
    if manifest["components"]["option_ohlc"]["files"].get(relative) != record["source_sha256"]:
        raise ValueError("Reused path/hash is not declared by the source bundle.")
    if sha256_file(source) != record["source_sha256"]:
        raise ValueError("Reused file checksum changed.")
    master = root / manifest["components"]["contracts"]["path"]
    if (
        not master.resolve().is_relative_to(root)
        or sha256_file(master) != manifest["components"]["contracts"]["checksum"]
    ):
        raise ValueError("Reused contracts-master provenance changed.")
    contract = normalize_contract(record["contract"])
    matches = [
        item
        for item in json.loads(master.read_text())
        if item["instrument_key"] == contract["contract_id"]
    ]
    if len(matches) != 1 or normalize_contract(matches[0]) != contract:
        raise ValueError("Reused file contract differs from captured source metadata.")
    expected = (
        f"options/{contract['expiry']}/{contract['strike']}_{contract['option_type']}.parquet"
    )
    if relative != expected:
        raise ValueError("Reused path does not match its exact expiry/strike/type.")
    return source, contract


def assemble(
    acquisition: Path,
    output: Path,
    spot_csv: Path,
    vix_csv: Path,
    reuse_root: Path,
    calendar_dir: Path | None = None,
) -> dict[str, Any]:
    # Complete/verified acquisition is mandatory before creating any output.
    downloaded = verify_complete_downloads(acquisition)
    plan = json.loads((acquisition / "download-plan.json").read_text())
    reused = json.loads((acquisition / "reuse-plan.json").read_text())
    calendar_dir = calendar_dir or acquisition / "calendar-source-v1"
    calendar_audit = json.loads((calendar_dir / "calendar_qualification.json").read_text())
    if calendar_audit["qualification"] != "QUALIFIED_SESSIONS":
        raise ValueError("Calendar session reconciliation has not passed.")
    if plan["requested_range"] != [
        calendar_audit["requested_start"],
        calendar_audit["requested_end"],
    ]:
        raise ValueError("Acquisition and calendar requested ranges disagree.")
    if sha256_file(spot_csv) != calendar_audit["spot_source_sha256"]:
        raise ValueError("Spot source differs from qualified calendar observations.")
    if (
        sha256_file(acquisition / "expiry-evidence.json")
        != calendar_audit["expiry_evidence_sha256"]
    ):
        raise ValueError("Expiry evidence differs from qualified calendar evidence.")
    if (
        sha256_file(Path(__file__).with_name("model2_calendar_sources_2023_2026.v1.json"))
        != calendar_audit["calendar_sources_sha256"]
    ):
        raise ValueError("Official calendar source facts changed after qualification.")
    contracts: dict[str, dict[str, Any]] = {}
    chunks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in downloaded:
        contract = normalize_contract(item["request"]["contract"])
        key = contract["contract_id"]
        if key in contracts and contracts[key] != contract:
            raise ValueError("Conflicting metadata across monthly chunks.")
        contracts[key] = contract
        chunks[key].append(item)
    reuse_checked = []
    for item in reused:
        source, contract = verify_reuse(item, reuse_root)
        key = contract["contract_id"]
        if key in contracts:
            raise ValueError("A contract cannot use mixed source timestamp semantics.")
        contracts[key] = contract
        reuse_checked.append((item, source, contract))
    if not contracts:
        raise ValueError("No declared contracts.")

    output.mkdir(parents=True, exist_ok=False)
    components: list[dict[str, Any]] = []

    def copy(source: Path, relative: str) -> Path:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if sha256_file(source) != sha256_file(destination):
            raise ValueError("Source changed while copying.")
        return destination

    for name in ("download-plan.json", "reuse-plan.json", "expiry-evidence.json"):
        copy(acquisition / name, "evidence/" + name)
    copy(acquisition / "downloaded/report.json", "evidence/downloaded/report.json")
    copy(calendar_dir / "calendar_qualification.json", "evidence/calendar_qualification.json")
    copy(
        Path(__file__).with_name("model2_calendar_sources_2023_2026.v1.json"),
        "evidence/calendar_sources.json",
    )
    copy(reuse_root / "bundle_manifest.json", "evidence/reused_bundle_manifest.json")
    copy(reuse_root / "contracts_master.json", "evidence/reused_contracts_master.json")
    copy(spot_csv, "spot.csv")
    copy(vix_csv, "india_vix.csv")
    for name in ("README.md", "download_report_2023-11-16_to_2026-11-15.json"):
        source_report = spot_csv.parent / name
        if source_report.is_file():
            copy(source_report, "evidence/source_market_history/" + name)
    copy(calendar_dir / "calendar.json", "calendar.json")
    write_json(
        output / "contracts.json", sorted(contracts.values(), key=lambda item: item["contract_id"])
    )
    for kind, path, semantics in (
        ("spot", "spot.csv", "interval_start"),
        ("india_vix", "india_vix.csv", "interval_start"),
        ("calendar", "calendar.json", None),
        ("contracts", "contracts.json", None),
    ):
        components.append({"kind": kind, "path": path, "timestamp_semantics": semantics})

    series_audit: list[dict[str, Any]] = []
    for key, items in sorted(chunks.items()):
        paths = []
        for item in items:
            folder = item["verified_folder"]
            for name in ("raw.json", "candles.csv", "contract.json", "report.json"):
                copy(folder / name, f"evidence/downloaded/requests/{item['request_id']}/{name}")
            paths.append(folder / "candles.csv")
            actual_count = sum(1 for _ in price_rows(folder / "candles.csv"))
            if actual_count != item["row_count"]:
                raise ValueError("Downloaded row count differs from report.")
        rows, duplicates = merge_rows(paths, key)
        relative = "options/" + hashlib.sha256(key.encode()).hexdigest() + ".csv"
        if rows:
            csv_destination = output / relative
            csv_destination.parent.mkdir(exist_ok=True)
            with csv_destination.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            components.append(
                {
                    "kind": "options",
                    "path": relative,
                    "contract_id": key,
                    "timestamp_semantics": "interval_start",
                }
            )
        series_audit.append(
            {
                "contract_id": key,
                "source": "FRESH_CAPTURE",
                "row_count": len(rows),
                "component_declared": bool(rows),
                "identical_chunk_duplicates": duplicates,
                "coverage_start": rows[0]["timestamp"] if rows else None,
                "coverage_end": rows[-1]["timestamp"] if rows else None,
                "requests": [
                    {
                        field: item[field]
                        for field in (
                            "request_id",
                            "status",
                            "invalid_bars",
                            "identical_duplicates",
                            "raw_candle_count",
                        )
                    }
                    for item in items
                ],
            }
        )

    for item, source, contract in reuse_checked:
        key = contract["contract_id"]
        relative = "options/" + hashlib.sha256(key.encode()).hexdigest() + ".parquet"
        rows = list(price_rows(source))
        if rows:
            copy(source, relative)
            components.append(
                {
                    "kind": "options",
                    "path": relative,
                    "contract_id": key,
                    "timestamp_semantics": "interval_end",
                }
            )
        series_audit.append(
            {
                "contract_id": key,
                "source": "VERIFIED_V6_REUSE",
                "row_count": len(rows),
                "component_declared": bool(rows),
                "source_sha256": item["source_sha256"],
                "coverage_start": str(rows[0]["timestamp"]) if rows else None,
                "coverage_end": str(rows[-1]["timestamp"]) if rows else None,
            }
        )

    priced_expiries = sorted(
        {
            contracts[item["contract_id"]]["expiry"]
            for item in series_audit
            if item["component_declared"]
        }
    )
    selected_expiries = sorted({item["expiry"] for item in contracts.values()})
    empty_expiries = sorted(set(selected_expiries) - set(priced_expiries))
    observation_starts = []
    observation_ends = []
    for item in series_audit:
        if not item["component_declared"]:
            continue
        shift = timedelta(minutes=5) if item["source"] == "VERIFIED_V6_REUSE" else timedelta(0)
        observation_starts.append(datetime.fromisoformat(item["coverage_start"]) - shift)
        observation_ends.append(datetime.fromisoformat(item["coverage_end"]) - shift)
    missing_expiries = [
        item
        for item in calendar_audit["scheduled_cycles"]
        if item["provider_status"] != "CONFIRMED_CE_PE"
    ]
    qualification = {
        "schema_version": "stockbot.model2-history-bundle-qualification.v1",
        "requested_range": plan["requested_range"],
        "scheduled_cycle_count": len(calendar_audit["scheduled_cycles"]),
        "provider_status_counts": calendar_audit["provider_status_counts"],
        "available_option_expiry_range": [priced_expiries[0], priced_expiries[-1]]
        if priced_expiries
        else None,
        "available_option_expiries": priced_expiries,
        "unconfirmed_scheduled_cycles": missing_expiries,
        "fully_empty_selected_expiries": empty_expiries,
        "available_option_observation_range_interval_start": [
            min(observation_starts).isoformat(),
            max(observation_ends).isoformat(),
        ]
        if observation_starts
        else None,
        "declared_contract_count": len(contracts),
        "declared_option_series_count": sum(item["component_declared"] for item in series_audit),
        "request_status_counts": dict(Counter(item["status"] for item in downloaded)),
        "invalid_source_row_reason_counts": dict(
            Counter(bar["reason"] for item in downloaded for bar in item["invalid_bars"])
        ),
        "selection": {
            "wing_presets_points": [150, 200],
            "purpose": plan["purpose"],
            "cycles": plan["cycles"],
            "outcome_based_selection": False,
        },
        "metadata_conversion": "Provider tick_size is paise; tick_size_inr = tick_size / 100. Contract identity, expiry, strike, option type and historical lot size are preserved. No current lot-size substitution.",
        "limits": [
            "Three-year spot/VIX archive does not imply three-year available option history.",
            "Selected contracts cover only the declared entry-derived strikes for 150/200-point presets, not the full option chain.",
            "Candles are OHLC proxies, not historical executable bid/ask prices.",
            "No prices, missing bars, settlements or risk decisions have been invented.",
            "Session/metadata qualification is not a backtest, validation approval or live-trading approval.",
        ],
        "series": sorted(series_audit, key=lambda item: item["contract_id"]),
    }
    write_json(output / "bundle_qualification.json", qualification)
    # Bind evidence through the native immutable component provenance. Only market
    # responses and explicit artifacts are copied, never a credential directory.
    index = {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    write_json(
        output / "evidence/evidence_index.json",
        {"schema_version": "stockbot.model2-evidence-index.v1", "files": index},
    )
    reference = "Declared acquisition; evidence/evidence_index.json sha256=" + sha256_file(
        output / "evidence/evidence_index.json"
    )
    native = [
        BundleComponent(
            **item, checksum=sha256_file(output / item["path"]), source_reference=reference
        )
        for item in components
    ]
    manifest = build_model2_bundle(output, native)
    return {
        "bundle_checksum": manifest.bundle_checksum,
        "output": str(output),
        "contracts": len(contracts),
        "option_series": qualification["declared_option_series_count"],
        "available_option_expiry_range": qualification["available_option_expiry_range"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquisition-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--spot-csv", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, required=True)
    parser.add_argument("--reuse-bundle-root", type=Path, required=True)
    parser.add_argument(
        "--calendar-dir", type=Path, help="Defaults to acquisition-root/calendar-source-v1."
    )
    args = parser.parse_args()
    print(
        json.dumps(
            assemble(
                args.acquisition_root,
                args.output_dir,
                args.spot_csv,
                args.vix_csv,
                args.reuse_bundle_root,
                args.calendar_dir,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
