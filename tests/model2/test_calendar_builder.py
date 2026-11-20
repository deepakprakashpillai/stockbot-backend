"""Operator calendar evidence must not conceal unavailable historical cycles."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from app.infrastructure.datasets.model2_bundle import CalendarPayload
from scripts.build_model2_calendar import EVIDENCE_SCHEMA, build_artifacts, load_evidence


def spot_file(tmp_path: Path, timestamps: list[str]) -> Path:
    path = tmp_path / "spot.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for timestamp in timestamps:
            writer.writerow([timestamp, 100, 100, 100, 100, 0])
    return path


def evidence_file(tmp_path: Path, expiry: str, status: str = "AVAILABLE") -> Path:
    path = tmp_path / "evidence.json"
    contracts = (
        [
            {
                "expiry": expiry,
                "instrument_type": kind,
                "instrument_key": kind,
                "strike_price": 25000,
                "lot_size": 75,
                "tick_size": 5,
                "weekly": False,
            }
            for kind in ("CE", "PE")
        ]
        if status == "AVAILABLE"
        else []
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": EVIDENCE_SCHEMA,
                "source_reference": "Provider captured response",
                "queried_expiries": [
                    {
                        "requested_expiry": expiry,
                        "status": status,
                        "response_sha256": "a" * 64,
                        "contracts": contracts,
                    }
                ],
            }
        )
    )
    return path


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2025-08-25", "2025-09-09", ["2025-08-28", "2025-09-02", "2025-09-09"]),
        ("2025-04-01", "2025-04-15", ["2025-04-03", "2025-04-09"]),
        ("2025-04-28", "2025-05-02", ["2025-04-30"]),
        ("2025-10-17", "2025-10-23", ["2025-10-20"]),
        ("2024-08-12", "2024-08-16", ["2024-08-14"]),
    ],
)
def test_official_transitions_and_holidays(
    tmp_path: Path, start: str, end: str, expected: list[str]
) -> None:
    spot = spot_file(tmp_path, [f"{start}T09:15:00+05:30"])
    calendar, audit = build_artifacts(spot, date.fromisoformat(start), date.fromisoformat(end))
    assert calendar["weekly_expiries"] == expected
    assert all(row["provider_status"] == "UNQUERIED" for row in audit["scheduled_cycles"])
    CalendarPayload.model_validate(calendar)


def test_muhurat_precedes_november_2026_shifted_expiry(tmp_path: Path) -> None:
    spot = spot_file(
        tmp_path,
        ["2026-11-06T09:15:00+05:30", "2026-11-08T14:20:00+05:30", "2026-11-09T09:15:00+05:30"],
    )
    calendar, audit = build_artifacts(spot, date(2026, 11, 6), date(2026, 11, 10))
    assert calendar["weekly_expiries"] == ["2026-11-09"]
    assert audit["qualification"] == "QUALIFIED_SESSIONS"
    assert audit["scheduled_cycles"][0]["entry_session"] == "2026-11-08"
    assert audit["scheduled_cycles"][0]["calendar_classification"] == "SPECIAL_SESSION"
    session = next(item for item in calendar["sessions"] if item["session_date"] == "2026-11-08")
    assert session == {
        "session_date": "2026-11-08",
        "kind": "SPECIAL",
        "opens_at": "14:20:00",
        "closes_at": "15:40:00",
    }


def test_monthly_metadata_confirms_without_deleting_unavailable_cycles(tmp_path: Path) -> None:
    evidence = evidence_file(tmp_path, "2025-10-28")
    spot = spot_file(tmp_path, ["2025-10-20T09:15:00+05:30"])
    calendar, audit = build_artifacts(spot, date(2025, 10, 20), date(2025, 10, 28), evidence)
    assert calendar["weekly_expiries"] == ["2025-10-20", "2025-10-28"]
    assert audit["provider_status_counts"] == {"UNQUERIED": 1, "CONFIRMED_CE_PE": 1}
    assert audit["scheduled_cycles"][0]["calendar_classification"] == "OUTSIDE_COMPLETE_CYCLE_RANGE"


def test_empty_metadata_is_explicit_and_does_not_change_calendar(tmp_path: Path) -> None:
    spot = spot_file(tmp_path, ["2023-11-16T09:15:00+05:30"])
    evidence = evidence_file(tmp_path, "2023-11-16", "EMPTY")
    calendar, audit = build_artifacts(spot, date(2023, 11, 16), date(2023, 11, 16), evidence)
    assert calendar["coverage_start"] == "2023-11-15"
    assert calendar["weekly_expiries"] == ["2023-11-16"]
    assert audit["context_sessions_without_asserted_spot_coverage"] == ["2023-11-15"]
    assert audit["scheduled_cycles"][0]["provider_status"] == "EMPTY"
    assert audit["scheduled_cycles"][0]["calendar_classification"] == "OUTSIDE_COMPLETE_CYCLE_RANGE"


def test_out_of_grid_rows_do_not_invent_special_sessions(tmp_path: Path) -> None:
    spot = spot_file(tmp_path, ["2025-04-25T09:15:00+05:30", "2025-04-25T15:55:00+05:30"])
    calendar, audit = build_artifacts(spot, date(2025, 4, 25), date(2025, 4, 25))
    assert calendar["sessions"][-1]["kind"] == "NORMAL"
    assert audit["qualification"] == "QUALIFIED_SESSIONS"
    assert audit["normal_session_bar_findings"][0]["out_of_grid_opens"] == ["15:55:00"]
    assert len(audit["normal_session_bar_findings"][0]["missing_scheduled_opens"]) == 74


def test_missing_and_unannounced_sessions_cannot_qualify(tmp_path: Path) -> None:
    spot = spot_file(tmp_path, ["2025-04-26T09:15:00+05:30"])
    _, audit = build_artifacts(spot, date(2025, 4, 25), date(2025, 4, 26))
    assert audit["qualification"] == "SESSION_RECONCILIATION_FAILED"
    assert audit["missing_source_session_dates"] == ["2025-04-25"]
    assert audit["unexpected_source_session_dates"] == ["2025-04-26"]


@pytest.mark.parametrize("mutation", ["expiry", "lot_size", "response_sha256"])
def test_bad_provider_evidence_is_rejected(tmp_path: Path, mutation: str) -> None:
    evidence = evidence_file(tmp_path, "2025-10-28")
    data = json.loads(evidence.read_text())
    item = data["queried_expiries"][0]
    if mutation == "expiry":
        item["contracts"][0]["expiry"] = "2025-10-29"
    elif mutation == "lot_size":
        item["contracts"][0]["lot_size"] = 0
    else:
        item["response_sha256"] = "unverified"
    evidence.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_evidence(evidence)
