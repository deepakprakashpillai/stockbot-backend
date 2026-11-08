from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.infrastructure.datasets.derivatives_bundle import build_derivatives_bundle

IST = timezone(timedelta(hours=5, minutes=30))


def market_bundle(root: Path, *, missing: datetime | None = None) -> Path:
    previous, current = date(2025, 7, 18), date(2025, 7, 21)
    timestamps = [
        datetime.combine(day, time(9, 20), IST) + timedelta(minutes=5 * i)
        for day in (previous, current)
        for i in range(75)
    ]
    if missing:
        timestamps.remove(missing)
    spot = root / "spot/nifty_spot_5min.csv"
    spot.parent.mkdir(parents=True)
    with spot.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
        )
        writer.writeheader()
        for timestamp in timestamps:
            close = 24900 if timestamp.date() == previous else 25000
            writer.writerow(
                dict(
                    timestamp=timestamp.isoformat(),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=100,
                    open_interest=100,
                )
            )

    def parquet(path: Path, prices: list[float]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                dict(
                    timestamp=[t.isoformat() for t in timestamps],
                    open=prices,
                    high=prices,
                    low=prices,
                    close=prices,
                    volume=[100] * len(prices),
                    open_interest=[100] * len(prices),
                )
            ),
            path,
        )

    parquet(root / "futures/nifty_futures_5min.parquet", [24900.0] * len(timestamps))
    parquet(
        root / "vix/india_vix_5min.parquet",
        [20.0 if t.date() == previous else 19.0 for t in timestamps],
    )
    contracts = []
    for strike, close in ((25000, 100.0), (25200, 40.0), (25250, 30.0)):
        contracts.append(
            dict(
                expiry="2025-07-24",
                strike_price=strike,
                instrument_type="CE",
                trading_symbol=f"TEST_{strike}_CE",
                lot_size=75,
                tick_size=0.05,
                weekly=True,
            )
        )
        parquet(root / f"options/2025-07-24/{strike}_CE.parquet", [close] * len(timestamps))
    (root / "contracts_master.json").write_text(json.dumps(contracts))
    build_derivatives_bundle(root, spot, source_semantics="interval_end")
    return root
