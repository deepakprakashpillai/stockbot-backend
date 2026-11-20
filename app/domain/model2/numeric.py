"""Exact numeric guards; no binary floats in strategy money calculations."""

from decimal import ROUND_HALF_UP, Decimal


def finite_decimal(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or (positive and value <= 0):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}Decimal.")
    return value


def inr(value: Decimal) -> Decimal:
    return finite_decimal(value, "Money").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
