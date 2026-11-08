#!/usr/bin/env python3
"""Deterministic, database-free throughput and memory baseline for the demo engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domain.algorithms import PLATFORM_DEMO  # noqa: E402
from app.domain.backtests import MarketCandle, evaluate_chronologically  # noqa: E402


def candles(count: int) -> list[MarketCandle]:
    offset = timezone(timedelta(hours=5, minutes=30))
    start = datetime(2025, 1, 2, 9, 15, tzinfo=offset)
    return [
        MarketCandle(
            timestamp=start + timedelta(minutes=5 * index),
            high=20_000 + (index % 31) / 10,
            close=20_000 + (index % 23) / 10,
            futures_vwap=None,
            india_vix=None,
        )
        for index in range(count)
    ]


def run_benchmark(count: int, repetitions: int) -> dict[str, object]:
    source = candles(count)
    configured = PLATFORM_DEMO.configure({"signal_every_n_candles": 5, "hold_candles": 3})
    durations = []
    digests = []
    tracemalloc.start()
    for _ in range(repetitions):
        started = time.perf_counter()
        decisions = list(evaluate_chronologically(source, configured))
        durations.append(time.perf_counter() - started)
        encoded = "\n".join(
            f"{item.timestamp.isoformat()}|{item.decision}|{item.rejection_reason}|{item.score}"
            for item in decisions
        ).encode()
        digests.append(hashlib.sha256(encoded).hexdigest())
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if len(set(digests)) != 1:
        raise RuntimeError("Determinism check failed: repeated runs produced different decisions.")
    median_seconds = sorted(durations)[len(durations) // 2]
    return {
        "schema": "stockbot.platform-demo-benchmark.v1",
        "candles": count,
        "repetitions": repetitions,
        "median_seconds": round(median_seconds, 6),
        "candles_per_second": round(count / median_seconds, 2),
        "peak_memory_mib": round(peak_bytes / 1024 / 1024, 2),
        "decision_digest": digests[0],
        "synthetic": True,
        "unit": "DEMO_UNITS",
        "financial_pnl_available": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=int, default=18_000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--minimum-candles-per-second", type=float, default=10_000)
    parser.add_argument("--maximum-peak-memory-mib", type=float, default=128)
    args = parser.parse_args()
    if args.candles < 1 or args.repetitions < 1:
        raise SystemExit("--candles and --repetitions must be positive")
    result = run_benchmark(args.candles, args.repetitions)
    result["targets"] = {
        "minimum_candles_per_second": args.minimum_candles_per_second,
        "maximum_peak_memory_mib": args.maximum_peak_memory_mib,
    }
    passed = bool(
        result["candles_per_second"] >= args.minimum_candles_per_second
        and result["peak_memory_mib"] <= args.maximum_peak_memory_mib
    )
    result["passed"] = passed
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
