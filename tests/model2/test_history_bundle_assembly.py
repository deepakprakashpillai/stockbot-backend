import csv
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.infrastructure.datasets.derivatives_bundle import _bundle_checksum
from app.infrastructure.datasets.model2_bundle import load_model2_bundle, read_contracts
from scripts.assemble_model2_history_bundle import (
    assemble,
    merge_rows,
    normalize_contract,
    verify_complete_downloads,
    verify_reuse,
    write_json,
)
from scripts.build_model2_calendar import SOURCE_FILE, digest
from scripts.download_option_candles import CandleRequest, acquire, load_plan


def contract(kind: str = "CE", strike: int = 25000) -> dict:
    return {
        "instrument_key": f"NSE_FO|{strike}{kind}|28-10-2025",
        "expiry": "2025-10-28",
        "instrument_type": kind,
        "strike_price": strike,
        "lot_size": 75,
        "tick_size": 5,
        "underlying_symbol": "NIFTY",
        "underlying_key": "NSE_INDEX|Nifty 50",
        "trading_symbol": f"NIFTY {strike} {kind} 28 OCT 25",
    }


def csv_file(path: Path, rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "open_interest"])
        writer.writerows(rows)
    return path


def test_actual_identity_lot_and_tick_conversion() -> None:
    metadata = normalize_contract(contract())
    assert metadata["contract_id"] == contract()["instrument_key"]
    assert metadata["lot_size"] == 75
    assert metadata["tick_size_inr"] == "0.05"
    wrong = {**contract(), "strike_price": 25000.5}
    with pytest.raises(ValueError, match="exact integer"):
        normalize_contract(wrong)
    current_key = {**contract(), "instrument_key": "NSE_FO|25000CE"}
    with pytest.raises(ValueError, match="actual expired NIFTY"):
        normalize_contract(current_key)


def test_merge_sorts_source_starts_and_collapses_only_identical_rows(tmp_path: Path) -> None:
    first = csv_file(tmp_path / "a.csv", [["2025-10-28T09:20:00+05:30", 2, 2, 2, 2, 10, 0]])
    second = csv_file(
        tmp_path / "b.csv",
        [
            ["2025-10-28T09:15:00+05:30", 1, 1, 1, 1, 10, 0],
            ["2025-10-28T09:20:00+05:30", "2.0", 2, 2, 2, 10, 0],
        ],
    )
    rows, duplicates = merge_rows([first, second], contract()["instrument_key"])
    assert [row["timestamp"] for row in rows] == [
        "2025-10-28T09:15:00+05:30",
        "2025-10-28T09:20:00+05:30",
    ]
    assert duplicates == 1
    csv_file(second, [["2025-10-28T09:20:00+05:30", 3, 3, 3, 3, 10, 0]])
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        merge_rows([first, second], contract()["instrument_key"])


class FakeClient:
    request_count = 0

    def __init__(self, partial: bool = False):
        self.partial = partial

    def get(self, request: CandleRequest) -> dict:
        self.request_count += 1
        # A third selected contract is deliberately empty: metadata must survive.
        rows = (
            []
            if request.contract["strike_price"] == 25200
            else [
                [
                    "2025-10-27T15:20:00+05:30",
                    10,
                    10,
                    10,
                    10,
                    10,
                    0,
                ]
            ]
        )
        if self.partial and request.contract["instrument_type"] == "PE":
            rows.append(["2025-10-27T15:21:00+05:30", 10, 10, 10, 10, 10, 0])
        return {"status": "success", "data": {"candles": rows}}


def fixture_acquisition(tmp_path: Path, partial: bool = False) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "acquisition"
    root.mkdir()
    selected = [contract(), contract("PE"), contract("CE", 25200)]
    write_json(
        root / "download-plan.json",
        {
            "requested_range": ["2025-10-27", "2025-10-28"],
            "purpose": "Entry-only selection",
            "requests": [
                {"contract": value, "from_date": "2025-10-27", "to_date": "2025-10-28"}
                for value in selected
            ],
            "cycles": [
                {
                    "expiry": "2025-10-28",
                    "entry": "2025-10-27",
                    "short_call": 25000,
                    "short_put": 25000,
                }
            ],
        },
    )
    write_json(root / "reuse-plan.json", [])
    write_json(root / "expiry-evidence.json", {"fixture": "captured metadata"})
    acquire(load_plan(root / "download-plan.json"), root / "downloaded", FakeClient(partial))  # type: ignore[arg-type]
    rows = [["2025-10-27T15:15:00+05:30", 100, 100, 100, 100, 0, 0]]
    spot = csv_file(tmp_path / "spot.csv", rows)
    vix = csv_file(tmp_path / "vix.csv", rows)
    write_json(
        root / "calendar-source-v1/calendar.json",
        {
            "schema_version": "stockbot.model2-calendar.v1",
            "version": "fixture",
            "coverage_start": "2025-10-27",
            "coverage_end": "2025-10-28",
            "expiry_reference": "Fixture official sources",
            "sessions": [
                {
                    "session_date": day,
                    "kind": "NORMAL",
                    "opens_at": "09:15:00",
                    "closes_at": "15:30:00",
                }
                for day in ["2025-10-27", "2025-10-28"]
            ],
            "weekly_expiries": ["2025-10-28"],
        },
    )
    write_json(
        root / "calendar-source-v1/calendar_qualification.json",
        {
            "qualification": "QUALIFIED_SESSIONS",
            "requested_start": "2025-10-27",
            "requested_end": "2025-10-28",
            "spot_source_sha256": digest(spot),
            "expiry_evidence_sha256": digest(root / "expiry-evidence.json"),
            "calendar_sources_sha256": digest(SOURCE_FILE),
            "scheduled_cycles": [{"expiry": "2025-10-28", "provider_status": "CONFIRMED_CE_PE"}],
            "provider_status_counts": {"CONFIRMED_CE_PE": 1},
        },
    )
    reuse_root = tmp_path / "v6"
    reuse_root.mkdir()
    write_json(reuse_root / "bundle_manifest.json", {})
    write_json(reuse_root / "contracts_master.json", [])
    return root, spot, vix, reuse_root


def test_full_assembly_preserves_empty_metadata_and_market_evidence(tmp_path: Path) -> None:
    acquisition, spot, vix, reuse_root = fixture_acquisition(tmp_path)
    output = tmp_path / "bundle"
    result = assemble(acquisition, output, spot, vix, reuse_root)
    manifest = load_model2_bundle(output)
    assert result["contracts"] == 3
    assert result["option_series"] == 2
    contracts = read_contracts(output, manifest.component("contracts"))  # type: ignore[arg-type]
    assert len(contracts) == 3
    assert {item.timestamp_semantics for item in manifest.components if item.kind == "options"} == {
        "interval_start"
    }
    qualification = json.loads((output / "bundle_qualification.json").read_text())
    assert qualification["request_status_counts"] == {"COMPLETE": 2, "EMPTY": 1}
    assert sum(not item["component_declared"] for item in qualification["series"]) == 1
    assert len(list((output / "evidence/downloaded/requests").glob("*/raw.json"))) == 3
    evidence_index = json.loads((output / "evidence/evidence_index.json").read_text())
    assert all(
        digest(output / name) == checksum for name, checksum in evidence_index["files"].items()
    )
    assert all(
        digest(output / "evidence/evidence_index.json") in item.source_reference
        for item in manifest.components
    )
    with pytest.raises(FileExistsError):
        assemble(acquisition, output, spot, vix, reuse_root)


def test_incomplete_downloads_cannot_create_bundle(tmp_path: Path) -> None:
    acquisition, spot, vix, reuse_root = fixture_acquisition(tmp_path)
    report_path = acquisition / "downloaded/report.json"
    report = json.loads(report_path.read_text())
    report["requests"].pop()
    write_json(report_path, report)
    output = tmp_path / "bundle"
    with pytest.raises(ValueError, match="incomplete"):
        assemble(acquisition, output, spot, vix, reuse_root)
    assert not output.exists()


def test_tampered_download_cannot_pass_hash_verification(tmp_path: Path) -> None:
    acquisition, *_ = fixture_acquisition(tmp_path)
    first = next((acquisition / "downloaded/requests").glob("*/raw.json"))
    first.write_text("{}")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_complete_downloads(acquisition)


def test_reuse_rejects_escape_and_semantics_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside"):
        verify_reuse(
            {
                "source_file": str(tmp_path / "outside.parquet"),
                "timestamp_semantics": "interval_end",
            },
            tmp_path / "v6",
        )


def test_partial_invalid_rows_are_preserved_in_pinned_evidence(tmp_path: Path) -> None:
    acquisition, spot, vix, reuse_root = fixture_acquisition(tmp_path, partial=True)
    output = tmp_path / "bundle"
    assemble(acquisition, output, spot, vix, reuse_root)
    qualification = json.loads((output / "bundle_qualification.json").read_text())
    assert qualification["request_status_counts"]["PARTIAL_INVALID"] == 1
    assert qualification["invalid_source_row_reason_counts"] == {"off_grid_timestamp": 1}
    raw = [
        json.loads(path.read_text())
        for path in (output / "evidence/downloaded/requests").glob("*/raw.json")
    ]
    assert any(len(payload["data"]["candles"]) == 2 for payload in raw)


def test_verified_reuse_preserves_bytes_and_interval_end(tmp_path: Path) -> None:
    acquisition, spot, vix, reuse_root = fixture_acquisition(tmp_path)
    metadata = contract("PE", 24800)
    source = reuse_root / "options/2025-10-28/24800_PE.parquet"
    source.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "timestamp": ["2025-10-27T15:25:00+05:30"],
                "open": [10],
                "high": [10],
                "low": [10],
                "close": [10],
                "volume": [10],
            }
        ),
        source,
    )
    write_json(reuse_root / "contracts_master.json", [metadata])
    clock = {"timestamp_contract": {"canonical_semantics": "interval_end"}}
    components = {
        "spot": clock,
        "futures": clock,
        "india_vix": clock,
        "option_ohlc": {**clock, "files": {"options/2025-10-28/24800_PE.parquet": digest(source)}},
        "contracts": {
            "path": "contracts_master.json",
            "checksum": digest(reuse_root / "contracts_master.json"),
        },
    }
    checksum = _bundle_checksum(components)
    write_json(
        reuse_root / "bundle_manifest.json",
        {
            "schema_version": "stockbot.derivatives-bundle.v2",
            "canonical_timestamp_semantics": "interval_end",
            "bundle_checksum": checksum,
            "components": components,
        },
    )
    record = {
        "contract": metadata,
        "source_file": str(source),
        "source_sha256": digest(source),
        "source_bundle_checksum": checksum,
        "timestamp_semantics": "interval_end",
    }
    write_json(acquisition / "reuse-plan.json", [record])
    output = tmp_path / "bundle"
    assemble(acquisition, output, spot, vix, reuse_root)
    manifest = load_model2_bundle(output)
    reused = next(
        item for item in manifest.components if item.contract_id == metadata["instrument_key"]
    )
    assert reused.timestamp_semantics == "interval_end"
    assert (output / reused.path).read_bytes() == source.read_bytes()
    assert {item.timestamp_semantics for item in manifest.components if item.kind == "options"} == {
        "interval_start",
        "interval_end",
    }
    record["contract"] = {**metadata, "lot_size": 65}
    with pytest.raises(ValueError, match="metadata"):
        verify_reuse(record, reuse_root)
    with pytest.raises(ValueError, match="interval-end"):
        verify_reuse(
            {
                "source_file": str(tmp_path / "v6/options.parquet"),
                "timestamp_semantics": "interval_start",
            },
            tmp_path / "v6",
        )
