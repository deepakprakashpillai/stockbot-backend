"""Validate the like-for-like completed futures observation without a spot fallback."""

from math import isfinite

from app.domain.signal_models import MarketCandle


def futures_comparison_available(candle: MarketCandle) -> bool:
    return (
        candle.futures_close is not None
        and isfinite(candle.futures_close)
        and candle.futures_close > 0
        and candle.futures_vwap is not None
        and isfinite(candle.futures_vwap)
        and candle.futures_vwap > 0
        and candle.futures_observed_at == candle.timestamp
    )
