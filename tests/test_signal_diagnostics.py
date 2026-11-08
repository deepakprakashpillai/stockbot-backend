from datetime import date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from starlette.requests import Request

from app.api.v1 import backtests as routes
from app.domain import backtests as engine
from app.domain.algorithms import (
    BULL_CALL_SPREAD,
    LEGACY_BULL_CALL_SPREAD,
    PLATFORM_DEMO,
    ScoreModel,
)
from app.domain.backtests import MarketCandle, SignalDecision, evaluate_chronologically
from app.domain.signal_diagnostics import SCHEMA_VERSION, build_signal_diagnostic


def _decision(*, accepted: bool, reason: str | None, score: int) -> SignalDecision:
    return SignalDecision(
        timestamp=datetime.fromisoformat("2026-01-02T09:30:00+05:30"),
        session_date=date(2026, 1, 2),
        decision="ACCEPTED" if accepted else "REJECTED",
        rejection_reason=reason,
        window="platform_demo",
        score=score,
        inputs={
            "close": 101,
            "high": 102,
            "session_candle_number": 2,
            "signal_every_n_candles": 2,
            "hold_candles": 1,
            "remaining_hold_candles_before_decision": 0,
            "completed_candle": True,
            "synthetic": True,
            "financial_pnl_available": False,
        },
    )


def _history() -> list[MarketCandle]:
    first = datetime.fromisoformat("2026-01-02T09:30:00+05:30")
    return [
        MarketCandle(first + timedelta(minutes=5 * index), 100, 100, 99, 18, 20)
        for index in range(28)
    ] + [MarketCandle(datetime.fromisoformat("2026-01-03T09:30:00+05:30"), 110, 110, 105, 18, 20)]


def _diagnostic(
    decision: SignalDecision, definition, parameters: dict[str, object]
) -> dict[str, object]:
    return build_signal_diagnostic(
        decision,
        definition=definition,
        parameter_version_id="version-1",
        parameter_snapshot=parameters,
    )


def test_platform_demo_diagnostic_is_versioned_and_score_preserving() -> None:
    parameters = {"signal_every_n_candles": 2, "hold_candles": 1}
    diagnostic = _diagnostic(
        _decision(accepted=True, reason=None, score=1), PLATFORM_DEMO, parameters
    )

    assert diagnostic["schema_version"] == SCHEMA_VERSION
    assert diagnostic["primary_reason"] == "ACCEPTED"
    steps = diagnostic["evaluation_steps"]
    assert [step["logic_step_id"] for step in steps] == [
        "candle_input",
        "fixture_decision",
        "demo_output",
    ]
    assert sum(step["score_contribution"] for step in steps) == diagnostic["score"]


def test_v1_and_legacy_bull_call_versions_keep_separate_diagnostics(monkeypatch) -> None:
    monkeypatch.setattr(engine, "_rsi", lambda _closes, _period: 59.0)
    v1_parameters = BULL_CALL_SPREAD.templates[0].parameters
    legacy_parameters = {
        **LEGACY_BULL_CALL_SPREAD.templates[1].parameters,
        "score_model": ScoreModel.LEGACY_WEIGHTED_10.value,
    }
    v1_decision = list(
        evaluate_chronologically(_history(), BULL_CALL_SPREAD.configure(v1_parameters))
    )[-1]
    legacy_decision = list(
        evaluate_chronologically(_history(), LEGACY_BULL_CALL_SPREAD.configure(legacy_parameters))
    )[-1]

    v1 = _diagnostic(v1_decision, BULL_CALL_SPREAD, v1_parameters)
    legacy = _diagnostic(legacy_decision, LEGACY_BULL_CALL_SPREAD, legacy_parameters)
    v1_steps = {step["logic_step_id"]: step for step in v1["evaluation_steps"]}
    legacy_steps = {step["logic_step_id"]: step for step in legacy["evaluation_steps"]}

    assert v1_steps["pdh_gate"]["status"] == "passed"
    assert v1_steps["vwap_gate"]["status"] == "passed"
    assert v1_steps["confirmation_score"]["status"] == "passed"
    assert v1_steps["confirmation_score"]["evidence"]["maximum_score"] == 5
    assert legacy_steps["legacy_research_decision"]["status"] == "passed"


def test_v1_score_failure_is_explained_without_an_entry(monkeypatch) -> None:
    monkeypatch.setattr(engine.WilderRsi, "update", lambda _self, _close: 58.0)
    values = _history()
    values[-1] = MarketCandle(values[-1].timestamp, 110, 110, 105, 20, 20)
    parameters = BULL_CALL_SPREAD.templates[0].parameters
    decision = list(evaluate_chronologically(values, BULL_CALL_SPREAD.configure(parameters)))[-1]
    diagnostic = _diagnostic(decision, BULL_CALL_SPREAD, parameters)
    steps = {step["logic_step_id"]: step for step in diagnostic["evaluation_steps"]}

    assert decision.rejection_reason == "CONFIRMATION_SCORE_TOO_LOW"
    assert steps["confirmation_score"]["status"] == "failed"
    assert steps["contract_selection"]["status"] == "not_evaluated"
    assert steps["pdh_gate"]["observed_value"] == 110
    assert steps["pdh_gate"]["threshold_value"] == 100
    assert steps["pdh_gate"]["status"] == "passed"
    assert steps["rsi_confirmation"]["observed_value"] == 58
    assert steps["rsi_confirmation"]["status"] == "failed"
    assert sum(step["score_contribution"] for step in steps.values()) == decision.score
    assert steps["vix_confirmation"]["threshold_value"] == 20


def test_vix_failure_does_not_claim_score_threshold_was_evaluated(monkeypatch) -> None:
    monkeypatch.setattr(engine, "_rsi", lambda _closes, _period: 59.0)
    values = _history()
    values[-1] = MarketCandle(values[-1].timestamp, 110, 110, 105, 23, 24)
    parameters = BULL_CALL_SPREAD.templates[0].parameters
    decision = list(evaluate_chronologically(values, BULL_CALL_SPREAD.configure(parameters)))[-1]
    diagnostic = _diagnostic(decision, BULL_CALL_SPREAD, parameters)
    steps = {step["logic_step_id"]: step for step in diagnostic["evaluation_steps"]}
    assert decision.rejection_reason == "VIX_GATE_FAILED"
    assert steps["vix_safety"]["status"] == "failed"
    assert steps["confirmation_score"]["status"] == "not_evaluated"
    assert steps["confirmation_score"]["observed_value"] == decision.score


def _request() -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "signal-diagnostic-test"
    return request


def test_detail_route_uses_persisted_diagnostic_and_legacy_fallback(monkeypatch) -> None:
    run_id, signal_id, dataset_id, algorithm_version_id = (uuid4(), uuid4(), uuid4(), uuid4())
    parameters = {"signal_every_n_candles": 2, "hold_candles": 1}
    decision = _decision(accepted=True, reason=None, score=1)
    signal = SimpleNamespace(
        id=signal_id,
        sequence=2,
        timestamp=decision.timestamp,
        session_date=decision.session_date,
        decision=decision.decision,
        rejection_reason=decision.rejection_reason,
        window=decision.window,
        score=decision.score,
        inputs=decision.inputs,
        diagnostic=_diagnostic(decision, PLATFORM_DEMO, parameters),
        entry_trades=[],
    )
    run = SimpleNamespace(
        id=run_id,
        dataset_version_id=dataset_id,
        algorithm_version_id=algorithm_version_id,
        mode="PLATFORM_DEMO",
        snapshot={
            "algorithm": {"key": "platform_demo", "code_version": "1.0.0-platform-demo"},
            "assumptions": {"synthetic": True, "unit": "DEMO_UNITS"},
        },
    )

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        def get_signal(self, requested_run_id, requested_signal_id):
            assert (requested_run_id, requested_signal_id) == (run_id, signal_id)
            return signal

        def get_run(self, requested_run_id):
            assert requested_run_id == run_id
            return run

        def signal_neighbors(self, requested_run_id, sequence):
            assert (requested_run_id, sequence) == (run_id, 2)
            return None, uuid4()

    monkeypatch.setattr(routes, "BacktestRepository", Repository)
    response = routes.get_signal_detail(run_id, signal_id, _request(), object())

    assert response.structured_explanation_available is True
    assert response.diagnostic is not None
    assert response.diagnostic.schema_version == SCHEMA_VERSION
    signal.diagnostic = None
    legacy = routes.get_signal_detail(run_id, signal_id, _request(), object())
    assert legacy.structured_explanation_available is False
    assert legacy.raw_evidence == decision.inputs
    signal.diagnostic = {"schema_version": "signal-diagnostic-v999"}
    unsupported = routes.get_signal_detail(run_id, signal_id, _request(), object())
    assert unsupported.structured_explanation_available is False
    assert unsupported.raw_evidence == decision.inputs
    linked = routes._signal_detail(
        signal,
        run,
        None,
        None,
        SimpleNamespace(
            timestamp=decision.timestamp,
            payload={"reason": "NET_DEBIT_CAP_EXCEEDED", "attempted_widths": [200, 250]},
        ),
    )
    assert linked.signal.decision == "ACCEPTED"
    assert linked.execution_rejection is not None
    assert linked.execution_rejection.reason == "NET_DEBIT_CAP_EXCEEDED"
    assert linked.execution_rejection.details == {"attempted_widths": [200, 250]}
