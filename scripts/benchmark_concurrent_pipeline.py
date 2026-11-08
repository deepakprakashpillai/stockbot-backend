"""Measure local pure-research concurrency without claiming worker/VPS capacity."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from time import monotonic


def measure(count: int) -> dict[str, object]:
    def run_one() -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.benchmark_bull_call_spread_pipeline"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    start = monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(lambda _: run_one(), range(count)))
    fingerprints = {str(result["fingerprint"]) for result in results}
    if len(fingerprints) != 1:
        raise ValueError("Concurrent runs produced different research fingerprints.")
    return {
        "runs": count,
        "wall_seconds": round(monotonic() - start, 3),
        "fingerprint": fingerprints.pop(),
        "child_peak_rss_mb": [result["peak_rss_mb"] for result in results],
        "sum_child_peak_rss_mb": round(sum(float(result["peak_rss_mb"]) for result in results), 1),
    }


if __name__ == "__main__":
    print(json.dumps({"results": [measure(count) for count in (1, 2, 4)]}, indent=2))
