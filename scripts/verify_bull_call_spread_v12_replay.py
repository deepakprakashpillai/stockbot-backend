#!/usr/bin/env python3
"""Run the corrected full-period Bull Call Spread backtest twice and compare bytes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.application.backtest_coverage import analyze_derivatives_coverage  # noqa: E402
from app.domain.algorithms import BULL_CALL_SPREAD, BullCallSpreadHardGateParameters  # noqa: E402
from app.domain.backtests import MarketCandle, evaluate_chronologically  # noqa: E402
from app.domain.derivatives_provider import ParquetDerivativesProvider  # noqa: E402
from app.domain.option_spread_execution import (  # noqa: E402
    OptionSpreadAssumptions,
    simulate_option_spread_backtest,
)


def _read_spot(path: Path, start: date, end: date) -> list[MarketCandle]:
    candles: list[MarketCandle] = []
    with path.open("r", encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            timestamp = datetime.fromisoformat(row["timestamp"])
            if start <= timestamp.date() <= end:
                candles.append(
                    MarketCandle(
                        timestamp=timestamp,
                        high=float(row["high"]),
                        close=float(row["close"]),
                        futures_vwap=None,
                        india_vix=None,
                        previous_india_vix=None,
                    )
                )
    return candles


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _run_once(
    bundle_dir: Path,
    start_date: date,
    end_date: date,
    included_sessions: set[date],
    parameters: BullCallSpreadHardGateParameters,
) -> tuple[dict[str, Any], bytes]:
    provider = ParquetDerivativesProvider(bundle_dir)
    spot_path = bundle_dir / "spot" / "nifty_spot_5min.csv"
    all_spot = provider.enrich_spot_candles(
        _read_spot(spot_path, start_date - timedelta(days=10), end_date)
    )
    configured = BULL_CALL_SPREAD.configure(parameters.model_dump(mode="python"))
    decisions = [
        item
        for item in evaluate_chronologically(all_spot, configured)
        if start_date <= item.session_date <= end_date and item.session_date in included_sessions
    ]
    requested_spot = [
        item
        for item in all_spot
        if start_date <= item.timestamp.date() <= end_date
        and item.timestamp.date() in included_sessions
    ]
    execution = simulate_option_spread_backtest(
        signals=decisions,
        derivatives_provider=provider,
        spot_candles=requested_spot,
        assumptions=OptionSpreadAssumptions.from_parameters(
            parameters, starting_capital=Decimal("500000.00")
        ),
        strike_width=parameters.primary_spread_width,
    )
    payload = {
        "algorithm_code_version": BULL_CALL_SPREAD.code_version,
        "decision_count": len(decisions),
        "accepted_signal_count": sum(item.decision == "ACCEPTED" for item in decisions),
        "pdh_unavailable_count": sum(
            item.rejection_reason == "PDH_UNAVAILABLE" for item in decisions
        ),
        "metrics": execution.metrics(Decimal("500000.00")),
        "trades": [asdict(item) for item in execution.trades],
        "execution_rejections": [asdict(item) for item in execution.rejections],
    }
    encoded = _canonical(payload)
    return payload, encoded


def verify(bundle_dir: Path, start_date: date, end_date: date) -> dict[str, Any]:
    bundle_dir = bundle_dir.resolve()
    parameters = BullCallSpreadHardGateParameters()
    coverage = analyze_derivatives_coverage(
        bundle_dir=bundle_dir,
        spot_path=bundle_dir / "spot" / "nifty_spot_5min.csv",
        start_date=start_date,
        end_date=end_date,
        parameters=parameters,
    )
    included_sessions = {date.fromisoformat(value) for value in coverage["included_sessions"]}
    first, first_bytes = _run_once(bundle_dir, start_date, end_date, included_sessions, parameters)
    second, second_bytes = _run_once(
        bundle_dir, start_date, end_date, included_sessions, parameters
    )
    first_digest = hashlib.sha256(first_bytes).hexdigest()
    second_digest = hashlib.sha256(second_bytes).hexdigest()
    if first_bytes != second_bytes:
        raise RuntimeError("The two corrected full-period replays produced different results.")
    return {
        "algorithm_code_version": BULL_CALL_SPREAD.code_version,
        "bundle_dir": bundle_dir.as_posix(),
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "coverage_status": coverage["coverage_status"],
        "included_session_count": coverage["included_session_count"],
        "excluded_session_count": coverage["excluded_session_count"],
        "coverage_digest": coverage["coverage_digest"],
        "first_replay_digest": first_digest,
        "second_replay_digest": second_digest,
        "identical": True,
        "result": first,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=BACKEND_ROOT / "data" / "datasets" / "upstox_nifty_derivatives_v6",
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 7, 21))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 7, 17))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(args.bundle_dir, args.start_date, args.end_date)
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(
            json.dumps(
                {key: value for key, value in report.items() if key not in {"result"}},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(rendered)


if __name__ == "__main__":
    main()
