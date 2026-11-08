from datetime import datetime, timedelta

import pytest

from app.domain import backtests as engine
from app.domain.algorithms import (
    BULL_CALL_SPREAD,
    LEGACY_BULL_CALL_SPREAD,
    PLATFORM_DEMO,
    WEIGHTED_BULL_CALL_SPREAD,
    ScoreModel,
)
from app.domain.backtests import MarketCandle, evaluate_chronologically


def candles(count: int = 32) -> list[MarketCandle]:
    base = datetime.fromisoformat("2026-01-02T09:15:00+05:30")
    values = [100 + index for index in range(count)]
    return [
        MarketCandle(
            timestamp=base + timedelta(minutes=index * 5),
            high=value + 1,
            close=value,
            futures_vwap=value - 1,
            india_vix=18,
            previous_india_vix=20,
        )
        for index, value in enumerate(values)
    ]


def test_window_and_previous_session_rejections_are_recorded() -> None:
    parameters = BULL_CALL_SPREAD.templates[0].parameters
    decisions = list(evaluate_chronologically(candles(), BULL_CALL_SPREAD.configure(parameters)))
    assert decisions[0].rejection_reason == "OUTSIDE_ENTRY_WINDOW"
    assert any(item.rejection_reason == "PDH_UNAVAILABLE" for item in decisions)
    assert all(item.decision in {"ACCEPTED", "REJECTED"} for item in decisions)


def test_unsorted_data_is_rejected_without_using_future_candles() -> None:
    values = candles(2)
    with pytest.raises(ValueError, match="strictly chronological"):
        list(
            evaluate_chronologically(
                reversed(values),
                BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters),
            )
        )


def test_platform_demo_emits_a_deterministic_synthetic_sequence() -> None:
    configured = PLATFORM_DEMO.configure({"signal_every_n_candles": 2, "hold_candles": 2})

    first = list(evaluate_chronologically(candles(7), configured))
    second = list(evaluate_chronologically(iter(candles(7)), configured))

    assert first == second
    assert [item.decision for item in first] == [
        "REJECTED",
        "ACCEPTED",
        "REJECTED",
        "REJECTED",
        "REJECTED",
        "ACCEPTED",
        "REJECTED",
    ]
    assert [item.rejection_reason for item in first] == [
        "DEMO_INTERVAL_NOT_REACHED",
        None,
        "DEMO_HOLD_ACTIVE",
        "DEMO_HOLD_ACTIVE",
        "DEMO_INTERVAL_NOT_REACHED",
        None,
        "DEMO_HOLD_ACTIVE",
    ]
    assert all(item.window == "platform_demo" for item in first)
    assert all(item.inputs["synthetic"] is True for item in first)
    assert all(item.inputs["financial_pnl_available"] is False for item in first)


def test_platform_demo_resets_fixture_state_for_each_session() -> None:
    configured = PLATFORM_DEMO.configure({"signal_every_n_candles": 2, "hold_candles": 5})
    first_session = candles(2)
    next_session = [
        MarketCandle(
            timestamp=value.timestamp + timedelta(days=1),
            high=value.high,
            close=value.close,
            futures_vwap=None,
            india_vix=None,
        )
        for value in candles(2)
    ]

    decisions = list(evaluate_chronologically(first_session + next_session, configured))

    assert [item.decision for item in decisions] == [
        "REJECTED",
        "ACCEPTED",
        "REJECTED",
        "ACCEPTED",
    ]
    assert [item.inputs["session_candle_number"] for item in decisions] == [1, 2, 1, 2]


def test_platform_demo_cancellation_does_not_consume_future_candles() -> None:
    consumed = 0

    def source():
        nonlocal consumed
        for value in candles(5):
            consumed += 1
            yield value

    checks = 0

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 2

    decisions = list(
        evaluate_chronologically(
            source(),
            PLATFORM_DEMO.configure({"signal_every_n_candles": 1, "hold_candles": 0}),
            should_cancel=should_cancel,
        )
    )

    assert len(decisions) == consumed == 2


def test_v12_and_weighted_code_versions_remain_distinct() -> None:
    v1 = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
    weighted = WEIGHTED_BULL_CALL_SPREAD.configure(
        WEIGHTED_BULL_CALL_SPREAD.templates[0].parameters
    )
    history = candles(35)
    v1_decisions = list(evaluate_chronologically(history, v1))
    weighted_decisions = list(evaluate_chronologically(history, weighted))
    assert [item.inputs["score_model"] for item in v1_decisions] != [
        item.inputs["score_model"] for item in weighted_decisions
    ]


def test_signal_gates_and_acceptance_are_explicit() -> None:
    base = datetime.fromisoformat("2026-01-02T09:30:00+05:30")
    first = [
        MarketCandle(base + timedelta(minutes=index * 5), 110, 100 + index, 99, 18, 20)
        for index in range(28)
    ]
    next_day = [
        MarketCandle(base.replace(day=3), 130, 131, 120, 18, 20),
        MarketCandle(base.replace(day=3) + timedelta(minutes=5), 130, 100, 120, 18, 20),
        MarketCandle(base.replace(day=3) + timedelta(minutes=10), 130, 131, 120, 25, 20),
    ]
    decisions = list(
        evaluate_chronologically(
            first + next_day, BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
        )
    )
    assert decisions[-3].decision == "ACCEPTED"
    assert decisions[-2].rejection_reason == "PDH_GATE_FAILED"
    assert decisions[-2].score is None
    assert decisions[-1].rejection_reason == "VIX_GATE_FAILED"


def boundary_history(*, india_vix: float = 19.5, previous_vix: float = 20) -> list[MarketCandle]:
    first_session = datetime.fromisoformat("2026-01-02T09:30:00+05:30")
    warmup = [
        MarketCandle(
            first_session + timedelta(minutes=index * 5),
            100,
            100,
            99,
            18,
            previous_vix,
        )
        for index in range(28)
    ]
    return warmup + [
        MarketCandle(
            datetime.fromisoformat("2026-01-03T09:30:00+05:30"),
            110,
            110,
            105,
            india_vix,
            previous_vix,
        )
    ]


def test_v1_rsi_and_vix_equality_boundaries(monkeypatch) -> None:
    configured = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
    monkeypatch.setattr(engine.WilderRsi, "update", lambda _self, _close: 58.0)

    exactly_rsi = list(evaluate_chronologically(boundary_history(india_vix=19.5), configured))[-1]
    assert exactly_rsi.inputs["rsi_confirmation_passed"] is False
    assert exactly_rsi.score == 3
    assert exactly_rsi.decision == "ACCEPTED"

    monkeypatch.setattr(engine, "_rsi", lambda _closes, _period: 59.0)
    vix_equal_limit = list(
        evaluate_chronologically(boundary_history(india_vix=22.0, previous_vix=24), configured)
    )[-1]
    vix_above_limit = list(evaluate_chronologically(boundary_history(india_vix=22.1), configured))[
        -1
    ]
    assert vix_equal_limit.decision == "ACCEPTED"
    assert vix_above_limit.rejection_reason == "VIX_GATE_FAILED"


def test_v12_rejects_each_price_gate_before_scoring(monkeypatch) -> None:
    monkeypatch.setattr(engine, "_rsi", lambda _closes, _period: 59.0)
    configured = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)

    pdh_failure = boundary_history()
    pdh_failure[-1] = MarketCandle(pdh_failure[-1].timestamp, 110, 100, 99, 19, 20)
    pdh_decision = list(evaluate_chronologically(pdh_failure, configured))[-1]
    assert pdh_decision.rejection_reason == "PDH_GATE_FAILED"
    assert pdh_decision.score is None

    vwap_failure = boundary_history()
    vwap_failure[-1] = MarketCandle(vwap_failure[-1].timestamp, 110, 110, 110, 19, 20)
    vwap_decision = list(evaluate_chronologically(vwap_failure, configured))[-1]
    assert vwap_decision.rejection_reason == "FUTURES_VWAP_GATE_FAILED"
    assert vwap_decision.score is None


def test_v12_price_gates_run_before_confirmation_warmup() -> None:
    configured = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
    history = [
        MarketCandle(
            datetime.fromisoformat("2026-01-02T15:30:00+05:30"),
            100,
            100,
            99,
            18,
            20,
        ),
        MarketCandle(
            datetime.fromisoformat("2026-01-05T09:30:00+05:30"),
            90,
            90,
            89,
            18,
            20,
        ),
    ]

    decision = list(evaluate_chronologically(history, configured))[-1]
    assert decision.inputs["rsi"] is None
    assert decision.rejection_reason == "PDH_GATE_FAILED"
    assert decision.score is None


@pytest.mark.parametrize(
    ("rsi", "current_vix", "expected_score", "expected_decision"),
    [
        (58.0, 20.0, 0, "REJECTED"),
        (59.0, 20.0, 2, "REJECTED"),
        (58.0, 19.0, 3, "ACCEPTED"),
        (59.0, 19.0, 5, "ACCEPTED"),
    ],
)
def test_v12_confirmation_scores(
    monkeypatch, rsi: float, current_vix: float, expected_score: int, expected_decision: str
) -> None:
    monkeypatch.setattr(engine.WilderRsi, "update", lambda _self, _close: rsi)
    configured = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
    decision = list(evaluate_chronologically(boundary_history(india_vix=current_vix), configured))[
        -1
    ]
    assert decision.score == expected_score
    assert decision.decision == expected_decision


def test_legacy_model_preserves_documented_ten_point_weights(monkeypatch) -> None:
    monkeypatch.setattr(engine, "_rsi", lambda _closes, _period: 59.0)
    legacy_parameters = {
        **LEGACY_BULL_CALL_SPREAD.templates[1].parameters,
        "score_model": ScoreModel.LEGACY_WEIGHTED_10.value,
    }
    decision = list(
        evaluate_chronologically(
            boundary_history(india_vix=19.9), LEGACY_BULL_CALL_SPREAD.configure(legacy_parameters)
        )
    )[-1]

    assert decision.score == 10
    assert decision.decision == "ACCEPTED"


def test_invalid_market_time_and_previous_vix_are_rejected() -> None:
    configured = BULL_CALL_SPREAD.configure(BULL_CALL_SPREAD.templates[0].parameters)
    invalid_time = candles(1)[0]
    invalid_time = MarketCandle(
        invalid_time.timestamp.replace(minute=17),
        invalid_time.high,
        invalid_time.close,
        invalid_time.futures_vwap,
        invalid_time.india_vix,
        invalid_time.previous_india_vix,
    )
    with pytest.raises(ValueError, match="completed five-minute"):
        list(evaluate_chronologically([invalid_time], configured))

    values = boundary_history()
    values[-1] = MarketCandle(
        values[-1].timestamp,
        values[-1].high,
        values[-1].close,
        values[-1].futures_vwap,
        values[-1].india_vix,
        0,
    )
    assert list(evaluate_chronologically(values, configured))[-1].rejection_reason == (
        "PREVIOUS_VIX_INVALID"
    )
