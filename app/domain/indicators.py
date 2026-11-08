"""Pure indicators used by historical signal versions."""

from __future__ import annotations


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder/RMA RSI from completed candles across session boundaries."""

    if len(closes) < period + 1:
        return None
    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, period + 1)]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, period + 1)]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        average_gain = ((average_gain * (period - 1)) + max(change, 0.0)) / period
        average_loss = ((average_loss * (period - 1)) + max(-change, 0.0)) / period
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


class WilderRsi:
    """Incremental equivalent of the retained historical RSI reference calculation."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.previous: float | None = None
        self.changes = 0
        self.gain = 0.0
        self.loss = 0.0

    def update(self, close: float) -> float | None:
        if self.previous is None:
            self.previous = close
            return None
        change = close - self.previous
        self.previous = close
        self.changes += 1
        if self.changes <= self.period:
            self.gain += max(change, 0.0)
            self.loss += max(-change, 0.0)
            if self.changes < self.period:
                return None
            self.gain /= self.period
            self.loss /= self.period
        else:
            self.gain = ((self.gain * (self.period - 1)) + max(change, 0.0)) / self.period
            self.loss = ((self.loss * (self.period - 1)) + max(-change, 0.0)) / self.period
        if self.loss == 0:
            return 100.0
        return 100 - (100 / (1 + self.gain / self.loss))
