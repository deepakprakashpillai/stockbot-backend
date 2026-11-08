"""Versioned, decision-time evidence for signal inspection.

These adapters describe an already-made ``SignalDecision``.  They never decide
whether to accept a signal and intentionally stay independent of FastAPI,
SQLAlchemy, Celery, and frontend presentation code.
"""

from __future__ import annotations

from typing import Any

from app.domain.algorithms import AlgorithmDefinition
from app.domain.backtests import SignalDecision
from app.domain.decision_checks import signal_checks

SCHEMA_VERSION = "signal-diagnostic-v2"


def _context(key: str, label: str, value: Any, unit: str | None, source: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "exact_value": value,
        "display_unit": unit,
        "source": source,
        "available": value is not None,
    }


def _step(
    step_id: str,
    order: int,
    kind: str,
    label: str,
    status: str,
    *,
    observed: Any = None,
    comparator: str | None = None,
    threshold: Any = None,
    unit: str | None = None,
    contribution: int = 0,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
    experimental: bool = True,
) -> dict[str, Any]:
    return {
        "step_id": f"{step_id}:{order}",
        "logic_step_id": step_id,
        "order": order,
        "kind": kind,
        "label": label,
        "status": status,
        "observed_value": observed,
        "comparator": comparator,
        "threshold_value": threshold,
        "unit": unit,
        "score_contribution": contribution,
        "reason_code": reason,
        "evidence": evidence or {},
        "experimental": experimental,
    }


def _not_evaluated(step_id: str, order: int, kind: str, label: str, reason: str) -> dict[str, Any]:
    return _step(
        step_id,
        order,
        kind,
        label,
        "not_evaluated",
        reason=reason,
        evidence={"blocked_by": reason},
    )


def _platform_demo(decision: SignalDecision, parameters: dict[str, Any]) -> dict[str, Any]:
    inputs = decision.inputs
    reason = decision.rejection_reason or "ACCEPTED"
    fixture_status = "passed" if decision.decision == "ACCEPTED" else "failed"
    return {
        "market_context": (
            _context("close", "Completed candle close", inputs.get("close"), None, "dataset"),
            _context("high", "Completed candle high", inputs.get("high"), None, "dataset"),
            _context(
                "session_candle_number",
                "Session candle number",
                inputs.get("session_candle_number"),
                "candles",
                "engine",
            ),
        ),
        "evaluation_steps": (
            _step(
                "candle_input",
                1,
                "input",
                "Read completed candles",
                "passed",
                observed=inputs.get("completed_candle"),
                comparator="is",
                threshold=True,
                evidence={"synthetic": True},
            ),
            _step(
                "fixture_decision",
                2,
                "decision",
                "Apply deterministic interval",
                fixture_status,
                observed=inputs.get("session_candle_number"),
                comparator="every_n_candles",
                threshold=parameters.get("signal_every_n_candles"),
                unit="candles",
                contribution=0 if decision.score is None else decision.score,
                reason=reason,
                evidence={
                    "hold_candles": inputs.get("hold_candles"),
                    "remaining_hold_candles_before_decision": inputs.get(
                        "remaining_hold_candles_before_decision"
                    ),
                },
            ),
            _step(
                "demo_output",
                3,
                "output",
                "Return synthetic platform evidence",
                "informational",
                observed=decision.decision,
                evidence={"synthetic": True, "financial_pnl_available": False},
            ),
        ),
        "limitations": ("PLATFORM_DEMO is synthetic and uses DEMO_UNITS, not financial P&L.",),
    }


def _bull_call_spread(decision: SignalDecision, parameters: dict[str, Any]) -> dict[str, Any]:
    inputs = decision.inputs
    reason = decision.rejection_reason or "ACCEPTED"
    return {
        "market_context": (
            _context("close", "Completed candle close", inputs.get("close"), None, "dataset"),
            _context("rsi", "RSI (14 Wilder/RMA)", inputs.get("rsi"), "RSI", "engine"),
        ),
        "evaluation_steps": (
            _step(
                "legacy_research_decision",
                1,
                "decision",
                "Replay legacy signal research logic",
                "passed" if decision.decision == "ACCEPTED" else "failed",
                observed=decision.decision,
                reason=reason,
                evidence={
                    "score_model": inputs.get("score_model") or parameters.get("score_model"),
                    "score": decision.score,
                },
            ),
        ),
        "limitations": (
            "Legacy signal-only research is retained for historical readability and is not option-spread P&L.",
        ),
    }


def _bull_call_spread_v1(decision: SignalDecision, parameters: dict[str, Any]) -> dict[str, Any]:
    inputs = decision.inputs
    contexts = (
        _context("close", "NIFTY spot close", inputs.get("close"), None, "spot_dataset"),
        _context(
            "previous_session_high",
            "Previous session high (PDH)",
            inputs.get("previous_session_high"),
            None,
            "spot_dataset",
        ),
        _context(
            "futures_vwap",
            "NIFTY futures VWAP",
            inputs.get("futures_vwap"),
            None,
            "futures_dataset",
        ),
        _context("india_vix", "India VIX", inputs.get("india_vix"), None, "vix_dataset"),
        _context(
            "previous_india_vix",
            "Previous-session VIX close",
            inputs.get("previous_india_vix"),
            None,
            "vix_dataset",
        ),
        _context(
            "rsi",
            f"RSI ({parameters.get('rsi_period', 14)} Wilder/RMA)",
            inputs.get("rsi"),
            "RSI",
            "engine",
        ),
        _context(
            "vix_decline_percent",
            "India VIX decline",
            inputs.get("vix_decline_percent"),
            "%",
            "engine",
        ),
    )
    return {
        "evidence_version": "bull-call-spread-checks-v2",
        "market_context": contexts,
        "evaluation_steps": signal_checks(decision, parameters),
        "limitations": (
            "Historical option fills use candle close plus configured adverse slippage.",
            "Executable bid/ask quotes, liquidity filters, and non-monthly events are deferred.",
        ),
    }


def build_signal_diagnostic(
    decision: SignalDecision,
    *,
    definition: AlgorithmDefinition,
    parameter_version_id: str,
    parameter_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return immutable evidence for one current evaluation schema."""

    if definition.key == "platform_demo":
        adapter = _platform_demo(decision, parameter_snapshot)
    elif definition.code_version in {
        "1.0.0-bull-call-spread-v1",
        "1.1.0-bull-call-spread-v1",
        "1.2.0-bull-call-spread-v1",
    }:
        adapter = _bull_call_spread_v1(decision, parameter_snapshot)
    else:
        adapter = _bull_call_spread(decision, parameter_snapshot)
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_key": definition.key,
        "code_version": definition.code_version,
        "parameter_version_id": parameter_version_id,
        "algorithm_logic_document_version": definition.documentation.diagnostic_schema_version,
        "decision": decision.decision,
        "primary_reason": decision.rejection_reason or "ACCEPTED",
        "window": decision.window,
        "score": decision.score,
        "parameter_snapshot": parameter_snapshot,
        "related_trade_id": None,
        **adapter,
    }
