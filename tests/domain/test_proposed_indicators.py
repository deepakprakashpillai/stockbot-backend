"""Proposed RSI vectors are separate from the saved historical algorithm."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.proposed_indicators import ProposedWilderRsi


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([Decimal("100")] * 28, Decimal("50")),
        ([Decimal(100 + index) for index in range(28)], Decimal("100")),
        ([Decimal(100 - index) for index in range(28)], Decimal("0")),
    ],
)
def test_warmup_and_flat_rising_falling_values(closes: list[Decimal], expected: Decimal) -> None:
    indicator = ProposedWilderRsi()
    values = [indicator.update_contiguous(close) for close in closes]
    assert values[:27] == [None] * 27
    assert values[27] == expected


def test_indicator_state_continues_across_call_batches() -> None:
    closes = [Decimal(100 + (index % 5) - (index % 3)) for index in range(40)]
    full = ProposedWilderRsi()
    one_pass = [full.update_contiguous(close) for close in closes]
    batched = ProposedWilderRsi()
    first = [batched.update_contiguous(close) for close in closes[:20]]
    second = [batched.update_contiguous(close) for close in closes[20:]]
    assert first + second == one_pass


def test_nonfinite_close_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        ProposedWilderRsi().update_contiguous(Decimal("NaN"))
