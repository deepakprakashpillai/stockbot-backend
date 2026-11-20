from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from itertools import product
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.algorithms import get_algorithm
from app.domain.model2.parameters import Model2Parameters, model2_presets
from app.domain.model2.specification import (
    ALGORITHM_KEY,
    ASSESSMENT_STATUS,
    FIXED_RULES,
    RULE_REFERENCES,
    SOURCES,
    specification_digest,
    specification_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/model2/specification-v1.4.json"
CASES = ROOT / "tests/fixtures/model2/acceptance_cases_v1.json"


@pytest.mark.parametrize(
    "width,slippage,filter_on", list(product((150, 200), (0, 1, 2), (False, True)))
)
def test_only_declared_parameter_combinations_round_trip(
    width: int, slippage: int, filter_on: bool
) -> None:
    parameters = Model2Parameters.model_validate(
        {
            "wing_width_points": width,
            "slippage_percent": slippage,
            "vix_state_filter_enabled": filter_on,
        }
    )
    assert parameters.model_dump(mode="json") == {
        "wing_width_points": width,
        "slippage_percent": f"{slippage}.00",
        "vix_state_filter_enabled": filter_on,
    }
    assert Model2Parameters.model_validate_json(parameters.model_dump_json()) == parameters
    assert parameters.experimental is filter_on


@pytest.mark.parametrize("value", [0, 100, 250, -150, True, 150.0, "200", None])
def test_unapproved_or_coerced_wing_width_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        Model2Parameters.model_validate({"wing_width_points": value})


@pytest.mark.parametrize("value", [-1, 0.5, 2.5, 100, "NaN", "Infinity", True, None])
def test_unapproved_or_nonfinite_slippage_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        Model2Parameters.model_validate({"slippage_percent": value})


@pytest.mark.parametrize("value", [0, 1, "false", "true", None])
def test_experimental_switch_requires_an_explicit_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        Model2Parameters.model_validate({"vix_state_filter_enabled": value})


@pytest.mark.parametrize(
    "field", ["lots", "k", "hard_risk_limit", "stop_loss", "exit_time", "reliability_denominator"]
)
def test_fixed_or_pending_rules_cannot_be_parameter_overrides(field: str) -> None:
    with pytest.raises(ValidationError):
        Model2Parameters.model_validate({field: 1})


def test_equivalent_decimal_inputs_have_one_snapshot_representation() -> None:
    snapshots = {
        Model2Parameters.model_validate({"slippage_percent": value}).model_dump_json()
        for value in (2, 2.0, "2", "2.000", Decimal("2.00000"))
    }
    assert len(snapshots) == 1


def test_parameters_and_presets_are_immutable_without_shared_mutable_values() -> None:
    primary, alternative = model2_presets()
    with pytest.raises(ValidationError, match="frozen"):
        primary.parameters.wing_width_points = 150
    with pytest.raises(FrozenInstanceError):
        primary.name = "Edited"
    snapshot = primary.parameters.model_dump(mode="json")
    snapshot["wing_width_points"] = 150
    assert model2_presets()[0].parameters.wing_width_points == 200
    assert alternative.parameters.wing_width_points == 150
    assert not primary.parameters.experimental
    assert not alternative.parameters.experimental
    assert primary.parameters.slippage_percent == alternative.parameters.slippage_percent == 2


def test_specification_is_frozen_and_snapshots_are_detached() -> None:
    with pytest.raises(TypeError):
        FIXED_RULES["lots"] = 3  # type: ignore[index]
    snapshot = specification_snapshot()
    snapshot["rules"]["lots"] = 3
    snapshot["source_precedence"].clear()
    assert specification_snapshot()["rules"]["lots"] == 1
    assert len(specification_snapshot()["source_precedence"]) == 3


def test_specification_matches_the_versioned_fingerprint_and_source_copies() -> None:
    payload = json.loads(CONTRACT.read_text())
    assert payload["specification"] == specification_snapshot()
    assert payload["specification_sha256"] == specification_digest()
    assert (
        CONTRACT.with_suffix(".sha256").read_text().strip()
        == hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    )
    for source in SOURCES:
        source_path = CONTRACT.parent / source.path
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source.sha256
    assert [source.id for source in SOURCES] == ["lifecycle", "round2", "original"]


def test_each_rule_has_a_source_and_a_frozen_future_acceptance_case() -> None:
    """Case coverage here is specification coverage, not an engine-test claim."""
    payload = json.loads(CASES.read_text())
    assert payload["engine_assertions_pending"] is True
    assert payload["specification_sha256"] == specification_digest()
    cases = {case["id"]: case for case in payload["cases"]}
    assert len(cases) == len(payload["cases"])
    expected = {case for rule in RULE_REFERENCES for case in rule.acceptance_cases}
    assert set(cases) == expected
    assert len({rule.id for rule in RULE_REFERENCES}) == len(RULE_REFERENCES)
    source_ids = {source.id for source in SOURCES}
    for rule in RULE_REFERENCES:
        assert rule.source_sections and rule.acceptance_cases
        for reference in rule.source_sections:
            assert reference.partition(":")[0] in source_ids
        for case_id in rule.acceptance_cases:
            assert cases[case_id]["input"] and cases[case_id]["expected"]
            assert cases[case_id]["target_slice"] in {"36B", "36C", "36D", "36F"}


def test_original_freeze_keeps_pending_policy_after_definition_registration() -> None:
    snapshot = specification_snapshot()
    assert snapshot["rules"]["reliability_denominator"] is None
    assert snapshot["rules"]["assessment_status"] == ASSESSMENT_STATUS
    assert snapshot["rules"]["live_trading_approved"] is False
    assert snapshot["implementation_status_at_freeze"] == "FOUNDATION_ONLY_NOT_REGISTERED"
    current = get_algorithm(ALGORITHM_KEY)
    assert current is not None and current.supported_modes == ("OPTION_PORTFOLIO_SIMULATION",)


def test_parameter_schema_only_advertises_approved_editable_values() -> None:
    schema = Model2Parameters.model_json_schema()
    assert set(schema["properties"]) == {
        "wing_width_points",
        "slippage_percent",
        "vix_state_filter_enabled",
    }
    assert schema["properties"]["wing_width_points"]["enum"] == [150, 200]
    assert schema["properties"]["slippage_percent"]["enum"] == ["0.00", "1.00", "2.00"]
    assert schema["additionalProperties"] is False
