"""Preserve and replay one declared real V6 cycle, never optimize or alter sources."""

from __future__ import annotations

import argparse
import csv
import json
import resource
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from pydantic import TypeAdapter

from app.domain.model2.clock import iter_replay
from app.domain.model2.execution import ReplayRequest, market_at
from app.domain.model2.parameters import Model2Parameters
from app.domain.model2.selection import short_strikes
from app.domain.trading_calendar import NSE_FO_CALENDAR
from app.infrastructure.datasets.derivatives_bundle import load_derivatives_bundle, sha256_file
from app.infrastructure.datasets.model2_bundle import (
    BundleComponent,
    build_model2_bundle,
    price_rows,
)
from app.infrastructure.market_data.model2_feed import Model2BundleFeed

ENTRY = date(2026, 1, 5)
EXPIRY = date(2026, 1, 6)


def qualify(source: Path, destination: Path) -> dict:
    if destination.exists():
        raise ValueError("Destination must be new; existing snapshots are never overwritten.")
    legacy = load_derivatives_bundle(source)
    if legacy["schema_version"] != "stockbot.derivatives-bundle.v2":
        raise ValueError("This declared importer requires interval-end V6 sources.")
    context = NSE_FO_CALENDAR.previous_session(ENTRY)
    spot = list(price_rows(source / legacy["components"]["spot"]["path"]))
    vix = list(price_rows(source / legacy["components"]["india_vix"]["path"]))
    key = market_at(ENTRY, 15, 20).isoformat()
    spot_close = next(row["close"] for row in spot if str(row["timestamp"]) == key)
    vix_close = next(row["close"] for row in vix if str(row["timestamp"]) == key)
    call, put = short_strikes(Decimal(str(spot_close)), Decimal(str(vix_close)))
    requested = ((call, "CE"), (call + 200, "CE"), (put, "PE"), (put - 200, "PE"))
    master = json.loads((source / "contracts_master.json").read_text())
    selected = [
        row
        for row in master
        if row["expiry"] == str(EXPIRY)
        and (int(row["strike_price"]), row["instrument_type"]) in requested
    ]
    if len(selected) != 4:
        raise ValueError("The fixed sample lacks the strategy-selected contracts.")
    originals = {
        legacy["components"][kind]["path"]: legacy["components"][kind]["checksum"]
        for kind in ("spot", "india_vix", "contracts")
    }
    for row in selected:
        path = f"options/{EXPIRY}/{int(row['strike_price'])}_{row['instrument_type']}.parquet"
        originals[path] = legacy["components"]["option_ohlc"]["files"][path]
    for path, expected in originals.items():
        if sha256_file(source / path) != expected:
            raise ValueError("Source component checksum mismatch.")
    destination.mkdir(parents=True)
    components = []

    def add(kind: str, path: str, contract_id: str | None = None) -> None:
        components.append(
            BundleComponent(
                kind=kind,
                path=path,
                checksum=sha256_file(destination / path),
                source_reference=f"Immutable V6 {legacy['bundle_checksum']}; original component hashes retained in qualification report; filtered sessions, interval-end convention retained",
                timestamp_semantics="interval_end" if path.endswith(".csv") else None,
                contract_id=contract_id,
            )
        )

    def prices(kind: str, rows: list[dict], path: str, contract_id: str | None = None) -> None:
        values = [row for row in rows if str(context) <= str(row["timestamp"])[:10] <= str(EXPIRY)]
        if not values:
            raise ValueError("Fixed sample component has no rows.")
        with (destination / path).open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(values[0]))
            writer.writeheader()
            writer.writerows(values)
        add(kind, path, contract_id)

    prices("spot", spot, "spot.csv")
    prices("india_vix", vix, "vix.csv")
    contracts = []
    for row in selected:
        if Decimal(str(row["tick_size"])) != 5:
            raise ValueError("Fixed V6 contract requires explicit five-paise source tick.")
        contracts.append(
            {
                "contract_id": row["instrument_key"],
                "symbol": row["trading_symbol"],
                "expiry": row["expiry"],
                "strike": int(row["strike_price"]),
                "option_type": row["instrument_type"],
                "lot_size": int(row["lot_size"]),
                "tick_size_inr": "0.05",
            }
        )
        original = f"options/{EXPIRY}/{int(row['strike_price'])}_{row['instrument_type']}.parquet"
        prices(
            "options",
            list(price_rows(source / original)),
            f"{row['strike_price']}_{row['instrument_type']}.csv",
            row["instrument_key"],
        )
    (destination / "contracts.json").write_text(json.dumps(contracts, indent=2))
    add("contracts", "contracts.json")
    sessions = []
    for day in NSE_FO_CALENDAR.sessions(context, EXPIRY):
        grid = NSE_FO_CALENDAR.completed_candle_times(day)
        sessions.append(
            {
                "session_date": str(day),
                "kind": "NORMAL",
                "opens_at": (grid[0] - timedelta(minutes=5)).time().isoformat(),
                "closes_at": grid[-1].time().isoformat(),
            }
        )
    calendar = {
        "schema_version": "stockbot.model2-calendar.v1",
        "version": f"{NSE_FO_CALENDAR.version}.fixed-2026-01-06",
        "coverage_start": str(context),
        "coverage_end": str(EXPIRY),
        "sessions": sessions,
        "weekly_expiries": [str(EXPIRY)],
        "expiry_reference": "Declared 2026-01-06 expiry matches preserved contract metadata; current platform NSE calendar reference, no wider calendar qualification inferred",
    }
    (destination / "calendar.json").write_text(json.dumps(calendar, indent=2))
    add("calendar", "calendar.json")
    manifest = build_model2_bundle(destination, components)
    started = perf_counter()
    feed = Model2BundleFeed(destination)
    request = ReplayRequest(
        manifest.bundle_checksum,
        ENTRY,
        EXPIRY,
        Decimal(500000),
        Model2Parameters(),
        ((EXPIRY, market_at(EXPIRY, 15, 30)),),
    )
    events = list(iter_replay(request, feed, feed.calendar, "MAX_LOSS_RISK"))
    results = [event.result for event in events if event.result]
    if len(results) != 1 or results[0].valuation is None:
        raise ValueError(
            f"Declared sample did not produce a priced cycle: {[result.reason for result in results]}"
        )
    unchanged = all(sha256_file(source / path) == expected for path, expected in originals.items())
    if not unchanged:
        raise ValueError("Original data changed.")
    return {
        "schema_version": "stockbot.model2-real-sample-qualification.v1",
        "entry_date": str(ENTRY),
        "expiry": str(EXPIRY),
        "source_bundle_checksum": legacy["bundle_checksum"],
        "source_components": originals,
        "source_bytes_unchanged": unchanged,
        "tick_conversion": "Provider metadata 5 paise -> INR 0.05; price values unchanged",
        "native_manifest": manifest.model_dump(mode="json"),
        "request": TypeAdapter(ReplayRequest).dump_python(request, mode="json"),
        "result": TypeAdapter(type(results[0])).dump_python(results[0], mode="json"),
        "summary": TypeAdapter(type(events[-1].summary)).dump_python(
            events[-1].summary, mode="json"
        ),
        "measurements": {
            "wall_seconds": perf_counter() - started,
            "peak_process_memory_bytes_macos": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "session_file_reads": feed.session_read_count,
            "cached_series_sessions": feed.cached_series_sessions,
            "journal_events": len(events),
        },
        "limitations": [
            "One fixed cycle, primary width 200, 2% slippage, not a profitability sample",
            "Opening OHLC estimates, assumed margin, placeholder charges; no executable bid/ask",
            "No official settlement/halt component supplied; sample exits using actual observed opening prices",
            "Calendar qualification limited to the declared cycle and existing platform reference",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = qualify(args.source, args.destination)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Qualified fixed real cycle; net INR {result['result']['valuation']['net_pnl_inr']}.")


if __name__ == "__main__":
    main()
