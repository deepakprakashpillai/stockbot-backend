from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.application.backtest_coverage import analyze_derivatives_coverage
from app.application.backtests import BacktestService
from app.domain.algorithms import BULL_CALL_SPREAD, FUTURES_BULL_CALL_SPREAD
from app.domain.backtests import evaluate_chronologically
from app.domain.option_spread_models import OpenPositionDataGapError, OptionSpreadAssumptions
from app.domain.option_spread_monitor import monitor_spread_exit
from app.domain.signal_diagnostics import build_signal_diagnostic
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider
from tests.market_fixture import market_bundle
from tests.test_backtest_engine import boundary_history


def evaluate(
    *, spot=110, futures=125, vwap=120, observed_delta=0, definition=FUTURES_BULL_CALL_SPREAD
):
    history = boundary_history()
    history[-1] = replace(
        history[-1],
        close=spot,
        high=max(spot, 110),
        futures_close=futures,
        futures_vwap=vwap,
        futures_observed_at=history[-1].timestamp + timedelta(minutes=observed_delta),
    )
    return list(
        evaluate_chronologically(history, definition.configure(definition.templates[0].parameters))
    )[-1]


def test_corrected_gate_uses_futures_and_preserves_the_spot_breakout_gate():
    corrected = evaluate()
    old = evaluate(definition=BULL_CALL_SPREAD)
    assert corrected.decision == "ACCEPTED"
    assert old.rejection_reason == "FUTURES_VWAP_GATE_FAILED"
    assert evaluate(spot=130, futures=115).rejection_reason == "FUTURES_VWAP_GATE_FAILED"
    assert evaluate(spot=100, futures=125).rejection_reason == "PDH_GATE_FAILED"
    assert evaluate(futures=120).rejection_reason == "FUTURES_VWAP_GATE_FAILED"
    assert evaluate(futures=120.01).decision == "ACCEPTED"
    assert corrected.score == old.score or old.score is None


@pytest.mark.parametrize(
    "futures,vwap,delta",
    [
        (None, 120, 0),
        (float("nan"), 120, 0),
        (float("inf"), 120, 0),
        (0, 120, 0),
        (125, None, 0),
        (125, float("nan"), 0),
        (125, 120, -5),
        (125, 120, 5),
    ],
)
def test_corrected_gate_rejects_missing_invalid_or_unsynchronized_futures(futures, vwap, delta):
    decision = evaluate(futures=futures, vwap=vwap, observed_delta=delta)
    assert decision.rejection_reason == "REQUIRED_MARKET_INPUT_MISSING"
    assert decision.score is None


def test_diagnostic_records_the_actual_price_compared():
    diagnostic = build_signal_diagnostic(
        evaluate(),
        definition=FUTURES_BULL_CALL_SPREAD,
        parameter_version_id="test",
        parameter_snapshot=FUTURES_BULL_CALL_SPREAD.templates[0].parameters,
    )
    gate = next(
        step for step in diagnostic["evaluation_steps"] if step["logic_step_id"] == "vwap_gate"
    )
    assert gate["observed_value"] == 125
    assert gate["threshold_value"] == 120
    assert gate["status"] == "passed"
    assert gate["label"].startswith("Futures close")
    assert (
        next(item for item in diagnostic["market_context"] if item["key"] == "close")["exact_value"]
        == 110
    )


def monitor(*, spot, futures, corrected=True):
    candle = replace(boundary_history()[-1], close=spot, futures_close=futures, futures_vwap=120)
    candle = replace(candle, futures_observed_at=candle.timestamp)
    final = replace(
        candle,
        timestamp=candle.timestamp.replace(hour=15),
        futures_observed_at=candle.timestamp.replace(hour=15),
    )
    definition = FUTURES_BULL_CALL_SPREAD if corrected else BULL_CALL_SPREAD
    assumptions = OptionSpreadAssumptions.from_parameters(
        definition.configure(definition.templates[0].parameters).parameters,
        starting_capital=Decimal("500000"),
    )
    events = []
    result = monitor_spread_exit(
        session_candles=[candle, final],
        buy_candles={item.timestamp: SimpleNamespace(close=65) for item in (candle, final)},
        sell_candles={item.timestamp: SimpleNamespace(close=20) for item in (candle, final)},
        entry_timestamp=candle.timestamp - timedelta(minutes=5),
        assumptions=assumptions,
        stop_level=Decimal("40"),
        target_level=Decimal("80"),
        trend_level=Decimal("50"),
        signal_sequence=1,
        emit=lambda name, timestamp, **payload: events.append(payload),
        should_cancel=None,
    )
    return result, events, assumptions


def test_reversal_uses_futures_in_both_directions_and_preserves_old_exit():
    result, events, assumptions = monitor(spot=110, futures=125)
    assert result.reason == "SESSION_SQUARE_OFF"
    assert assumptions.snapshot()["vwap_comparison_price"] == "futures_close"
    assert events[0]["checks"][2]["observed_value"] == "125"
    assert events[0]["checks"][2]["status"] == "failed"
    assert monitor(spot=130, futures=115)[0].reason == "TREND_REVERSAL"
    assert monitor(spot=110, futures=120)[0].reason == "SESSION_SQUARE_OFF"
    assert monitor(spot=110, futures=125, corrected=False)[0].reason == "TREND_REVERSAL"
    with pytest.raises(OpenPositionDataGapError, match="synchronized_futures_close"):
        monitor(spot=130, futures=None)


def test_preflight_excludes_missing_futures_close_even_when_vwap_exists(tmp_path, monkeypatch):
    bundle = market_bundle(tmp_path)
    original = ParquetDerivativesProvider._enrich_session

    def enrich(provider, day, candles):
        for candle in original(provider, day, candles):
            yield replace(candle, futures_close=None) if day == date(2025, 7, 21) else candle

    monkeypatch.setattr(ParquetDerivativesProvider, "_enrich_session", enrich)
    arguments = dict(
        bundle_dir=bundle,
        spot_path=bundle / "spot/nifty_spot_5min.csv",
        start_date=date(2025, 7, 21),
        end_date=date(2025, 7, 21),
    )
    corrected = analyze_derivatives_coverage(
        **arguments,
        algorithm=FUTURES_BULL_CALL_SPREAD,
        parameters=FUTURES_BULL_CALL_SPREAD.configure({}).parameters,
    )
    control = analyze_derivatives_coverage(
        **arguments,
        algorithm=BULL_CALL_SPREAD,
        parameters=BULL_CALL_SPREAD.configure({}).parameters,
    )
    assert corrected["included_session_count"] == 0
    assert corrected["excluded_sessions"][0]["reason_code"] == "SYNCHRONIZED_MARKET_INPUT_MISSING"
    assert control["included_session_count"] == 1
    assert (
        BacktestService._mode(FUTURES_BULL_CALL_SPREAD.key, FUTURES_BULL_CALL_SPREAD.code_version)
        == "OPTION_SPREAD_SIMULATION"
    )
