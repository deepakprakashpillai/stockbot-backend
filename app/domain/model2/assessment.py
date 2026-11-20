"""Approved data-quality gate only; full strategy pass/fail remains a later report."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.domain.model2.approved_specification import RELIABILITY_POLICY_VERSION


@dataclass(frozen=True)
class ReliabilityAssessment:
    opened_cycles: int
    excluded_cycles: int
    exclusion_fraction: Decimal | None
    status: Literal["RELIABLE_DATA", "UNRELIABLE_DATA", "INSUFFICIENT_SAMPLE"]
    data_gate_passed: bool
    policy_version: str = RELIABILITY_POLICY_VERSION


def assess_reliability(opened_cycles: int, excluded_cycles: int) -> ReliabilityAssessment:
    if (
        type(opened_cycles) is not int
        or type(excluded_cycles) is not int
        or not 0 <= excluded_cycles <= opened_cycles
    ):
        raise ValueError("Excluded/opened counts must be integers with 0 <= excluded <= opened.")
    if opened_cycles == 0:
        return ReliabilityAssessment(0, 0, None, "INSUFFICIENT_SAMPLE", False)
    # Exact integer comparison protects the 2% boundary from decimal rounding.
    unreliable = excluded_cycles * 50 > opened_cycles
    return ReliabilityAssessment(
        opened_cycles,
        excluded_cycles,
        Decimal(excluded_cycles) / opened_cycles,
        "UNRELIABLE_DATA" if unreliable else "RELIABLE_DATA",
        not unreliable,
    )
