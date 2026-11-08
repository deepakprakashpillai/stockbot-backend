from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain.market_data_time import completed_candle_timestamp
from app.domain.trading_calendar import NSE_FO_CALENDAR

IST = timezone(timedelta(hours=5, minutes=30))


def test_upstox_start_timestamp_becomes_completed_candle_time() -> None:
    source = datetime(2026, 1, 2, 9, 15, tzinfo=IST)

    assert completed_candle_timestamp(
        source,
        source_semantics="interval_start",
        interval_minutes=5,
    ) == datetime(2026, 1, 2, 9, 20, tzinfo=IST)
    assert (
        completed_candle_timestamp(
            source,
            source_semantics="interval_end",
            interval_minutes=5,
        )
        == source
    )


def test_candle_timestamp_rejects_invalid_intervals_and_semantics() -> None:
    source = datetime(2026, 1, 2, 9, 15, tzinfo=IST)

    with pytest.raises(ValueError, match="positive"):
        completed_candle_timestamp(
            source,
            source_semantics="interval_start",
            interval_minutes=0,
        )
    with pytest.raises(ValueError, match="Unsupported"):
        completed_candle_timestamp(
            source,
            source_semantics="unknown",  # type: ignore[arg-type]
            interval_minutes=5,
        )


def test_calendar_includes_special_sunday_and_excludes_exchange_holidays() -> None:
    assert NSE_FO_CALENDAR.is_session(date(2026, 2, 1)) is True
    assert NSE_FO_CALENDAR.is_session(date(2026, 1, 26)) is False
    assert NSE_FO_CALENDAR.is_session(date(2026, 1, 15)) is False


def test_previous_session_crosses_weekends_and_holidays() -> None:
    assert NSE_FO_CALENDAR.previous_session(date(2026, 1, 27)) == date(2026, 1, 23)
    assert NSE_FO_CALENDAR.previous_session(date(2026, 2, 2)) == date(2026, 2, 1)


def test_calendar_refuses_unversioned_years() -> None:
    with pytest.raises(ValueError, match="does not cover"):
        NSE_FO_CALENDAR.sessions(date(2027, 1, 1), date(2027, 1, 2))
