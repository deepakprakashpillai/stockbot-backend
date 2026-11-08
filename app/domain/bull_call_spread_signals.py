"""Retained Bull Call Spread signal versions; no infrastructure dependencies."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime, time
from typing import Any, cast

from app.domain.algorithms import (
    BullCallSpreadSignalParameters,
    ConfiguredAlgorithm,
    LegacyBullCallSpreadSignalParameters,
    ScoreModel,
)
from app.domain.indicators import WilderRsi
from app.domain.signal_models import EXCHANGE_OFFSET, MarketCandle, SignalDecision


def _window(value: time, parameters: Any) -> str | None:
    if getattr(parameters, "entry_window_1_enabled", True) and (
        parameters.entry_window_1_start <= value <= parameters.entry_window_1_end
    ):
        return "window_1"
    if getattr(parameters, "entry_window_2_enabled", True) and (
        parameters.entry_window_2_start <= value <= parameters.entry_window_2_end
    ):
        return "window_2"
    return None


def evaluate_bull_call_spread(
    candles: Iterable[MarketCandle],
    algorithm: ConfiguredAlgorithm,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[SignalDecision]:
    parameters = algorithm.parameters
    if not isinstance(
        parameters, (BullCallSpreadSignalParameters, LegacyBullCallSpreadSignalParameters)
    ):
        raise TypeError("Bull Call Spread requires BullCallSpreadSignalParameters.")
    # Runtime validation above guarantees the field accesses in the matching
    # V1 or archived-replay branch below.
    parameters = cast(Any, parameters)
    legacy = isinstance(parameters, LegacyBullCallSpreadSignalParameters)
    hard_gates = algorithm.code_version == "1.2.0-bull-call-spread-v1"
    unavailable_score = None if hard_gates else 0
    indicator = WilderRsi(14 if legacy else parameters.rsi_period)
    current_session: date | None = None
    session_high: float | None = None
    previous_session_high: float | None = None
    last_timestamp: datetime | None = None
    for candle in candles:
        if should_cancel and should_cancel():
            return
        if candle.timestamp.tzinfo is None or candle.timestamp.utcoffset() != EXCHANGE_OFFSET:
            raise ValueError("Market data timestamps must use the Asia/Kolkata +05:30 offset.")
        if candle.timestamp.second or candle.timestamp.microsecond or candle.timestamp.minute % 5:
            raise ValueError("Market data timestamps must represent completed five-minute candles.")
        if last_timestamp is not None and candle.timestamp <= last_timestamp:
            raise ValueError("Market data must be strictly chronological.")
        last_timestamp = candle.timestamp
        session = candle.timestamp.date()
        if current_session != session:
            if session_high is not None:
                previous_session_high = session_high
            current_session, session_high = session, candle.high
        else:
            session_high = max(session_high or candle.high, candle.high)
        rsi = indicator.update(candle.close)
        window = _window(candle.timestamp.timetz().replace(tzinfo=None), parameters)
        inputs: dict[str, Any] = {
            "close": candle.close,
            "previous_session_high": previous_session_high,
            "futures_vwap": candle.futures_vwap,
            "india_vix": candle.india_vix,
            "previous_india_vix": candle.previous_india_vix,
            "rsi": rsi,
            "score_model": (
                parameters.score_model.value
                if legacy
                else "hard_gates_confirmation_5"
                if hard_gates
                else "primary_weighted_10"
            ),
        }
        if window is None:
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "OUTSIDE_ENTRY_WINDOW",
                None,
                unavailable_score,
                inputs,
            )
            continue
        if not legacy:
            window_start = (
                parameters.entry_window_1_start
                if window == "window_1"
                else parameters.entry_window_2_start
            )
            elapsed_minutes = (
                candle.timestamp.hour * 60
                + candle.timestamp.minute
                - window_start.hour * 60
                - window_start.minute
            )
            if elapsed_minutes % parameters.evaluation_interval_minutes:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "EVALUATION_INTERVAL_NOT_REACHED",
                    window,
                    unavailable_score,
                    inputs,
                )
                continue
        if previous_session_high is None:
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "PDH_UNAVAILABLE",
                window,
                unavailable_score,
                inputs,
            )
            continue
        if hard_gates:
            pdh_positive = candle.close > previous_session_high
            inputs["pdh_gate_passed"] = pdh_positive
            if not pdh_positive:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "PDH_GATE_FAILED",
                    window,
                    None,
                    inputs,
                )
                continue
            if candle.futures_vwap is None:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "REQUIRED_MARKET_INPUT_MISSING",
                    window,
                    None,
                    inputs,
                )
                continue
            vwap_positive = candle.close > candle.futures_vwap
            inputs["futures_vwap_gate_passed"] = vwap_positive
            if not vwap_positive:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "FUTURES_VWAP_GATE_FAILED",
                    window,
                    None,
                    inputs,
                )
                continue
        if rsi is None:
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "RSI_WARMUP",
                window,
                unavailable_score,
                inputs,
            )
            continue
        if (
            candle.futures_vwap is None
            or candle.india_vix is None
            or candle.previous_india_vix is None
        ):
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "REQUIRED_MARKET_INPUT_MISSING",
                window,
                unavailable_score,
                inputs,
            )
            continue
        if candle.previous_india_vix <= 0:
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "PREVIOUS_VIX_INVALID",
                window,
                unavailable_score,
                inputs,
            )
            continue
        vix_decline = (
            (candle.previous_india_vix - candle.india_vix) / candle.previous_india_vix
        ) * 100
        inputs["vix_decline_percent"] = vix_decline
        pdh_positive = candle.close > previous_session_high
        vwap_positive = candle.close > candle.futures_vwap
        rsi_positive = rsi > parameters.rsi_threshold
        vix_gate = (
            candle.india_vix < parameters.vix_gate_threshold
            if legacy
            else candle.india_vix <= parameters.vix_maximum
        )
        vix_positive = vix_decline > parameters.minimum_vix_decline_percent if not legacy else False
        vix_points = (
            (
                3
                if vix_decline > parameters.vix_strong_decline_percent
                else (2 if vix_decline >= parameters.vix_min_decline_percent else 0)
            )
            if legacy
            else (parameters.vix_weight if vix_positive else 0)
        )
        inputs.update(
            {
                "pdh_gate_passed": pdh_positive,
                "futures_vwap_gate_passed": vwap_positive,
                "rsi_confirmation_passed": rsi_positive,
                "vix_gate_passed": vix_gate,
                "vix_confirmation_passed": vix_positive if not legacy else vix_points > 0,
                "vix_confirmation_points": vix_points,
            }
        )
        if hard_gates:
            score = (parameters.rsi_weight if rsi_positive else 0) + vix_points
            inputs.update(
                {
                    "score_weights": {
                        "rsi": parameters.rsi_weight,
                        "vix": parameters.vix_weight,
                    },
                    "minimum_score": parameters.minimum_score,
                    "maximum_score": parameters.rsi_weight + parameters.vix_weight,
                }
            )
            if not vix_gate:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "VIX_GATE_FAILED",
                    window,
                    score,
                    inputs,
                )
                continue
        elif legacy and parameters.score_model == ScoreModel.HARD_GATES_CONFIRMATION_5:
            if not pdh_positive:
                yield SignalDecision(
                    candle.timestamp, session, "REJECTED", "PDH_GATE_FAILED", window, 0, inputs
                )
                continue
            if not vwap_positive:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "FUTURES_VWAP_GATE_FAILED",
                    window,
                    0,
                    inputs,
                )
                continue
            if not vix_gate:
                yield SignalDecision(
                    candle.timestamp, session, "REJECTED", "VIX_GATE_FAILED", window, 0, inputs
                )
                continue
            score = (2 if rsi_positive else 0) + vix_points
        elif legacy:
            score = (
                (3 if pdh_positive else 0)
                + (2 if vwap_positive else 0)
                + (2 if rsi_positive else 0)
                + (3 if vix_decline > 0 else 0)
            )
            if not vix_gate:
                yield SignalDecision(
                    candle.timestamp, session, "REJECTED", "VIX_GATE_FAILED", window, score, inputs
                )
                continue
        else:
            score = (
                (parameters.pdh_weight if pdh_positive else 0)
                + (parameters.vwap_weight if vwap_positive else 0)
                + (parameters.rsi_weight if rsi_positive else 0)
                + vix_points
            )
            inputs.update(
                {
                    "score_weights": {
                        "pdh": parameters.pdh_weight,
                        "vwap": parameters.vwap_weight,
                        "rsi": parameters.rsi_weight,
                        "vix": parameters.vix_weight,
                    },
                    "minimum_score": parameters.minimum_score,
                }
            )
            if not vix_gate:
                yield SignalDecision(
                    candle.timestamp,
                    session,
                    "REJECTED",
                    "VIX_GATE_FAILED",
                    window,
                    score,
                    inputs,
                )
                continue
        minimum_score = (
            parameters.minimum_confirmation_score if legacy else parameters.minimum_score
        )
        if score < minimum_score:
            yield SignalDecision(
                candle.timestamp,
                session,
                "REJECTED",
                "CONFIRMATION_SCORE_TOO_LOW",
                window,
                score,
                inputs,
            )
            continue
        yield SignalDecision(candle.timestamp, session, "ACCEPTED", None, window, score, inputs)
