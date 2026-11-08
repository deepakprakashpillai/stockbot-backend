from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import pytest

from app.application.backtest_coverage import analyze_derivatives_coverage
from app.domain.algorithms import BULL_CALL_SPREAD, BullCallSpreadHardGateParameters
from app.domain.backtests import MarketCandle, evaluate_chronologically
from app.domain.derivatives_provider import ParquetDerivativesProvider

V5_DATASET = Path("data/datasets/upstox_nifty_derivatives_v5")
V6_DATASET = Path("data/datasets/upstox_nifty_derivatives_v6")


def _spot_candles(bundle: Path, start: date, end: date) -> list[MarketCandle]:
    candles: list[MarketCandle] = []
    with (bundle / "spot" / "nifty_spot_5min.csv").open(
        "r", encoding="utf-8", newline=""
    ) as source:
        for row in csv.DictReader(source):
            timestamp = datetime.fromisoformat(row["timestamp"])
            if start <= timestamp.date() <= end:
                candles.append(
                    MarketCandle(
                        timestamp=timestamp,
                        high=float(row["high"]),
                        close=float(row["close"]),
                        futures_vwap=None,
                        india_vix=None,
                        previous_india_vix=None,
                    )
                )
    return candles


def test_v6_warmup_initializes_21_july_pdh_and_rejects_22_july_below_vwap() -> None:
    if not V6_DATASET.exists():
        pytest.skip("Optional local V6 market dataset is not installed.")

    provider = ParquetDerivativesProvider(V6_DATASET)
    candles = provider.enrich_spot_candles(
        _spot_candles(V6_DATASET, date(2025, 7, 18), date(2025, 7, 22))
    )
    configured = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
    decisions = list(evaluate_chronologically(candles, configured))

    first_requested_day = [
        decision for decision in decisions if decision.session_date == date(2025, 7, 21)
    ]
    assert first_requested_day
    assert not any(item.rejection_reason == "PDH_UNAVAILABLE" for item in first_requested_day)
    assert {item.inputs["previous_session_high"] for item in first_requested_day} == {25144.6}

    example = next(
        item
        for item in decisions
        if item.timestamp == datetime.fromisoformat("2025-07-22T09:30:00+05:30")
    )
    assert example.inputs["close"] == 25124.0
    assert example.inputs["futures_vwap"] == 25165.36
    assert example.inputs["pdh_gate_passed"] is True
    assert example.inputs["futures_vwap_gate_passed"] is False
    assert example.rejection_reason == "FUTURES_VWAP_GATE_FAILED"
    assert example.score is None


def test_missing_previous_actual_session_is_excluded_before_evaluation() -> None:
    if not V5_DATASET.exists():
        pytest.skip("Optional local V5 market dataset is not installed.")

    result = analyze_derivatives_coverage(
        bundle_dir=V5_DATASET,
        spot_path=V5_DATASET / "spot" / "nifty_spot_5min.csv",
        start_date=date(2025, 7, 21),
        end_date=date(2025, 7, 21),
        parameters=BullCallSpreadHardGateParameters(),
    )

    assert result["included_session_count"] == 0
    assert result["excluded_sessions"] == [
        {
            "session_date": "2025-07-21",
            "classification": "EXCLUDED_WARMUP_GAP",
            "reason_code": "PREVIOUS_TRADING_SESSION_UNAVAILABLE",
            "details": {"required_previous_session": "2025-07-18"},
        }
    ]


def test_contract_metadata_preserves_the_official_75_to_65_transition() -> None:
    if not V6_DATASET.exists():
        pytest.skip("Optional local V6 market dataset is not installed.")

    provider = ParquetDerivativesProvider(V6_DATASET)
    assert provider.get_active_expiry_for_date(date(2025, 12, 22)) == date(2025, 12, 23)
    assert provider.get_lot_size(date(2025, 12, 22)) == 75
    assert provider.get_active_expiry_for_date(date(2026, 1, 2)) == date(2026, 1, 6)
    assert provider.get_lot_size(date(2026, 1, 2)) == 65
