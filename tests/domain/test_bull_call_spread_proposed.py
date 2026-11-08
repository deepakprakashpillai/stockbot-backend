"""Boundary vectors for the unsigned partner scoring proposal."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.bull_call_spread_proposed import ProposedEntryObservation, evaluate_proposed_entry

BASE = ProposedEntryObservation(
    spot_close=Decimal("25100"),
    previous_spot_high=Decimal("25000"),
    futures_close=Decimal("25150"),
    futures_vwap=Decimal("25100"),
    rsi=Decimal("59"),
    india_vix=Decimal("14.24"),
    previous_india_vix=Decimal("15"),
)


@pytest.mark.parametrize(
    ("vix", "rsi", "score", "accepted"),
    [
        ("14.24", "58", 3, True),
        ("14.24", "59", 5, True),
        ("14.70", "59", 4, True),
        ("14.70", "58", 2, False),
        ("14.71", "59", 2, False),
        ("14.71", "58", 0, False),
    ],
)
def test_partner_score_table(vix: str, rsi: str, score: int, accepted: bool) -> None:
    result = evaluate_proposed_entry(replace(BASE, india_vix=Decimal(vix), rsi=Decimal(rsi)))
    assert result.score == score
    assert result.accepted is accepted


def test_exact_vix_boundaries_and_session_latch() -> None:
    exact_five = evaluate_proposed_entry(replace(BASE, india_vix=Decimal("14.25")))
    assert exact_five.vix_points == 2
    assert exact_five.vix_decline_percent == Decimal("5")
    breach = evaluate_proposed_entry(replace(BASE, india_vix=Decimal("22")))
    assert breach.reason == "VIX_LEVEL_BLOCKED"
    assert breach.vix_breach_latched is True
    later = evaluate_proposed_entry(BASE, vix_breach_latched=breach.vix_breach_latched)
    assert later.reason == "VIX_LEVEL_BLOCKED"


def test_strict_price_gates_and_required_rsi() -> None:
    pdh = evaluate_proposed_entry(replace(BASE, spot_close=BASE.previous_spot_high))
    assert pdh.reason == "PDH_GATE_FAILED"
    assert pdh.score is None
    vwap = evaluate_proposed_entry(replace(BASE, futures_close=BASE.futures_vwap))
    assert vwap.reason == "FUTURES_VWAP_GATE_FAILED"
    assert vwap.score is None
    missing_rsi = evaluate_proposed_entry(replace(BASE, rsi=None))
    assert missing_rsi.reason == "RSI_UNAVAILABLE"


def test_invalid_previous_vix_is_not_silently_scored() -> None:
    with pytest.raises(ValueError, match="positive"):
        evaluate_proposed_entry(replace(BASE, previous_india_vix=Decimal("0")))
