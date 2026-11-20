"""Build an operator-owned v3 calendar and honest provider-coverage audit.

No API calls, credentials or shared-calendar changes. Availability never removes a
scheduled cycle. A confirmed expiry only means matching CE/PE metadata exists;
the bundle validator and strategy preflight still check strikes and candles.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SOURCE_FILE = Path(__file__).with_name("model2_calendar_sources_2023_2026.v1.json")
EVIDENCE_SCHEMA = "stockbot.model2-expiry-evidence.v1"


def days(start: date, end: date) -> Iterator[date]:
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_evidence(path: Path | None) -> tuple[dict[str, dict[str, Any]], str]:
    if path is None:
        return {}, "No provider evidence supplied; every expiry remains UNQUERIED."
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("Unsupported expiry evidence schema.")
    reference = payload.get("source_reference")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("Expiry evidence requires a source_reference.")
    result = {}
    for item in payload["queried_expiries"]:
        key = item["requested_expiry"]
        date.fromisoformat(key)
        if key in result or item["status"] not in {"AVAILABLE", "EMPTY", "ERROR"}:
            raise ValueError("Duplicate expiry or invalid provider status.")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", item.get("response_sha256", "")):
            raise ValueError("Each provider response requires its SHA-256.")
        contracts = item.get("contracts", [])
        if item["status"] != "AVAILABLE" and contracts:
            raise ValueError("EMPTY/ERROR responses cannot declare contracts.")
        identities = set()
        for contract in contracts:
            if contract["expiry"] != key or contract["instrument_type"] not in {"CE", "PE"}:
                raise ValueError("Provider metadata has a mismatched expiry or option type.")
            identity = contract["instrument_key"]
            if not isinstance(identity, str) or not identity or identity in identities:
                raise ValueError("Contract identities must be nonempty and unique per expiry.")
            identities.add(identity)
            lot = contract["lot_size"]
            if isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
                raise ValueError("Contract lot_size must be a positive integer.")
            for field in ("strike_price", "tick_size"):
                try:
                    number = Decimal(str(contract[field]))
                except (InvalidOperation, KeyError) as exc:
                    raise ValueError(f"Invalid contract {field}.") from exc
                if not number.is_finite() or number <= 0:
                    raise ValueError(f"Invalid contract {field}.")
        result[key] = item
    return result, reference


def read_observations(path: Path, start: date, end: date) -> dict[date, set[str]]:
    observations: dict[date, set[str]] = defaultdict(set)
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp.utcoffset() != timedelta(hours=5, minutes=30):
                raise ValueError("Spot timestamps must explicitly use exchange offset +05:30.")
            if not start <= timestamp.date() <= end:
                continue
            clock = timestamp.strftime("%H:%M:%S")
            if timestamp.microsecond or clock in observations[timestamp.date()]:
                raise ValueError("Spot timestamps must be distinct whole seconds.")
            observations[timestamp.date()].add(clock)
    return dict(observations)


def build_artifacts(
    spot_csv: Path,
    start: date,
    end: date,
    evidence_path: Path | None = None,
    source_path: Path = SOURCE_FILE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sources = json.loads(source_path.read_text())
    if sources["schema_version"] != "stockbot.model2-calendar-sources.v1":
        raise ValueError("Unsupported source calendar schema.")
    source_start = date.fromisoformat(sources["coverage_start"])
    source_end = date.fromisoformat(sources["coverage_end"])
    if not source_start <= start <= end <= source_end:
        raise ValueError("Requested dates are outside the qualified source calendar.")
    for record in [
        *sources["holiday_groups"],
        *sources["exceptional_sessions"],
        *sources["expiry_regimes"],
    ]:
        if not record["source_ids"] or any(
            key not in sources["sources"] for key in record["source_ids"]
        ):
            raise ValueError("Every calendar fact requires known official sources.")
    holidays = {
        date.fromisoformat(value) for group in sources["holiday_groups"] for value in group["dates"]
    }
    exceptional = {
        date.fromisoformat(item["session_date"]): item for item in sources["exceptional_sessions"]
    }
    expected = {
        day
        for day in days(source_start, source_end)
        if (day.weekday() < 5 and day not in holidays) or day in exceptional
    }
    earlier = [day for day in expected if day < start]
    # One sourced context session makes the first expiry explicit, even when its
    # entry predates the requested data. It does not assert that context has bars.
    coverage_start = max(earlier) if earlier else start
    session_days = sorted(day for day in expected if coverage_start <= day <= end)
    sessions = []
    for day in session_days:
        detail = exceptional.get(day, {"kind": "NORMAL", **sources["normal_session"]})
        sessions.append(
            {
                "session_date": str(day),
                **{key: detail[key] for key in ("kind", "opens_at", "closes_at")},
            }
        )

    expiries = set()
    for regime in sources["expiry_regimes"]:
        regime_start = date.fromisoformat(regime["start"])
        regime_end = date.fromisoformat(regime["end"])
        for nominal in days(max(source_start, regime_start), min(source_end, regime_end)):
            if nominal.weekday() != regime["weekday"]:
                continue
            expiry = nominal
            # A Muhurat session on a declared regular holiday must not restore
            # that nominal expiry (e.g. 21 Oct 2025). Earlier actual sessions do
            # count, including announced weekend openings, when shifting back.
            if nominal in holidays:
                expiry -= timedelta(days=1)
                while expiry >= source_start and expiry not in expected:
                    expiry -= timedelta(days=1)
            if start <= expiry <= end:
                if expiry not in expected or not any(day < expiry for day in session_days):
                    raise ValueError(f"Expiry {expiry} lacks a sourced session/previous session.")
                expiries.add(expiry)

    observations = read_observations(spot_csv, start, end)
    required_dates = {day for day in expected if start <= day <= end}
    missing = sorted(required_dates - observations.keys())
    unexpected = sorted(observations.keys() - required_dates)
    evidence, evidence_reference = load_evidence(evidence_path)
    expiry_report = []
    for expiry in sorted(expiries):
        item = evidence.get(str(expiry))
        contracts = item.get("contracts", []) if item else []
        types = {contract["instrument_type"] for contract in contracts}
        state = item["status"] if item else "UNQUERIED"
        if state == "AVAILABLE":
            state = "CONFIRMED_CE_PE" if types == {"CE", "PE"} else "INSUFFICIENT_CONTRACT_TYPES"
        entry = max(day for day in session_days if day < expiry)
        classification = "NORMAL_CYCLE"
        if entry < start:
            classification = "OUTSIDE_COMPLETE_CYCLE_RANGE"
        elif entry in exceptional or expiry in exceptional:
            classification = "SPECIAL_SESSION"
        expiry_report.append(
            {
                "expiry": str(expiry),
                "entry_session": str(entry),
                "calendar_classification": classification,
                "provider_status": state,
                "contract_count": len(contracts),
                "response_sha256": item["response_sha256"] if item else None,
            }
        )

    normal_grid = {
        (datetime(2000, 1, 1, 9, 15) + timedelta(minutes=5 * offset)).strftime("%H:%M:%S")
        for offset in range(75)
    }
    normal_bar_findings = []
    for day, clocks in sorted(observations.items()):
        if day in expected and day not in exceptional:
            missing_clocks = sorted(normal_grid - clocks)
            extra_clocks = sorted(clocks - normal_grid)
            if missing_clocks or extra_clocks:
                normal_bar_findings.append(
                    {
                        "session_date": str(day),
                        "missing_scheduled_opens": missing_clocks,
                        "out_of_grid_opens": extra_clocks,
                    }
                )

    calendar = {
        "schema_version": "stockbot.model2-calendar.v1",
        "version": f"{sources['version']}:{start}:{end}",
        "coverage_start": str(coverage_start),
        "coverage_end": str(end),
        "expiry_reference": f"Official NSE sourced schedule; {sources['sources']['FAOP68747']}; {sources['sources']['FAOP67338']}; holiday references and per-date metadata checks in calendar_qualification.json. Availability does not remove cycles.",
        "sessions": sessions,
        "weekly_expiries": [str(day) for day in sorted(expiries)],
    }
    audit = {
        "schema_version": "stockbot.model2-calendar-qualification.v1",
        "qualification": "QUALIFIED_SESSIONS"
        if not missing and not unexpected
        else "SESSION_RECONCILIATION_FAILED",
        "requested_start": str(start),
        "requested_end": str(end),
        "qualification_limits": sources["qualification"],
        "spot_source_sha256": digest(spot_csv),
        "calendar_sources_sha256": digest(source_path),
        "expiry_evidence_sha256": digest(evidence_path) if evidence_path else None,
        "expiry_evidence_reference": evidence_reference,
        "context_sessions_without_asserted_spot_coverage": [
            str(day) for day in session_days if day < start
        ],
        "expected_source_sessions": len(required_dates),
        "observed_source_sessions": len(observations),
        "missing_source_session_dates": [str(day) for day in missing],
        "unexpected_source_session_dates": [str(day) for day in unexpected],
        "normal_session_bar_findings": normal_bar_findings,
        "provider_status_counts": dict(Counter(row["provider_status"] for row in expiry_report)),
        "scheduled_cycles": expiry_report,
        "provider_expiries_outside_sourced_schedule": sorted(
            set(evidence) - {str(day) for day in expiries}
        ),
        "official_sources": sources["sources"],
        "exceptional_session_evidence": [
            item
            for item in sources["exceptional_sessions"]
            if start <= date.fromisoformat(item["session_date"]) <= end
        ],
        "holiday_evidence": sources["holiday_groups"],
        "expiry_regimes": sources["expiry_regimes"],
    }
    return calendar, audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spot-csv", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--expiry-evidence", type=Path)
    parser.add_argument("--sources", type=Path, default=SOURCE_FILE)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    calendar, audit = build_artifacts(
        args.spot_csv, args.start_date, args.end_date, args.expiry_evidence, args.sources
    )
    # New directory only: do not overwrite immutable research inputs.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "calendar_qualification.json").write_text(json.dumps(audit, indent=2) + "\n")
    if audit["qualification"] != "QUALIFIED_SESSIONS":
        raise SystemExit("Session reconciliation failed; audit written, calendar not published.")
    (args.output_dir / "calendar.json").write_text(json.dumps(calendar, indent=2) + "\n")
    print(
        json.dumps(
            {
                "qualification": audit["qualification"],
                "sessions": audit["observed_source_sessions"],
                "scheduled_cycles": len(audit["scheduled_cycles"]),
                "provider_status_counts": audit["provider_status_counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
