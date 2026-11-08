"""Record comparisons made by the engine, without changing its decisions."""

from typing import Any


def check(
    key: str,
    label: str,
    observed: Any,
    comparator: str | None,
    threshold: Any,
    passed: bool | None,
    *,
    reason: str | None = None,
    points: int = 0,
    evidence: dict[str, Any] | None = None,
    kind: str = "gate",
) -> dict[str, Any]:
    return {
        "step_id": key,
        "logic_step_id": key,
        "order": 0,
        "kind": kind,
        "label": label,
        "status": "not_evaluated" if passed is None else "passed" if passed else "failed",
        "observed_value": observed,
        "comparator": comparator,
        "threshold_value": threshold,
        "unit": None,
        "score_contribution": points,
        "reason_code": reason,
        "evidence": evidence or {},
        "experimental": False,
    }


def _hard_gate_signal_checks(decision: Any, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    inputs = decision.inputs
    reason = decision.rejection_reason
    schedule_failed = reason in {"OUTSIDE_ENTRY_WINDOW", "EVALUATION_INTERVAL_NOT_REACHED"}
    input_failed = reason in {
        "PDH_UNAVAILABLE",
        "RSI_WARMUP",
        "REQUIRED_MARKET_INPUT_MISSING",
        "PREVIOUS_VIX_INVALID",
    }
    market_ready = not schedule_failed and not input_failed
    pdh_evaluated = market_ready
    pdh_passed = inputs.get("pdh_gate_passed") if pdh_evaluated else None
    vwap_evaluated = pdh_passed is True
    vwap_passed = inputs.get("futures_vwap_gate_passed") if vwap_evaluated else None
    confirmation_evaluated = vwap_passed is True
    vix_gate_passed = inputs.get("vix_gate_passed") if confirmation_evaluated else None
    score_checked = confirmation_evaluated and vix_gate_passed is True

    steps = [
        check(
            "entry_window",
            "Completed candle is in an enabled entry window",
            decision.timestamp.strftime("%H:%M:%S"),
            "inside",
            decision.window,
            decision.window is not None,
            evidence={
                key: value for key, value in parameters.items() if key.startswith("entry_window")
            },
        ),
        check(
            "evaluation_interval",
            "Scheduled evaluation interval",
            decision.timestamp.strftime("%H:%M:%S"),
            "every N minutes from window start",
            parameters.get("evaluation_interval_minutes"),
            None if decision.window is None else reason != "EVALUATION_INTERVAL_NOT_REACHED",
            reason=reason if schedule_failed else None,
        ),
        check(
            "market_input",
            "Required completed market inputs are available",
            None,
            None,
            None,
            None if schedule_failed else not input_failed,
            reason=reason if input_failed else None,
            evidence={
                key: inputs.get(key)
                for key in (
                    "close",
                    "previous_session_high",
                    "futures_vwap",
                    "rsi",
                    "india_vix",
                    "previous_india_vix",
                )
            },
        ),
        check(
            "pdh_gate",
            "Spot close is strictly above previous session high",
            inputs.get("close"),
            ">",
            inputs.get("previous_session_high"),
            pdh_passed,
            reason="PDH_GATE_FAILED" if reason == "PDH_GATE_FAILED" else None,
            evidence={"stored_result_field": "pdh_gate_passed", "mandatory": True},
        ),
        check(
            "vwap_gate",
            "Spot close is strictly above futures VWAP",
            inputs.get("close"),
            ">",
            inputs.get("futures_vwap"),
            vwap_passed,
            reason=("FUTURES_VWAP_GATE_FAILED" if reason == "FUTURES_VWAP_GATE_FAILED" else None),
            evidence={"stored_result_field": "futures_vwap_gate_passed", "mandatory": True},
        ),
        check(
            "vix_safety",
            "India VIX is at or below maximum",
            inputs.get("india_vix"),
            "<=",
            parameters.get("vix_maximum"),
            vix_gate_passed,
            reason="VIX_GATE_FAILED" if reason == "VIX_GATE_FAILED" else None,
        ),
        check(
            "rsi_confirmation",
            "RSI is strictly above threshold",
            inputs.get("rsi"),
            ">",
            parameters.get("rsi_threshold"),
            inputs.get("rsi_confirmation_passed") if confirmation_evaluated else None,
            points=(
                int(parameters.get("rsi_weight", 0))
                if confirmation_evaluated and inputs.get("rsi_confirmation_passed")
                else 0
            ),
            kind="confirmation",
            evidence={"weight": parameters.get("rsi_weight")},
        ),
        check(
            "vix_confirmation",
            "Current VIX is below the previous-session close",
            inputs.get("india_vix"),
            "<",
            inputs.get("previous_india_vix"),
            inputs.get("vix_confirmation_passed") if confirmation_evaluated else None,
            points=(
                int(parameters.get("vix_weight", 0))
                if confirmation_evaluated and inputs.get("vix_confirmation_passed")
                else 0
            ),
            kind="confirmation",
            evidence={
                "weight": parameters.get("vix_weight"),
                "vix_decline_percent": inputs.get("vix_decline_percent"),
            },
        ),
        check(
            "confirmation_score",
            "RSI and VIX confirmation score meets minimum",
            decision.score,
            ">=",
            parameters.get("minimum_score"),
            None if not score_checked else reason != "CONFIRMATION_SCORE_TOO_LOW",
            kind="confirmation",
            reason=(
                "CONFIRMATION_SCORE_TOO_LOW" if reason == "CONFIRMATION_SCORE_TOO_LOW" else None
            ),
            evidence={
                "formula": "RSI points + VIX points",
                "maximum_score": int(parameters.get("rsi_weight", 0))
                + int(parameters.get("vix_weight", 0)),
                "score_computed": decision.score is not None,
            },
        ),
    ]
    execution = inputs.get("execution_rejection")
    steps.append(
        check(
            "contract_selection",
            "Execution eligibility and weekly spread selection",
            decision.decision,
            None,
            None,
            False if execution is not None else None,
            reason=reason
            if execution is not None
            else "See the linked trade and execution journal"
            if decision.decision == "ACCEPTED"
            else reason,
            kind="decision",
            evidence=execution or {},
        )
    )
    for index, attempt in enumerate((execution or {}).get("attempts", []), start=1):
        for recorded in attempt.get("checks", []):
            steps.append(
                {
                    **recorded,
                    "step_id": f"spread_{index}_{recorded['step_id']}",
                    "evidence": {
                        **recorded.get("evidence", {}),
                        **{key: value for key, value in attempt.items() if key != "checks"},
                    },
                }
            )
    for order, step in enumerate(steps, start=1):
        step["order"] = order
        step["step_id"] = f"{step['step_id']}:{order}"
    return steps


def signal_checks(decision: Any, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    inputs = decision.inputs
    if inputs.get("score_model") == "hard_gates_confirmation_5":
        return _hard_gate_signal_checks(decision, parameters)
    reason = decision.rejection_reason
    schedule_failed = reason in {"OUTSIDE_ENTRY_WINDOW", "EVALUATION_INTERVAL_NOT_REACHED"}
    input_failed = reason in {
        "PDH_UNAVAILABLE",
        "RSI_WARMUP",
        "REQUIRED_MARKET_INPUT_MISSING",
        "PREVIOUS_VIX_INVALID",
    }
    scored = "pdh_gate_passed" in inputs
    steps = [
        check(
            "entry_window",
            "Completed candle is in an enabled entry window",
            decision.timestamp.strftime("%H:%M:%S"),
            "inside",
            decision.window,
            decision.window is not None,
            evidence={
                key: value for key, value in parameters.items() if key.startswith("entry_window")
            },
        ),
        check(
            "evaluation_interval",
            "Scheduled evaluation interval",
            decision.timestamp.strftime("%H:%M:%S"),
            "every N minutes from window start",
            parameters.get("evaluation_interval_minutes"),
            None if decision.window is None else reason != "EVALUATION_INTERVAL_NOT_REACHED",
            reason=reason if schedule_failed else None,
        ),
        check(
            "market_input",
            "Required completed market inputs are available",
            None,
            None,
            None,
            None if schedule_failed else not input_failed,
            reason=reason if input_failed else None,
            evidence={
                key: inputs.get(key)
                for key in (
                    "close",
                    "previous_session_high",
                    "futures_vwap",
                    "rsi",
                    "india_vix",
                    "previous_india_vix",
                )
            },
        ),
    ]
    for key, label, observed, operator, threshold, flag, weight in (
        (
            "vix_safety",
            "India VIX is at or below maximum",
            "india_vix",
            "<=",
            parameters.get("vix_maximum"),
            "vix_gate_passed",
            None,
        ),
        (
            "price_breakout",
            "Spot close is above previous session high",
            "close",
            ">",
            inputs.get("previous_session_high"),
            "pdh_gate_passed",
            "pdh_weight",
        ),
        (
            "institutional_bias",
            "Spot close is above futures VWAP",
            "close",
            ">",
            inputs.get("futures_vwap"),
            "futures_vwap_gate_passed",
            "vwap_weight",
        ),
        (
            "momentum",
            "RSI is above threshold",
            "rsi",
            ">",
            parameters.get("rsi_threshold"),
            "rsi_confirmation_passed",
            "rsi_weight",
        ),
        (
            "vix_momentum",
            "VIX decline exceeds minimum percentage",
            "vix_decline_percent",
            ">",
            parameters.get("minimum_vix_decline_percent"),
            "vix_confirmation_passed",
            "vix_weight",
        ),
    ):
        passed = inputs.get(flag) if scored else None
        steps.append(
            check(
                key,
                label,
                inputs.get(observed),
                operator,
                threshold,
                passed,
                points=int(parameters.get(weight, 0)) if weight and passed else 0,
                kind="confirmation" if weight else "gate",
                reason=reason if not scored else None,
                evidence={
                    "stored_result_field": flag,
                    "weight": parameters.get(weight) if weight else None,
                    **(
                        {
                            "formula": "(previous VIX close - current VIX) / previous VIX close * 100",
                            "previous_vix_close": inputs.get("previous_india_vix"),
                            "current_vix": inputs.get("india_vix"),
                            "result": inputs.get("vix_decline_percent"),
                        }
                        if key == "vix_momentum"
                        else {}
                    ),
                },
            )
        )
    # Score is computed before the VIX guard, but its threshold is only checked after it.
    score_checked = scored and inputs.get("vix_gate_passed") is True
    steps.append(
        check(
            "weighted_score",
            "Weighted score meets minimum",
            decision.score if scored else None,
            ">=",
            parameters.get("minimum_score"),
            None if not score_checked else reason != "CONFIRMATION_SCORE_TOO_LOW",
            kind="confirmation",
            reason=reason if not score_checked or reason == "CONFIRMATION_SCORE_TOO_LOW" else None,
            evidence={
                "formula": "PDH points + VWAP points + RSI points + VIX points",
                "maximum_score": sum(
                    int(parameters.get(key, 0))
                    for key in ("pdh_weight", "vwap_weight", "rsi_weight", "vix_weight")
                ),
                "score_computed": scored,
            },
        )
    )
    execution = inputs.get("execution_rejection")
    steps.append(
        check(
            "contract_selection",
            "Execution eligibility and weekly spread selection",
            decision.decision,
            None,
            None,
            False if execution is not None else None,
            reason=reason
            if execution is not None
            else "See the linked trade and execution journal"
            if decision.decision == "ACCEPTED"
            else reason,
            kind="decision",
            evidence=execution or {},
        )
    )
    for index, attempt in enumerate((execution or {}).get("attempts", []), start=1):
        for recorded in attempt.get("checks", []):
            steps.append(
                {
                    **recorded,
                    "step_id": f"spread_{index}_{recorded['step_id']}",
                    "evidence": {
                        **recorded.get("evidence", {}),
                        **{k: v for k, v in attempt.items() if k != "checks"},
                    },
                }
            )
    for order, step in enumerate(steps, start=1):
        step["order"] = order
        step["step_id"] = f"{step['step_id']}:{order}"
    return steps
