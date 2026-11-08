"""Unsigned partner research indicators for contiguous completed candles only."""

from __future__ import annotations

from decimal import Decimal


class ProposedWilderRsi:
    """Wilder RSI(14), neutral flat series, valid after 28 completed closes.

    A caller must establish candle continuity. Gap recovery is deliberately not
    specified here because the partner response does not settle that policy.
    """

    def __init__(self) -> None:
        self.previous: Decimal | None = None
        self.closes = 0
        self.changes = 0
        self.gain = Decimal("0")
        self.loss = Decimal("0")

    def update_contiguous(self, close: Decimal) -> Decimal | None:
        if not close.is_finite() or close <= 0:
            raise ValueError("A completed close must be positive and finite.")
        self.closes += 1
        if self.previous is None:
            self.previous = close
            return None
        change = close - self.previous
        self.previous = close
        self.changes += 1
        if self.changes <= 14:
            self.gain += max(change, Decimal("0"))
            self.loss += max(-change, Decimal("0"))
            if self.changes == 14:
                self.gain /= Decimal("14")
                self.loss /= Decimal("14")
        else:
            self.gain = (self.gain * Decimal("13") + max(change, Decimal("0"))) / Decimal("14")
            self.loss = (self.loss * Decimal("13") + max(-change, Decimal("0"))) / Decimal("14")
        if self.closes < 28:
            return None
        if self.gain == 0 and self.loss == 0:
            return Decimal("50")
        if self.loss == 0:
            return Decimal("100")
        if self.gain == 0:
            return Decimal("0")
        return Decimal("100") - Decimal("100") / (Decimal("1") + self.gain / self.loss)
