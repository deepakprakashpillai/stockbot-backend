from datetime import date
from pathlib import Path

from scripts.ingest_upstox_derivatives import futures_expiry_candidates, load_spot_atm_candidates


def test_option_ingestion_uses_all_completed_candle_atms_and_half_up_rounding(
    tmp_path: Path,
) -> None:
    spot = tmp_path / "spot.csv"
    spot.write_text(
        "timestamp,close\n"
        "2025-10-15T09:30:00+05:30,25175\n"
        "2025-10-15T09:35:00+05:30,25224\n"
        "2025-10-16T09:30:00+05:30,25225\n",
        encoding="utf-8",
    )

    candidates = load_spot_atm_candidates(spot)

    assert candidates[next(day for day in candidates if day.isoformat() == "2025-10-15")] == {25200}
    assert candidates[next(day for day in candidates if day.isoformat() == "2025-10-16")] == {25250}


def test_futures_expiry_candidates_include_the_rollover_after_the_requested_end() -> None:
    candidates = futures_expiry_candidates(
        [
            date(2026, 6, 30),
            date(2026, 7, 7),
            date(2026, 7, 14),
            date(2026, 7, 21),
            date(2026, 7, 28),
        ],
        date(2026, 6, 1),
        date(2026, 7, 17),
    )

    assert candidates == [date(2026, 6, 30), date(2026, 7, 21), date(2026, 7, 28)]
