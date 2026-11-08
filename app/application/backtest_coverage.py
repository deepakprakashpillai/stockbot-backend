"""Deterministic whole-session coverage analysis for option-spread runs."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.domain.algorithms import (
    BULL_CALL_SPREAD,
    AlgorithmDefinition,
    BullCallSpreadSignalParameters,
)
from app.domain.backtests import MarketCandle, evaluate_chronologically
from app.domain.trading_calendar import (
    NSE_FO_CALENDAR,
    NSE_FO_CALENDAR_SOURCES,
    NSE_FO_CALENDAR_VERSION,
)
from app.infrastructure.datasets.derivatives_bundle import (
    BUNDLE_SCHEMA_VERSION,
    load_derivatives_bundle,
    verify_derivatives_bundle_files,
)
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider


def _read_spot(path: Path, start: date, end: date) -> list[MarketCandle]:
    values: list[MarketCandle] = []
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            timestamp = datetime.fromisoformat(row["timestamp"])
            if start <= timestamp.date() <= end:
                values.append(
                    MarketCandle(
                        timestamp=timestamp,
                        high=float(row["high"]),
                        close=float(row["close"]),
                        futures_vwap=None,
                        india_vix=None,
                        previous_india_vix=None,
                    )
                )
    return sorted(values, key=lambda item: item.timestamp)


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def analyze_derivatives_coverage(
    *,
    bundle_dir: Path,
    spot_path: Path,
    start_date: date,
    end_date: date,
    parameters: BullCallSpreadSignalParameters,
    algorithm: AlgorithmDefinition = BULL_CALL_SPREAD,
    dataset_version_id: str | None = None,
    algorithm_version_id: str | None = None,
) -> dict[str, Any]:
    manifest = load_derivatives_bundle(bundle_dir)
    verify_derivatives_bundle_files(bundle_dir, manifest)
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            "This dataset predates explicit completed-candle timestamp semantics. "
            "Build a corrected immutable bundle before starting a new run."
        )

    spot = _read_spot(spot_path, start_date - timedelta(days=10), end_date)
    requested_sessions = NSE_FO_CALENDAR.sessions(start_date, end_date)
    all_sessions = {item.timestamp.date() for item in spot}
    derivatives = ParquetDerivativesProvider(bundle_dir)
    enriched = derivatives.enrich_spot_candles(spot)
    configured_algorithm = algorithm.configure(parameters.model_dump(mode="python"))
    accepted_entry_times: dict[date, set[datetime]] = {}
    for decision in evaluate_chronologically(enriched, configured_algorithm):
        if decision.decision == "ACCEPTED":
            accepted_entry_times.setdefault(decision.session_date, set()).add(decision.timestamp)
    by_session: dict[date, list[MarketCandle]] = {}
    for candle in enriched:
        by_session.setdefault(candle.timestamp.date(), []).append(candle)

    exclusions: list[dict[str, Any]] = []
    included: list[str] = []
    for session_date in requested_sessions:
        candles = sorted(by_session.get(session_date, []), key=lambda item: item.timestamp)
        if not candles:
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_DATA_GAP",
                    "reason_code": "SPOT_SESSION_MISSING",
                    "details": {"calendar_version": NSE_FO_CALENDAR_VERSION},
                }
            )
            continue
        previous_session = NSE_FO_CALENDAR.previous_session(session_date)
        if previous_session not in all_sessions:
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_WARMUP_GAP",
                    "reason_code": "PREVIOUS_TRADING_SESSION_UNAVAILABLE",
                    "details": {"required_previous_session": previous_session.isoformat()},
                }
            )
            continue
        try:
            for checked_date in (previous_session, session_date):
                expected = set(NSE_FO_CALENDAR.completed_candle_times(checked_date))
                observed_clock = {item.timestamp for item in by_session.get(checked_date, [])}
                missing_clock = sorted(expected - observed_clock)
                if missing_clock:
                    raise ValueError(
                        json.dumps(
                            {
                                "checked_session": checked_date.isoformat(),
                                "missing_count": len(missing_clock),
                                "missing_timestamps": [
                                    item.isoformat() for item in missing_clock[:10]
                                ],
                            }
                        )
                    )
        except ValueError as exc:
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_DATA_GAP",
                    "reason_code": "SESSION_CLOCK_INCOMPLETE",
                    "details": {"reason": str(exc)},
                }
            )
            continue
        if derivatives.vix_close(previous_session) is None:
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_DATA_GAP",
                    "reason_code": "PREVIOUS_SESSION_VIX_MISSING",
                    "details": {"required_previous_session": previous_session.isoformat()},
                }
            )
            continue
        previous_close_time = NSE_FO_CALENDAR.completed_candle_times(previous_session)[-1]
        if previous_close_time not in derivatives.vix_timestamps(previous_session):
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_DATA_GAP",
                    "reason_code": "PREVIOUS_SESSION_VIX_CLOSE_MISSING",
                    "details": {"required_timestamp": previous_close_time.isoformat()},
                }
            )
            continue
        missing_inputs = [
            candle.timestamp.isoformat()
            for candle in candles
            if candle.futures_vwap is None
            or candle.india_vix is None
            or candle.previous_india_vix is None
        ]
        if missing_inputs:
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_DATA_GAP",
                    "reason_code": "SYNCHRONIZED_MARKET_INPUT_MISSING",
                    "details": {
                        "timestamps": missing_inputs[:10],
                        "missing_count": len(missing_inputs),
                    },
                }
            )
            continue
        try:
            session = derivatives.load_session(
                session_date,
                candles,
                allow_same_day=parameters.same_day_weekly_expiry,
            )
            # Coverage follows the real signal evaluator. Rejected candles cannot
            # lead to an option lookup, so requiring contracts for every candle in
            # an entry window would exclude otherwise usable sessions.
            qualifying_times = accepted_entry_times.get(session_date, set())
            relevant = [candle for candle in candles if candle.timestamp in qualifying_times]
            required_widths = [parameters.primary_spread_width]
            if parameters.fallback_enabled:
                required_widths.append(parameters.fallback_spread_width)
            gap: dict[str, Any] | None = None
            for candle in relevant:
                atm = int(
                    (Decimal(str(candle.close)) / Decimal(parameters.atm_increment)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                    * Decimal(parameters.atm_increment)
                )
                strikes = [atm, *(atm + width for width in required_widths)]
                monitoring_times = [
                    value.timestamp
                    for value in candles
                    if value.timestamp >= candle.timestamp
                    and value.timestamp.time() <= parameters.hard_exit_time
                    and (
                        int((value.timestamp - candle.timestamp).total_seconds() // 60)
                        % parameters.monitoring_interval_minutes
                        == 0
                        or value.timestamp.time() == parameters.hard_exit_time
                    )
                ]
                for strike in strikes:
                    contract = session.get_option_contract(strike, "CE")
                    observed = session.get_option_candles(strike, "CE") if contract else {}
                    missing_times = [value for value in monitoring_times if value not in observed]
                    if contract is None or missing_times:
                        gap = {
                            "entry_timestamp": candle.timestamp.isoformat(),
                            "expiry": session.active_expiry.isoformat(),
                            "strike": strike,
                            "contract_missing": contract is None,
                            "missing_timestamps": [
                                value.isoformat() for value in missing_times[:10]
                            ],
                            "missing_count": len(missing_times),
                        }
                        break
                if gap:
                    break
            if gap:
                raise ValueError(json.dumps(gap, sort_keys=True))
        except (OSError, ValueError) as exc:
            try:
                parsed = json.loads(str(exc))
                details = parsed if isinstance(parsed, dict) else {"reason": str(exc)}
            except json.JSONDecodeError:
                details = {"reason": str(exc)}
            exclusions.append(
                {
                    "session_date": session_date.isoformat(),
                    "classification": "EXCLUDED_DATA_GAP",
                    "reason_code": "OPTION_COVERAGE_INCOMPLETE",
                    "details": details,
                }
            )
            continue
        included.append(session_date.isoformat())

    status = "COMPLETE" if not exclusions else ("PARTIAL" if included else "BLOCKED")
    coverage_payload = {
        "schema_version": "stockbot.backtest-coverage.v2",
        "dataset_version_id": dataset_version_id,
        "algorithm_version_id": algorithm_version_id,
        "algorithm_key": algorithm.key,
        "algorithm_code_version": algorithm.code_version,
        "parameters": parameters.model_dump(mode="json"),
        "bundle_checksum": manifest["bundle_checksum"],
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "policy": "EXCLUDE_WHOLE_SESSION",
        "trading_calendar_version": NSE_FO_CALENDAR_VERSION,
        "trading_calendar_sources": list(NSE_FO_CALENDAR_SOURCES),
        "included_sessions": included,
        "excluded_sessions": exclusions,
    }
    return {
        **coverage_payload,
        "coverage_status": status,
        "coverage_digest": _digest(coverage_payload),
        "requested_session_count": len(requested_sessions),
        "included_session_count": len(included),
        "excluded_session_count": len(exclusions),
        "exclusion_counts": dict(Counter(item["reason_code"] for item in exclusions)),
    }
