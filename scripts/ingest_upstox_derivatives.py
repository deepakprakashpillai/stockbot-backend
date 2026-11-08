#!/usr/bin/env python3
"""
Upstox Derivatives Ingestion Pipeline for StockBOT.

Fetches and structures:
  1. Historical NIFTY Futures 5-minute candles (OHLC + Volume for VWAP)
  2. Historical India VIX 5-minute candles
  3. Historical NIFTY Weekly Options 5-minute candles (CE & PE around ATM)
  4. Contract metadata (expiries, strikes, lot sizes, tick sizes)

Outputs structured Parquet files and SQLite cache into StockBOT storage.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.market_data_time import completed_candle_timestamp  # noqa: E402
from app.infrastructure.datasets.derivatives_bundle import build_derivatives_bundle  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("upstox_ingestion")

DEFAULT_TOKEN_FILE = Path(
    os.environ.get(
        "UPSTOX_TOKEN_FILE", BACKEND_ROOT.parent.parent / "stock-analyzer" / "token.json"
    )
)
DEFAULT_SPOT_CSV = BACKEND_ROOT.parent / "nifty50_5min_2025-07-19_to_2026-07-19.csv"
DEFAULT_OUTPUT_DIR = BACKEND_ROOT / "data" / "datasets" / "upstox_nifty_derivatives"
DEFAULT_CACHE_FILE = BACKEND_ROOT / "data" / ".upstox_cache.sqlite"

NIFTY_SPOT_KEY = "NSE_INDEX|Nifty 50"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"

EXPIRED_EXPIRIES_URL = "https://api.upstox.com/v2/expired-instruments/expiries"
EXPIRED_OPTION_CONTRACT_URL = "https://api.upstox.com/v2/expired-instruments/option/contract"
EXPIRED_FUTURE_CONTRACT_URL = "https://api.upstox.com/v2/expired-instruments/future/contract"
EXPIRED_HISTORICAL_CANDLE_URL = "https://api.upstox.com/v2/expired-instruments/historical-candle"
V3_HISTORICAL_CANDLE_URL = "https://api.upstox.com/v3/historical-candle"


class SQLiteCache:
    """Thread-safe SQLite persistent cache to prevent re-fetching and save API quota."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT)")
        self.conn.commit()
        self.lock = threading.Lock()

    def _hash(self, url: str, params: dict[str, Any] | None) -> str:
        key_str = f"{url}?{json.dumps(params or {}, sort_keys=True)}"
        return hashlib.sha256(key_str.encode()).hexdigest()

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        key = self._hash(url, params)
        with self.lock:
            cur = self.conn.execute("SELECT data FROM cache WHERE key = ?", (key,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def set(self, url: str, data: dict[str, Any], params: dict[str, Any] | None = None) -> None:
        key = self._hash(url, params)
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache (key, data) VALUES (?, ?)",
                (key, json.dumps(data)),
            )
            self.conn.commit()


class RateLimiter:
    """Enforces max requests per second across threads."""

    def __init__(self, requests_per_second: float = 3.0) -> None:
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_request_time = time.monotonic()


class UpstoxClient:
    """Handles authenticated requests to Upstox API with rate limiting and caching."""

    def __init__(self, token_path: Path, cache: SQLiteCache, rate_limiter: RateLimiter) -> None:
        self.token_path = token_path
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.session = httpx.Client()
        self._load_token()

    def _load_token(self) -> None:
        if not self.token_path.exists():
            raise FileNotFoundError(f"Token file not found: {self.token_path}")
        with open(self.token_path, encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("access_token")
        if not token:
            raise ValueError(f"No access_token found in {self.token_path}")
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )

    def get(
        self, url: str, params: dict[str, Any] | None = None, use_cache: bool = True
    ) -> dict[str, Any]:
        if use_cache:
            cached = self.cache.get(url, params)
            if cached is not None:
                return cached

        max_retries = 3
        for attempt in range(max_retries):
            self.rate_limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=30.0)
                if resp.status_code == 429:
                    logger.warning("Rate limit hit (429), sleeping 2s before retry...")
                    time.sleep(2.0)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if use_cache and data.get("status") == "success":
                    self.cache.set(url, data, params)
                return data
            except httpx.HTTPError as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed request to {url}: {e}")
                    raise
                time.sleep(1.0)
        return {}


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Return calendar-month chunks from start through end, inclusive."""
    chunks: list[tuple[date, date]] = []
    current = start
    while current <= end:
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)
        chunk_end = min(next_month - timedelta(days=1), end)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


def futures_expiry_candidates(expiries: list[date], start_date: date, end_date: date) -> list[date]:
    """Return possible monthly futures expiries needed to cover the selected range.

    The catalogue also contains weekly option expiries. Asking the futures
    endpoint about those dates is harmless, while including the first monthly
    future after ``end_date`` is essential for VWAP during the final rollover.
    """
    return sorted(
        {
            expiry
            for expiry in expiries
            if start_date <= expiry <= end_date + timedelta(days=40) and expiry.day >= 20
        }
    )


def load_spot_atm_candidates(spot_csv_path: Path) -> dict[date, set[int]]:
    """Return every half-up ATM reached by completed spot candles, per session.

    Each weekly expiry must include contracts for the full week of possible
    document-defined entries, not just the last daily close before expiry.
    """
    if not spot_csv_path.exists():
        logger.warning(f"Spot CSV not found at {spot_csv_path}, will use dynamic ATM calculations.")
        return {}

    candidates: dict[date, set[int]] = {}
    with open(spot_csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamp = datetime.fromisoformat(row["timestamp"])
            atm = int(
                (Decimal(row["close"]) / Decimal("50")).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
                * Decimal("50")
            )
            candidates.setdefault(timestamp.date(), set()).add(atm)
    return candidates


def save_candles_to_parquet(candles: list[list[Any]], output_path: Path) -> None:
    """Save 5-min candles to a standardized Parquet file."""
    if not candles:
        return

    timestamps: list[str] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[int] = []
    ois: list[int] = []

    for c in candles:
        source_timestamp = datetime.fromisoformat(str(c[0]))
        timestamps.append(
            completed_candle_timestamp(
                source_timestamp, source_semantics="interval_start", interval_minutes=5
            ).isoformat()
        )
        opens.append(float(c[1]))
        highs.append(float(c[2]))
        lows.append(float(c[3]))
        closes.append(float(c[4]))
        volumes.append(int(c[5]) if len(c) > 5 else 0)
        ois.append(int(c[6]) if len(c) > 6 else 0)

    table = pa.Table.from_arrays(
        [
            pa.array(timestamps, pa.string()),
            pa.array(opens, pa.float64()),
            pa.array(highs, pa.float64()),
            pa.array(lows, pa.float64()),
            pa.array(closes, pa.float64()),
            pa.array(volumes, pa.int64()),
            pa.array(ois, pa.int64()),
        ],
        names=["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path, compression="snappy")


def run_ingestion(
    start_date: date,
    end_date: date,
    strike_range: int = 500,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    token_path: Path = DEFAULT_TOKEN_FILE,
    cache_path: Path = DEFAULT_CACHE_FILE,
    spot_csv_path: Path = DEFAULT_SPOT_CSV,
    max_workers: int = 4,
) -> None:
    logger.info(f"Starting Upstox Derivatives Ingestion for {start_date} to {end_date}")
    output_dir.mkdir(parents=True, exist_ok=True)

    cache = SQLiteCache(cache_path)
    limiter = RateLimiter(requests_per_second=3.5)
    client = UpstoxClient(token_path, cache, limiter)

    spot_atm_candidates = load_spot_atm_candidates(spot_csv_path)

    # 1. Fetch historical weekly expiries
    logger.info("Fetching expired weekly expiries from Upstox...")
    expiries_res = client.get(EXPIRED_EXPIRIES_URL, {"instrument_key": NIFTY_SPOT_KEY})
    all_expiries = [datetime.strptime(d, "%Y-%m-%d").date() for d in expiries_res.get("data", [])]
    filtered_expiries = sorted(
        [d for d in all_expiries if start_date <= d <= (end_date + timedelta(days=7))]
    )
    logger.info(f"Found {len(filtered_expiries)} relevant weekly expiries.")

    # 2. Fetch India VIX 5-min candles (chunked by month to avoid 400 Bad Request limits)
    logger.info("Fetching India VIX 5-minute historical candles in monthly chunks...")
    all_vix_candles: list[list[Any]] = []
    # The first tradable session still needs the previous trading session's
    # final VIX close under the Bull Call Spread V1 rules.
    vix_warmup_start = start_date - timedelta(days=7)
    for chunk_start, chunk_end in month_chunks(vix_warmup_start, end_date):
        vix_url = f"{V3_HISTORICAL_CANDLE_URL}/{quote(INDIA_VIX_KEY, safe='')}/minutes/5/{chunk_end}/{chunk_start}"
        try:
            vix_res = client.get(vix_url)
            c_list = vix_res.get("data", {}).get("candles", [])
            all_vix_candles.extend(c_list)
        except Exception as e:
            logger.warning(f"Could not fetch VIX for {chunk_start} to {chunk_end}: {e}")

    if all_vix_candles:
        seen_vix = set()
        unique_vix = []
        for c in sorted(all_vix_candles, key=lambda x: str(x[0])):
            if c[0] not in seen_vix:
                seen_vix.add(c[0])
                unique_vix.append(c)
        vix_out = output_dir / "vix" / "india_vix_5min.parquet"
        save_candles_to_parquet(unique_vix, vix_out)
        logger.info(f"Saved {len(unique_vix)} India VIX candles to {vix_out}")

    # 3. Fetch NIFTY Futures Contracts & 5-min candles
    logger.info("Fetching NIFTY Futures contracts & candles for VWAP...")
    monthly_expiries = futures_expiry_candidates(all_expiries, start_date, end_date)
    all_futures_candles: list[list[Any]] = []

    for fut_exp in monthly_expiries:
        fut_res = client.get(
            EXPIRED_FUTURE_CONTRACT_URL,
            {"instrument_key": NIFTY_SPOT_KEY, "expiry_date": fut_exp.isoformat()},
        )
        fut_data = fut_res.get("data", [])
        if not fut_data:
            continue
        fut_contract = fut_data[0]
        fut_key = fut_contract["instrument_key"]

        # A future becomes active before its calendar expiry month. A 40-day
        # lookback bridges monthly rollover; the extra day preserves the
        # requested start boundary with the upstream inclusive date range.
        fut_start = max(start_date - timedelta(days=1), fut_exp - timedelta(days=40))
        fut_end = min(end_date, fut_exp)
        if fut_start > fut_end:
            continue

        c_url = f"{EXPIRED_HISTORICAL_CANDLE_URL}/{quote(fut_key, safe='')}/5minute/{fut_end}/{fut_start}"
        try:
            c_res = client.get(c_url)
            candles = c_res.get("data", {}).get("candles", [])
            all_futures_candles.extend(candles)
        except Exception as e:
            logger.warning(f"Could not fetch Futures candles for expiry {fut_exp}: {e}")

    if all_futures_candles:
        seen_ts = set()
        unique_fut = []
        for c in sorted(all_futures_candles, key=lambda x: str(x[0])):
            if c[0] not in seen_ts:
                seen_ts.add(c[0])
                unique_fut.append(c)
        fut_out = output_dir / "futures" / "nifty_futures_5min.parquet"
        save_candles_to_parquet(unique_fut, fut_out)
        logger.info(f"Saved {len(unique_fut)} NIFTY Futures candles to {fut_out}")

    # 4. Fetch Weekly Options Contracts & Candles
    logger.info("Fetching Option Contracts for each weekly expiry...")
    tasks: list[dict[str, Any]] = []
    contracts_metadata: list[dict[str, Any]] = []

    last_weekly_expiry: date | None = None
    for exp_date in filtered_expiries:
        opt_res = client.get(
            EXPIRED_OPTION_CONTRACT_URL,
            {"instrument_key": NIFTY_SPOT_KEY, "expiry_date": exp_date.isoformat()},
        )
        contracts = opt_res.get("data", [])
        if not contracts:
            continue

        weekly_contracts = [
            contract for contract in contracts if bool(contract.get("weekly", False))
        ]
        if not weekly_contracts:
            continue
        eligibility_start = (
            last_weekly_expiry + timedelta(days=1) if last_weekly_expiry else start_date
        )
        matching_atms = {
            atm
            for session_date, candidates in spot_atm_candidates.items()
            if eligibility_start <= session_date <= exp_date
            for atm in candidates
        }
        if matching_atms:
            min_strike = min(matching_atms) - strike_range
            max_strike = max(matching_atms) + strike_range
        else:
            ref_atm = int(contracts[len(contracts) // 2].get("strike_price", 24500))
            min_strike = ref_atm - strike_range
            max_strike = ref_atm + strike_range

        for contract in weekly_contracts:
            strike = int(contract["strike_price"])
            if min_strike <= strike <= max_strike:
                contracts_metadata.append(contract)
                tasks.append(
                    {
                        "contract": contract,
                        "expiry_date": exp_date,
                        "from_date": max(start_date, eligibility_start),
                        "to_date": min(end_date, exp_date),
                    }
                )
        last_weekly_expiry = exp_date

    logger.info(
        f"Identified {len(tasks)} target option contracts to fetch across {len(filtered_expiries)} expiries."
    )

    master_path = output_dir / "contracts_master.json"
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(contracts_metadata, f, indent=2)
    logger.info(f"Saved contract master metadata to {master_path}")

    # Fetch option candles in parallel
    def fetch_option_task(task_info: dict[str, Any]) -> tuple[str, int]:
        c = task_info["contract"]
        key = c["instrument_key"]
        to_d = task_info["to_date"]
        from_d = task_info["from_date"]
        url = f"{EXPIRED_HISTORICAL_CANDLE_URL}/{quote(key, safe='')}/5minute/{to_d}/{from_d}"
        res = client.get(url)
        candles = res.get("data", {}).get("candles", [])
        if candles:
            candles.sort(key=lambda x: str(x[0]))
            exp_str = task_info["expiry_date"].isoformat()
            filename = f"{int(c['strike_price'])}_{c['instrument_type']}.parquet"
            out_file = output_dir / "options" / exp_str / filename
            save_candles_to_parquet(candles, out_file)
            return (key, len(candles))
        return (key, 0)

    downloaded_count = 0
    total_candles = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_option_task, t) for t in tasks]
        for f in tqdm(as_completed(futures), total=len(futures), desc="Fetching Options Candles"):
            try:
                _, count = f.result()
                if count > 0:
                    downloaded_count += 1
                    total_candles += count
            except Exception as e:
                logger.error(f"Error fetching contract: {e}")

    manifest = build_derivatives_bundle(output_dir, spot_csv_path)
    logger.info(
        "Ingestion complete: saved %s option contracts (%s candles); bundle checksum %s",
        downloaded_count,
        total_candles,
        manifest["bundle_checksum"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest historical derivatives data from Upstox.")
    parser.add_argument(
        "--start-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2025, 7, 21),
        help="Start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2025, 8, 1),
        help="End date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--strike-range", type=int, default=500, help="Points around ATM to fetch (default: 500)"
    )
    parser.add_argument("--workers", type=int, default=4, help="Parallel worker threads")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    args = parser.parse_args()

    run_ingestion(
        start_date=args.start_date,
        end_date=args.end_date,
        strike_range=args.strike_range,
        output_dir=args.output_dir,
        token_path=args.token_file,
        max_workers=args.workers,
    )


if __name__ == "__main__":
    main()
