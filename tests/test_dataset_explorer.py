from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.domain.dataset_explorer import preview_csv, profile_csv, quality_markers, read_candles


def fixture_csv(path: Path) -> None:
    path.write_text(
        "timestamp,open,high,low,close,volume,open_interest\n"
        "2025-07-21T09:15:00+05:30,100,102,99,101,10,2\n"
        "2025-07-21T09:20:00+05:30,101,105,100,104,15,3\n"
        "2025-07-21T09:25:00+05:30,104,106,102,103,20,4\n"
        "2025-07-21T09:30:00+05:30,103,107,101,106,25,5\n",
        encoding="utf-8",
    )


def test_preview_and_profile_are_bounded_and_never_return_storage_paths(tmp_path: Path) -> None:
    source = tmp_path / "nifty.csv"
    fixture_csv(source)

    preview = preview_csv(source, sample_limit=2)
    profile = profile_csv(source)

    assert preview["status"] == "committable"
    assert len(preview["sample_rows"]) == 2
    assert preview["headers"] == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]
    assert profile["row_count_sampled"] == 4
    assert profile["columns"][1]["minimum"] == "100"


def test_preview_blocks_unsupported_schema(tmp_path: Path) -> None:
    source = tmp_path / "wrong.csv"
    source.write_text("when,price\n2025-01-01,100\n", encoding="utf-8")

    preview = preview_csv(source)

    assert preview["status"] == "blocked"
    assert preview["issues"][0]["code"] == "PREVIEW_UNSUPPORTED_SCHEMA"


def test_candle_aggregation_preserves_ohlc_and_cursor(tmp_path: Path) -> None:
    source = tmp_path / "nifty.csv"
    fixture_csv(source)
    start = datetime.fromisoformat("2025-07-21T09:15:00+05:30")
    end = datetime.fromisoformat("2025-07-21T09:30:00+05:30")

    candles, cursor, truncated = read_candles(
        source, start=start, end=end, granularity="15m", cursor=None, limit=1
    )

    assert truncated is True
    assert cursor is not None
    assert candles[0] == {
        "timestamp": "2025-07-21T09:15:00+05:30",
        "open": "100",
        "high": "106",
        "low": "99",
        "close": "103",
        "volume": "45",
        "open_interest": "9",
    }
    second, _, _ = read_candles(
        source, start=start, end=end, granularity="15m", cursor=cursor, limit=10
    )
    assert [item["timestamp"] for item in second] == ["2025-07-21T09:30:00+05:30"]


def test_invalid_candle_cursor_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "nifty.csv"
    fixture_csv(source)
    with pytest.raises(ValueError):
        read_candles(
            source,
            start=datetime.fromisoformat("2025-07-21T09:15:00+05:30"),
            end=datetime.fromisoformat("2025-07-21T09:25:00+05:30"),
            granularity="5m",
            cursor="not-a-cursor",
            limit=10,
        )


def test_quality_markers_intersect_requested_range() -> None:
    markers = quality_markers(
        [
            {"code": "ATYPICAL_SESSION", "message": "short", "details": {"date": "2025-07-21"}},
            {"code": "ZERO_VOLUME_SERIES", "message": "zero", "details": {}},
        ],
        datetime.fromisoformat("2025-07-21T09:15:00+05:30"),
        datetime.fromisoformat("2025-07-21T15:25:00+05:30"),
    )
    assert [marker["code"] for marker in markers] == ["ATYPICAL_SESSION"]
