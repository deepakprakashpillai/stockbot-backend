from __future__ import annotations

import base64
import csv
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.domain.dataset_validation import REQUIRED_COLUMNS

PreviewStatus = Literal["committable", "blocked"]
Granularity = Literal["5m", "15m", "30m", "1h", "1d"]
GRANULARITY_MINUTES: dict[Granularity, int] = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "1d": 24 * 60,
}


def preview_csv(path: Path, *, sample_limit: int = 20) -> dict[str, Any]:
    """Read only a bounded sample. The validation pipeline remains authoritative."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            headers = reader.fieldnames or []
            sample = [
                {key: value or "" for key, value in row.items()}
                for _, row in zip(range(sample_limit), reader, strict=False)
            ]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return {
            "status": "blocked",
            "headers": [],
            "sample_rows": [],
            "issues": [
                {
                    "code": "PREVIEW_MALFORMED_CSV",
                    "message": "The CSV preview could not be read.",
                    "details": {"reason": str(exc)},
                }
            ],
        }
    issues: list[dict[str, Any]] = []
    if headers != REQUIRED_COLUMNS:
        issues.append(
            {
                "code": "PREVIEW_UNSUPPORTED_SCHEMA",
                "message": "CSV columns must match the NIFTY candle schema exactly.",
                "details": {"expected": REQUIRED_COLUMNS, "received": headers},
            }
        )
    return {
        "status": "blocked" if issues else "committable",
        "headers": headers,
        "sample_rows": sample,
        "issues": issues,
    }


def profile_csv(path: Path) -> dict[str, Any]:
    """Build small profile evidence without retaining raw rows in PostgreSQL."""
    preview = preview_csv(path)
    if preview["status"] == "blocked":
        return {"columns": [], "row_count_sampled": 0, "available": False}
    counts = {column: 0 for column in REQUIRED_COLUMNS}
    minimums: dict[str, str] = {}
    maximums: dict[str, str] = {}
    rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        for row in reader:
            rows += 1
            for column in REQUIRED_COLUMNS:
                value = row.get(column, "")
                if value not in (None, ""):
                    counts[column] += 1
            for column in REQUIRED_COLUMNS[1:]:
                try:
                    value = Decimal(row[column])
                except Exception:
                    continue
                if not value.is_finite():
                    continue
                minimums[column] = str(min(value, Decimal(minimums.get(column, value))))
                maximums[column] = str(max(value, Decimal(maximums.get(column, value))))
    return {
        "available": True,
        "row_count_sampled": rows,
        "columns": [
            {
                "name": column,
                "type": "timestamp" if column == "timestamp" else "decimal",
                "non_empty_count": counts[column],
                "minimum": minimums.get(column),
                "maximum": maximums.get(column),
            }
            for column in REQUIRED_COLUMNS
        ],
    }


def _bucket(timestamp: datetime, granularity: Granularity) -> datetime:
    if granularity == "1d":
        return timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = GRANULARITY_MINUTES[granularity]
    minute = timestamp.minute - timestamp.minute % minutes
    return timestamp.replace(minute=minute, second=0, microsecond=0)


def _encode_cursor(timestamp: datetime) -> str:
    return base64.urlsafe_b64encode(json.dumps({"last": timestamp.isoformat()}).encode()).decode()


def _decode_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(
            json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())["last"]
        )
    except Exception as exc:
        raise ValueError("The candle cursor is invalid.") from exc


def read_candles(
    path: Path,
    *,
    start: datetime,
    end: datetime,
    granularity: Granularity,
    cursor: str | None,
    limit: int,
) -> tuple[list[dict[str, str]], str | None, bool]:
    after = _decode_cursor(cursor)
    grouped: dict[datetime, list[dict[str, str]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        for row in reader:
            timestamp = datetime.fromisoformat(row["timestamp"])
            if start <= timestamp <= end:
                grouped[_bucket(timestamp, granularity)].append(row)
    values: list[dict[str, str]] = []
    for timestamp in sorted(grouped):
        if after and timestamp <= after:
            continue
        rows = grouped[timestamp]
        values.append(
            {
                "timestamp": timestamp.isoformat(),
                "open": str(Decimal(rows[0]["open"])),
                "high": str(max(Decimal(item["high"]) for item in rows)),
                "low": str(min(Decimal(item["low"]) for item in rows)),
                "close": str(Decimal(rows[-1]["close"])),
                "volume": str(sum((Decimal(item["volume"]) for item in rows), Decimal())),
                "open_interest": str(
                    sum((Decimal(item["open_interest"]) for item in rows), Decimal())
                ),
            }
        )
    page = values[:limit]
    truncated = len(values) > len(page)
    return (
        page,
        _encode_cursor(datetime.fromisoformat(page[-1]["timestamp"]))
        if truncated and page
        else None,
        truncated,
    )


def quality_markers(
    warnings: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for issue in warnings:
        if issue.get("code") not in {"INTRADAY_GAP", "ATYPICAL_SESSION"}:
            continue
        details = issue.get("details", {})
        date_value = details.get("date")
        if date_value and start.date().isoformat() <= date_value <= end.date().isoformat():
            markers.append({"code": issue["code"], "message": issue["message"], "details": details})
    return markers
