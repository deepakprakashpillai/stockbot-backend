"""Characterize existing identities before Model 2 enters the runtime registry."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from app.domain.algorithms import ALGORITHM_REGISTRY, ALGORITHM_VERSION_REGISTRY, PLATFORM_DEMO
from app.domain.backtests import ENGINE_VERSION, MarketCandle, evaluate_chronologically
from app.domain.demo_execution import PlatformDemoExecutionAdapter

BASELINE = Path(__file__).parent / "fixtures/model2/legacy_compatibility_v1.json"


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _market_fixture() -> list[MarketCandle]:
    """Three fixed sessions spanning the weekend; no provider/files required."""
    candles = []
    exchange = timezone(timedelta(hours=5, minutes=30))
    for session_index, session_date in enumerate(
        (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    ):
        first = datetime.combine(session_date, time(9, 20), exchange)
        for bar in range(75):
            timestamp = first + timedelta(minutes=5 * bar)
            close = 25000.0 + session_index * 100.0 + bar * 0.5
            candles.append(
                MarketCandle(
                    timestamp=timestamp,
                    high=close + 0.25,
                    close=close,
                    futures_vwap=24990.0 + session_index * 100.0,
                    futures_close=close + 10.0,
                    india_vix=15.0 - session_index * 0.1,
                    previous_india_vix=16.0,
                    futures_observed_at=timestamp,
                    india_vix_observed_at=timestamp,
                )
            )
    return candles


def _snapshot(
    identities: set[str] | None = None, active_keys: set[str] | None = None
) -> dict[str, Any]:
    versions = {}
    for (key, code_version), definition in sorted(ALGORITHM_VERSION_REGISTRY.items()):
        if identities is not None and f"{key}@{code_version}" not in identities:
            continue
        parameters = definition.parameter_model.model_validate(definition.templates[0].parameters)
        configured = definition.configure(parameters.model_dump(mode="json"))
        decisions = list(evaluate_chronologically(_market_fixture(), configured))
        result = {
            "active": definition.active,
            "schema_version": definition.schema_version,
            "supported_modes": list(definition.supported_modes),
            "required_capabilities": list(definition.required_capabilities),
            "default_parameters": parameters.model_dump(mode="json"),
            "parameter_schema_sha256": _fingerprint(definition.parameter_model.model_json_schema()),
            "fixed_rules_sha256": _fingerprint(definition.fixed_rules),
            "documentation_sha256": _fingerprint(asdict(definition.documentation)),
            "templates_sha256": _fingerprint([asdict(item) for item in definition.templates]),
            "decision_count": len(decisions),
            "accepted_count": sum(item.decision == "ACCEPTED" for item in decisions),
            "reasons": dict(Counter(item.rejection_reason or "ACCEPTED" for item in decisions)),
            "decisions_sha256": _fingerprint([asdict(item) for item in decisions]),
            "template_decisions": {
                template.key: _fingerprint(
                    [
                        asdict(item)
                        for item in evaluate_chronologically(
                            _market_fixture(), definition.configure(template.parameters)
                        )
                    ]
                )
                for template in definition.templates
            },
        }
        if key == "platform_demo":
            execution = PlatformDemoExecutionAdapter().execute(
                decisions, hold_candles=int(parameters.model_dump()["hold_candles"])
            )
            result["execution_sha256"] = _fingerprint(asdict(execution))
            result["metrics"] = execution.metrics()
        versions[f"{key}@{code_version}"] = result
    return {
        "engine_version": ENGINE_VERSION,
        "active_registry": {
            key: value.code_version
            for key, value in ALGORITHM_REGISTRY.items()
            if active_keys is None or key in active_keys
        },
        "versions": versions,
    }


def test_existing_versions_preserve_parameters_decisions_and_demo_results() -> None:
    expected = json.loads(BASELINE.read_text())["snapshot"]
    assert _snapshot(set(expected["versions"]), set(expected["active_registry"])) == expected


def test_baseline_fixture_exercises_existing_acceptances() -> None:
    """Avoid freezing a useless fixture in which every strategy rejects everything."""
    baseline = json.loads(BASELINE.read_text())["snapshot"]
    assert all(item["decision_count"] == 225 for item in baseline["versions"].values())
    assert all(item["accepted_count"] > 0 for item in baseline["versions"].values())
    demo = baseline["versions"]["platform_demo@1.0.0-platform-demo"]["metrics"]
    assert demo["total_trades"] > 0
    assert demo["synthetic"] is True
    assert demo["financial_pnl_available"] is False


def test_an_additional_algorithm_does_not_require_replacing_legacy_fingerprints(
    monkeypatch,
) -> None:
    expected = json.loads(BASELINE.read_text())["snapshot"]
    added = replace(PLATFORM_DEMO, key="additional_algorithm_fixture")
    monkeypatch.setitem(ALGORITHM_REGISTRY, added.key, added)
    monkeypatch.setitem(ALGORITHM_VERSION_REGISTRY, (added.key, added.code_version), added)
    assert _snapshot(set(expected["versions"]), set(expected["active_registry"])) == expected
