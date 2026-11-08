"""Measure the local V1.2 research pipeline without writing business records.

Run this against the same immutable bundle, Python environment, and machine when
comparing revisions. Times are diagnostic measurements, not a capacity promise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from app.application.backtest_coverage import analyze_derivatives_coverage
from app.domain.algorithms import BULL_CALL_SPREAD, BullCallSpreadHardGateParameters
from app.domain.backtests import SignalDecision
from app.domain.option_spread_execution import OptionSpreadAssumptions
from app.domain.research_pipeline import run_chronological_option_research
from app.infrastructure.market_data.csv_reader import CsvMarketDataProvider
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider


def benchmark(bundle: Path, start: date, end: date) -> dict[str, object]:
    spot_path = bundle / "spot" / "nifty_spot_5min.csv"
    parameters = BullCallSpreadHardGateParameters()
    timings: dict[str, float] = {}

    began = perf_counter()
    coverage = analyze_derivatives_coverage(
        bundle_dir=bundle,
        spot_path=spot_path,
        start_date=start,
        end_date=end,
        parameters=parameters,
    )
    timings["coverage_seconds"] = perf_counter() - began
    included = set(coverage["included_sessions"])

    began = perf_counter()
    provider = ParquetDerivativesProvider(bundle)
    observations = provider.iter_enriched_spot_candles(
        CsvMarketDataProvider(spot_path).stream(start - timedelta(days=7), end)
    )
    configured = BULL_CALL_SPREAD.configure(parameters.model_dump(mode="python"))
    signals: list[SignalDecision] = []
    result = run_chronological_option_research(
        observations,
        configured,
        provider,
        OptionSpreadAssumptions.from_parameters(parameters, starting_capital=Decimal("100000")),
        requested_start=start,
        requested_end=end,
        included_sessions={date.fromisoformat(value) for value in included},
        on_signal=signals.append,
    )
    timings["research_seconds"] = perf_counter() - began

    evidence = {"signals": [asdict(signal) for signal in signals], "result": asdict(result)}
    fingerprint = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_mb = max_rss / (1024 * 1024 if platform.system() == "Darwin" else 1024)
    return {
        "bundle": str(bundle),
        "range": [start.isoformat(), end.isoformat()],
        "fingerprint": fingerprint,
        "included_sessions": len(included),
        "signals": len(signals),
        "accepted_signals": sum(signal.decision == "ACCEPTED" for signal in signals),
        "trades": len(result.trades),
        "rejections": len(result.rejections),
        "peak_rss_mb": round(peak_rss_mb, 1),
        "seconds": {name: round(value, 3) for name, value in timings.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle", type=Path, default=Path("data/datasets/upstox_nifty_derivatives_v6")
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 7, 21))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 7, 17))
    args = parser.parse_args()
    print(json.dumps(benchmark(args.bundle, args.start, args.end), indent=2))


if __name__ == "__main__":
    main()
