from __future__ import annotations

from pathlib import Path

from app.domain.dataset_validation import REQUIRED_COLUMNS, validate_nifty_index_candles
from app.domain.datasets import DatasetStatus


def write_csv(path: Path, rows: list[str], header: str = ",".join(REQUIRED_COLUMNS)) -> Path:
    path.write_text("\n".join([header, *rows, ""]), encoding="utf-8")
    return path


def candle(
    timestamp: str,
    open_price: str = "100",
    high: str = "102",
    low: str = "99",
    close: str = "101",
    volume: str = "0",
    open_interest: str = "0",
) -> str:
    return ",".join([timestamp, open_price, high, low, close, volume, open_interest])


def test_validator_reports_candle_only_dataset_warnings(tmp_path: Path) -> None:
    outcome = validate_nifty_index_candles(
        write_csv(
            tmp_path / "candles.csv",
            [
                candle("2025-07-21T09:15:00+05:30"),
                candle("2025-07-21T09:20:00+05:30"),
            ],
        )
    )

    assert outcome.status == DatasetStatus.WARNING
    assert outcome.row_count == 2
    assert outcome.session_count == 1
    assert outcome.capabilities["price_candles"]
    assert not outcome.capabilities["options_pnl"]
    assert {issue.code for issue in outcome.warnings} >= {
        "ATYPICAL_SESSION",
        "ZERO_VOLUME_SERIES",
        "ZERO_OPEN_INTEREST_SERIES",
    }


def test_validator_rejects_schema_timestamp_and_ohlc_errors(tmp_path: Path) -> None:
    bad_schema = validate_nifty_index_candles(
        write_csv(tmp_path / "bad-schema.csv", ["1,2"], header="timestamp,close")
    )
    assert bad_schema.status == DatasetStatus.INVALID
    assert bad_schema.errors[0].code == "UNSUPPORTED_SCHEMA"

    invalid_rows = validate_nifty_index_candles(
        write_csv(
            tmp_path / "invalid.csv",
            [
                candle("2025-07-21T09:20:00+05:30", low="103"),
                candle("2025-07-21T09:15:00+05:30"),
            ],
        )
    )
    assert invalid_rows.status == DatasetStatus.INVALID
    assert {issue.code for issue in invalid_rows.errors} >= {
        "INVALID_OHLC_RELATIONSHIP",
        "TIMESTAMPS_NOT_STRICTLY_ORDERED",
    }


def test_validator_reports_intraday_gaps_and_empty_files(tmp_path: Path) -> None:
    gapped = validate_nifty_index_candles(
        write_csv(
            tmp_path / "gapped.csv",
            [candle("2025-07-21T09:15:00+05:30"), candle("2025-07-21T09:25:00+05:30")],
        )
    )
    assert any(issue.code == "INTRADAY_GAP" for issue in gapped.warnings)

    empty = validate_nifty_index_candles(write_csv(tmp_path / "empty.csv", []))
    assert empty.status == DatasetStatus.INVALID
    assert any(issue.code == "EMPTY_DATASET" for issue in empty.errors)
