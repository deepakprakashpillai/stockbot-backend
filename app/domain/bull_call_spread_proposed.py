"""Isolated research vectors from the unsigned 7 November partner response.

This module is deliberately absent from the algorithm registry. It can check
complete, synchronized observations without deciding unresolved gap/roll policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ProposedEntryObservation:
    spot_close: Decimal
    previous_spot_high: Decimal
    futures_close: Decimal
    futures_vwap: Decimal
    rsi: Decimal | None
    india_vix: Decimal
    previous_india_vix: Decimal


@dataclass(frozen=True)
class ProposedEntryDecision:
    accepted: bool
    reason: str | None
    score: int | None
    vix_points: int | None
    vix_decline_percent: Decimal | None
    vix_breach_latched: bool


def evaluate_proposed_entry(
    observation: ProposedEntryObservation, *, vix_breach_latched: bool = False
) -> ProposedEntryDecision:
    """Check L01–L03 on complete data; callers own missing-data and roll policy."""

    if observation.previous_india_vix <= 0:
        raise ValueError("The previous VIX close must be positive.")
    if observation.india_vix < 0:
        raise ValueError("India VIX cannot be negative.")
    breached = vix_breach_latched or observation.india_vix >= Decimal("22")
    if observation.spot_close <= observation.previous_spot_high:
        return ProposedEntryDecision(False, "PDH_GATE_FAILED", None, None, None, breached)
    if observation.futures_close <= observation.futures_vwap:
        return ProposedEntryDecision(False, "FUTURES_VWAP_GATE_FAILED", None, None, None, breached)
    if observation.rsi is None:
        return ProposedEntryDecision(False, "RSI_UNAVAILABLE", None, None, None, breached)
    decline = (
        (observation.previous_india_vix - observation.india_vix)
        / observation.previous_india_vix
        * Decimal("100")
    )
    vix_points = 3 if decline > 5 else 2 if decline >= 2 else 0
    score = (2 if observation.rsi > 58 else 0) + vix_points
    if breached:
        return ProposedEntryDecision(False, "VIX_LEVEL_BLOCKED", score, vix_points, decline, True)
    if score < 3:
        return ProposedEntryDecision(
            False, "CONFIRMATION_SCORE_TOO_LOW", score, vix_points, decline, False
        )
    return ProposedEntryDecision(True, None, score, vix_points, decline, False)
