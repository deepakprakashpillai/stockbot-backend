from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import cast

from app.domain.datasets import DatasetStatus, ValidationIssue, ValidationOutcome

NIFTY_INDEX_CANDLES_V1 = "nifty_index_candles_v1"
REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "open_interest"]
IST_OFFSET = timedelta(hours=5, minutes=30)
EXPECTED_SESSION_START = time(9, 15)
EXPECTED_SESSION_END = time(15, 25)
EXPECTED_SESSION_ROWS = 75


def _issue(code: str, message: str, **details: object) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, details=details)


def _parse_decimal(
    value: str, field: str, row_number: int, errors: list[ValidationIssue]
) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        errors.append(
            _issue(
                "INVALID_NUMERIC_VALUE",
                f"{field} must be numeric.",
                row=row_number,
                field=field,
                value=value,
            )
        )
        return None
    if not isfinite(float(parsed)):
        errors.append(
            _issue(
                "NON_FINITE_NUMERIC_VALUE",
                f"{field} must be finite.",
                row=row_number,
                field=field,
                value=value,
            )
        )
        return None
    return parsed


def validate_nifty_index_candles(path: Path) -> ValidationOutcome:
    warnings: list[ValidationIssue] = []
    errors: list[ValidationIssue] = []
    session_rows: dict[date, list[datetime]] = defaultdict(list)
    previous_timestamp: datetime | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    row_count = 0
    has_volume = False
    has_open_interest = False

    try:
        source = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        return ValidationOutcome(
            DatasetStatus.FAILED,
            0,
            [],
            None,
            None,
            [],
            [_issue("FILE_READ_FAILED", "The uploaded file could not be read.", reason=str(exc))],
            {},
        )

    with source:
        reader = csv.DictReader(source)
        if reader.fieldnames != REQUIRED_COLUMNS:
            return ValidationOutcome(
                DatasetStatus.INVALID,
                0,
                [],
                None,
                None,
                [],
                [
                    _issue(
                        "UNSUPPORTED_SCHEMA",
                        "CSV columns must match the NIFTY candle schema exactly.",
                        expected=REQUIRED_COLUMNS,
                        received=reader.fieldnames or [],
                    )
                ],
                {},
            )

        for row_number, row in enumerate(reader, start=2):
            row_count += 1
            raw_timestamp = row["timestamp"]
            try:
                timestamp = datetime.fromisoformat(raw_timestamp)
            except (ValueError, TypeError):
                errors.append(
                    _issue(
                        "INVALID_TIMESTAMP",
                        "timestamp must be ISO-8601.",
                        row=row_number,
                        value=raw_timestamp,
                    )
                )
                continue
            if timestamp.tzinfo is None or timestamp.utcoffset() != IST_OFFSET:
                errors.append(
                    _issue(
                        "INVALID_TIMEZONE",
                        "timestamp must use the +05:30 offset.",
                        row=row_number,
                        value=raw_timestamp,
                    )
                )
                continue
            if timestamp.second or timestamp.microsecond or timestamp.minute % 5:
                errors.append(
                    _issue(
                        "INVALID_CANDLE_ALIGNMENT",
                        "timestamps must align to five-minute boundaries.",
                        row=row_number,
                        value=raw_timestamp,
                    )
                )
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                errors.append(
                    _issue(
                        "TIMESTAMPS_NOT_STRICTLY_ORDERED",
                        "timestamps must be unique and strictly ascending.",
                        row=row_number,
                        previous=previous_timestamp.isoformat(),
                        current=timestamp.isoformat(),
                    )
                )
            previous_timestamp = timestamp

            values = {
                field: _parse_decimal(row[field], field, row_number, errors)
                for field in REQUIRED_COLUMNS[1:]
            }
            if not all(isinstance(value, Decimal) for value in values.values()):
                continue
            open_price = cast(Decimal, values["open"])
            high = cast(Decimal, values["high"])
            low = cast(Decimal, values["low"])
            close = cast(Decimal, values["close"])
            volume = cast(Decimal, values["volume"])
            open_interest = cast(Decimal, values["open_interest"])
            if open_price <= 0 or high <= 0 or low <= 0 or close <= 0:
                errors.append(
                    _issue("NON_POSITIVE_PRICE", "OHLC prices must be positive.", row=row_number)
                )
            if not low <= min(open_price, close) <= max(open_price, close) <= high:
                errors.append(
                    _issue(
                        "INVALID_OHLC_RELATIONSHIP",
                        "low <= open/close <= high must hold.",
                        row=row_number,
                    )
                )
            if volume < 0 or open_interest < 0:
                errors.append(
                    _issue(
                        "NEGATIVE_ACTIVITY_VALUE",
                        "volume and open_interest must be non-negative.",
                        row=row_number,
                    )
                )
            has_volume = has_volume or volume > 0
            has_open_interest = has_open_interest or open_interest > 0
            session_rows[timestamp.date()].append(timestamp)
            coverage_start = coverage_start or timestamp
            coverage_end = timestamp

    if row_count == 0:
        errors.append(_issue("EMPTY_DATASET", "The CSV contains no candle rows."))

    for session_date, timestamps in session_rows.items():
        for previous, current in zip(timestamps, timestamps[1:], strict=False):
            if current - previous != timedelta(minutes=5):
                warnings.append(
                    _issue(
                        "INTRADAY_GAP",
                        "A session contains a gap between candle timestamps.",
                        date=str(session_date),
                        previous=previous.isoformat(),
                        current=current.isoformat(),
                    )
                )
        if (
            len(timestamps) != EXPECTED_SESSION_ROWS
            or timestamps[0].time() != EXPECTED_SESSION_START
            or timestamps[-1].time() != EXPECTED_SESSION_END
        ):
            warnings.append(
                _issue(
                    "ATYPICAL_SESSION",
                    "A session does not match the usual 09:15–15:25 five-minute shape.",
                    date=str(session_date),
                    candle_count=len(timestamps),
                    first=timestamps[0].isoformat(),
                    last=timestamps[-1].isoformat(),
                )
            )

    if not has_volume:
        warnings.append(
            _issue(
                "ZERO_VOLUME_SERIES",
                "All volume values are zero; volume-based analysis is unavailable.",
            )
        )
    if not has_open_interest:
        warnings.append(
            _issue(
                "ZERO_OPEN_INTEREST_SERIES",
                "All open-interest values are zero; open-interest analysis is unavailable.",
            )
        )

    capabilities = {
        "price_candles": row_count > 0 and not errors,
        "volume_data": has_volume,
        "open_interest_data": has_open_interest,
        "futures_vwap": False,
        "india_vix": False,
        "option_ohlc": False,
        "option_contracts": False,
        "executable_bid_ask": False,
        # Deprecated names remain readable by legacy algorithm versions.
        "options_contracts": False,
        "executable_option_quotes": False,
        "options_pnl": False,
    }
    status = (
        DatasetStatus.INVALID
        if errors
        else DatasetStatus.WARNING
        if warnings
        else DatasetStatus.VALID
    )
    return ValidationOutcome(
        status,
        row_count,
        [str(day) for day in sorted(session_rows)],
        coverage_start,
        coverage_end,
        warnings,
        errors,
        capabilities,
    )
