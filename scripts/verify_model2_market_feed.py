"""Verify a fixed four-file real-data sample; never select or backtest a strategy."""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from app.domain.model2.market import BAR_INTERVAL, EXCHANGE_ZONE
from app.domain.trading_calendar import NSE_FO_CALENDAR, NSE_FO_CALENDAR_SOURCES
from app.infrastructure.datasets.derivatives_bundle import load_derivatives_bundle, sha256_file
from app.infrastructure.datasets.model2_bundle import BundleComponent, build_model2_bundle
from app.infrastructure.market_data.model2_feed import Model2BundleFeed

SAMPLE_EXPIRY = date(2026, 1, 6)
SAMPLE_ENTRY = date(2026, 1, 5)
# Hand-declared data-reading sample, not Model 2's strategy-selected strikes.
SAMPLE_OPTIONS = ((26000, "CE"), (26200, "CE"), (25900, "PE"), (25700, "PE"))


def verify_sample(source: Path) -> dict[str, Any]:
    manifest = load_derivatives_bundle(source)
    if manifest["schema_version"] != "stockbot.derivatives-bundle.v2":
        raise ValueError("The sample importer requires explicit v2 completed-candle semantics.")
    contracts = json.loads((source / "contracts_master.json").read_text())
    rows = [
        item
        for item in contracts
        if item["expiry"] == str(SAMPLE_EXPIRY)
        and (int(item["strike_price"]), item["instrument_type"]) in SAMPLE_OPTIONS
    ]
    if len(rows) != 4:
        raise ValueError("The declared four-contract sample is not installed.")
    component_sources = {
        manifest["components"][key]["path"]: manifest["components"][key]["checksum"]
        for key in ("spot", "india_vix", "contracts")
    }
    for strike, option_type in SAMPLE_OPTIONS:
        path = f"options/{SAMPLE_EXPIRY}/{strike}_{option_type}.parquet"
        component_sources[path] = manifest["components"]["option_ohlc"]["files"][path]
    for path, checksum in component_sources.items():
        if Path(path).is_absolute() or not (source / path).resolve().is_relative_to(
            source.resolve()
        ):
            raise ValueError("A source component escapes the supplied bundle.")
        if sha256_file(source / path) != checksum:
            raise ValueError("A declared source sample differs from its immutable manifest.")

    with TemporaryDirectory(prefix="stockbot-model2-feed-") as temporary:
        root = Path(temporary)
        components = []

        def add(kind: str, path: str, contract_id: str | None = None) -> None:
            components.append(
                BundleComponent.model_validate(
                    {
                        "kind": kind,
                        "path": path,
                        "contract_id": contract_id,
                        "checksum": sha256_file(root / path),
                        "timestamp_semantics": "interval_end"
                        if kind in {"spot", "india_vix", "options"}
                        else None,
                        "source_reference": f"Existing immutable V6 sample {manifest['bundle_checksum']}; legacy timestamp/tick conventions retained",
                    }
                )
            )

        for kind in ("spot", "india_vix"):
            path = manifest["components"][kind]["path"]
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / path, root / path)
            add(kind, path)
        native_contracts = []
        for row in rows:
            raw_tick = Decimal(str(row["tick_size"]))
            # This V6 snapshot records provider ticks in paise, explicitly checked.
            if raw_tick != Decimal("5"):
                raise ValueError("This fixed sample expects the retained five-paise source tick.")
            native_contracts.append(
                {
                    "contract_id": row["instrument_key"],
                    "symbol": row["trading_symbol"],
                    "expiry": row["expiry"],
                    "strike": int(row["strike_price"]),
                    "option_type": row["instrument_type"],
                    "lot_size": int(row["lot_size"]),
                    "tick_size_inr": str(raw_tick / 100),
                }
            )
            path = f"options/{SAMPLE_EXPIRY}/{int(row['strike_price'])}_{row['instrument_type']}.parquet"
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / path, root / path)
            add("options", path, row["instrument_key"])
        (root / "contracts.json").write_text(json.dumps(native_contracts))
        add("contracts", "contracts.json")
        context = NSE_FO_CALENDAR.previous_session(SAMPLE_ENTRY)
        sessions = []
        for day in NSE_FO_CALENDAR.sessions(context, SAMPLE_EXPIRY):
            grid = NSE_FO_CALENDAR.completed_candle_times(day)
            sessions.append(
                {
                    "session_date": str(day),
                    "kind": "NORMAL",
                    "opens_at": (grid[0] - BAR_INTERVAL).time().isoformat(),
                    "closes_at": grid[-1].time().isoformat(),
                }
            )
        calendar = {
            "schema_version": "stockbot.model2-calendar.v1",
            "version": f"{NSE_FO_CALENDAR.version}.model2-sample",
            "coverage_start": str(context),
            "coverage_end": str(SAMPLE_EXPIRY),
            "sessions": sessions,
            "weekly_expiries": [str(SAMPLE_EXPIRY)],
            "expiry_reference": "Explicit 2026-01-06 sample expiry from original contract metadata; not a broader expiry-catalog audit",
        }
        (root / "calendar.json").write_text(json.dumps(calendar))
        add("calendar", "calendar.json")
        native_manifest = build_model2_bundle(root, components)
        feed = Model2BundleFeed(root)
        pinned = feed.pin_cycle(feed.calendar.cycles(SAMPLE_ENTRY, SAMPLE_EXPIRY)[0])
        entry = datetime.combine(SAMPLE_ENTRY, time(15, 20), EXCHANGE_ZONE)
        expiry_open = datetime.combine(SAMPLE_EXPIRY, time(9, 15), EXCHANGE_ZONE)
        observations = []
        for strike, option_type in SAMPLE_OPTIONS:
            contract = pinned.contract(strike, option_type)  # type: ignore[arg-type]
            if contract is None:
                raise ValueError("Declared sample contract disappeared.")
            opening = pinned.option_open(contract, entry, as_of=entry)
            previous = pinned.option_close(contract, entry, as_of=entry)
            next_day = pinned.option_open(contract, expiry_open, as_of=expiry_open)
            prior_day = pinned.option_close(
                contract, feed.calendar.previous_completed_at(expiry_open), as_of=expiry_open
            )
            if any(item is None for item in (opening, previous, next_day, prior_day)):
                raise ValueError("The real-data sample lacks a required two-session observation.")
            observations.append(
                {
                    "contract": asdict(contract),
                    "entry_open": asdict(opening),
                    "entry_preceding_bar": asdict(previous),
                    "expiry_open": asdict(next_day),
                    "expiry_preceding_bar": asdict(prior_day),
                }
            )
        feed.verify_snapshot()
        result = {
            "schema_version": "stockbot.model2-feed-verification.v1",
            "scope": "Fixed data-reading sample only; not strategy-selected legs, fills or P&L",
            "source_bundle_checksum": manifest["bundle_checksum"],
            "sample_bundle_checksum": native_manifest.bundle_checksum,
            "source_component_checksums": component_sources,
            "native_components": [
                item.model_dump(mode="json") for item in native_manifest.components
            ],
            "calendar_reference_version": NSE_FO_CALENDAR.version,
            "calendar_sources": NSE_FO_CALENDAR_SOURCES,
            "calendar_qualification": "CURRENT_PLATFORM_REFERENCE_SAMPLE_NOT_NEW_EXCHANGE_AUDIT",
            "entry_session": str(SAMPLE_ENTRY),
            "expiry": str(SAMPLE_EXPIRY),
            "observations": observations,
            "futures_required": False,
            "official_settlement_supplied": False,
            "halt_evidence_supplied": False,
            "temporary_bundle_retained": False,
        }
    result["original_source_bytes_unchanged"] = all(
        sha256_file(source / path) == expected for path, expected in component_sources.items()
    )
    if not result["original_source_bytes_unchanged"]:
        raise ValueError("Source bytes changed during sample verification.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-bundle", type=Path, default=Path("data/datasets/upstox_nifty_derivatives_v6")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify_sample(args.source_bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    print(
        json.dumps(
            {
                "contracts_read": len(result["observations"]),
                "entry_session": result["entry_session"],
                "expiry": result["expiry"],
                "original_source_bytes_unchanged": result["original_source_bytes_unchanged"],
            }
        )
    )


if __name__ == "__main__":
    main()
