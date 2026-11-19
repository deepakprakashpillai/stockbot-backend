"""Current approved revision; the original 36A snapshot remains immutable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from app.domain.model2.specification import (
    FIXED_RULES,
    SourceDocument,
    specification_digest,
    specification_snapshot,
)

REVISION = "2026-11-17-r1"
RELIABILITY_POLICY_VERSION = "model2.reliability.opened.v1"
APPROVAL_SOURCE = SourceDocument(
    "approval_r1",
    "sources/approved-reliability-2026-11-17.txt",
    "51f2b3668ea3c08b08e43e0e97fbde70df27cc0db24b2d116b6e3636913f06ca",
)
RULE_CHANGES = MappingProxyType(
    {
        "reliability_denominator": "OPENED_CYCLES_INCLUDING_EXCLUDED",
        "assessment_status": "RELIABILITY_POLICY_APPROVED_OTHER_CRITERIA_SEPARATE",
        "excluded_risk_booking_date": "EXPIRY_DATE",
        "pending_target_secondary_tag": "PENDING_TARGET",
        "new_settlement_data_policy": "NEW_IMMUTABLE_DATASET_AND_REPLAY",
    }
)
APPROVED_RULES = MappingProxyType(dict(FIXED_RULES) | dict(RULE_CHANGES))


def decimal_rule(name: str) -> Decimal:
    value = APPROVED_RULES[name]
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} is not a decimal rule.")
    return value


def approved_specification_snapshot() -> dict[str, object]:
    payload = specification_snapshot()
    payload.update(
        {
            "schema_version": "stockbot.model2-specification.v2",
            "revision": REVISION,
            "supersedes_specification_sha256": specification_digest(),
            "implementation_status_at_freeze": "36C_COMPONENTS_NOT_REGISTERED",
            "reliability_policy_version": RELIABILITY_POLICY_VERSION,
        }
    )
    payload["source_precedence"] = [
        asdict(APPROVAL_SOURCE),
        *cast(list[dict[str, object]], payload["source_precedence"]),
    ]
    payload["rules"] = cast(dict[str, object], payload["rules"]) | dict(RULE_CHANGES)
    for rule in cast(list[dict[str, object]], payload["rule_references"]):
        if rule["id"] in {"reliability", "excluded_risk", "loss_controls"}:
            rule["source_sections"] = ["approval_r1:R2-10"]
        elif rule["id"] == "overnight":
            rule["source_sections"] = ["approval_r1:R2-9"]
        if rule["id"] == "reliability":
            rule["acceptance_cases"] = [
                "reliability_opened_denominator",
                "strictly_over_two_percent",
            ]
    return payload


def approved_specification_digest() -> str:
    raw = json.dumps(approved_specification_snapshot(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
