"""Original 36A specification freeze, with its historically pending policy.

Current approved rules live in approved_specification.py. This original snapshot
remains immutable for provenance; no current engine should infer policy from it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, time
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from app.domain.model2.parameters import model2_presets

ALGORITHM_KEY = "model2_overnight_iron_condor"
SPECIFICATION_VERSION = "1.4"
SPECIFICATION_SCHEMA_VERSION = "stockbot.model2-specification.v1"
ASSESSMENT_STATUS = "NOT_ASSESSED_POLICY_PENDING"
SCENARIO_POLICIES = ("MAX_LOSS_RISK", "ZERO_RISK", "HALT_AT_FIRST_EXCLUSION")


@dataclass(frozen=True)
class SourceDocument:
    id: str
    path: str
    sha256: str


# Highest precedence first. Copies are preserved in contracts/model2/sources.
SOURCES = (
    SourceDocument(
        "lifecycle",
        "sources/lifecycle-answers-2026-11-17.txt",
        "9ec6eb4a41d4a1817a256ca6b5b60accc168d32f8243da16bfedd42ca99a4977",
    ),
    SourceDocument(
        "round2",
        "sources/round2-answers-2026-11-17.txt",
        "0162c342b255af4fb77dcd1d611ea629b6b2707b333d003e2988e185e5dde628",
    ),
    SourceDocument(
        "original",
        "sources/original-model2-specification.docx",
        "45b5b4ad5b2821011c054f7d1777e75512db08a5fa770dbb336062c058acb5d4",
    ),
)


@dataclass(frozen=True)
class RuleReference:
    id: str
    source_sections: tuple[str, ...]
    acceptance_cases: tuple[str, ...]


# These case IDs name frozen expectations, not claims that the engine exists.
RULE_REFERENCES = (
    RuleReference(
        "calendar", ("original:3,4,4A", "round2:Q1"), ("holiday_cycle", "special_session")
    ),
    RuleReference("entry", ("round2:Q2,R2-1",), ("entry_1520", "entry_retry", "entry_rejected")),
    RuleReference("selection", ("round2:Q3,R2-6",), ("round_away", "missing_contract", "widths")),
    RuleReference(
        "entry_filters",
        ("original:4", "round2:Q5,R2-7"),
        ("credit_boundary", "vix_boundary", "invalid_max_loss"),
    ),
    RuleReference(
        "pricing",
        ("round2:Q4,Q5,R2-1,R2-3",),
        ("adverse_slippage", "no_future_volume", "missing_previous_bar"),
    ),
    RuleReference(
        "risk_cap",
        ("round2:Q6,R2-5,R2-6,R2-7",),
        ("cap_rejection", "cap_equality", "historical_lot_size"),
    ),
    RuleReference("margin", ("round2:Q7,R2-6",), ("margin_rejection", "margin_equality")),
    RuleReference(
        "exits",
        ("round2:Q8,Q9,R2-1,R2-2",),
        ("next_open_exit", "time_exit", "gap_stop", "exit_priority"),
    ),
    RuleReference(
        "emergency",
        ("original:6", "round2:Q9,R2-8"),
        ("vix_spike", "vix_gap", "feed_lost", "exchange_halt"),
    ),
    RuleReference("late_data", ("round2:R2-4",), ("late_fill", "settlement", "excluded")),
    RuleReference("costs", ("round2:Q11,R2-4,R2-6",), ("charges_once", "no_settlement_slippage")),
    RuleReference(
        "loss_controls",
        ("round2:Q11,R2-5",),
        ("net_monthly_loss", "loss_pause", "zero_streak", "capital_scaling"),
    ),
    RuleReference(
        "overnight",
        ("lifecycle:1",),
        ("pending_overnight", "overnight_retries", "target_becomes_stop_gap"),
    ),
    RuleReference(
        "excluded_risk",
        ("lifecycle:2",),
        ("placeholder_risk_only", "three_scenarios", "scenario_divergence"),
    ),
    RuleReference(
        "reliability", ("lifecycle:2",), ("reliability_pending", "strictly_over_two_percent")
    ),
    RuleReference(
        "experimental_filter",
        ("round2:Q1,R2-8",),
        ("filter_default_off", "filter_reference", "filter_unknown"),
    ),
    RuleReference(
        "research_scope",
        ("original:8,10,12", "round2:Q12,R2-7"),
        ("development_split", "validation_limit", "no_live_trading"),
    ),
)

FIXED_RULES = MappingProxyType(
    {
        "underlying": "NIFTY_50",
        "holding_period": "EXPIRY_EVE_OVERNIGHT",
        "lots": 1,
        "allowed_wing_width_points": (150, 200),
        "allowed_slippage_percent": (Decimal("0.00"), Decimal("1.00"), Decimal("2.00")),
        "default_vix_state_filter_enabled": False,
        "leg_roles": ("SHORT_CALL", "LONG_CALL", "SHORT_PUT", "LONG_PUT"),
        "required_market_inputs": (
            "NIFTY_SPOT_OHLC",
            "INDIA_VIX_OHLC",
            "OPTION_CE_PE_OHLCV",
            "OPTION_CONTRACT_METADATA",
            "EXCHANGE_SESSION_CALENDAR",
        ),
        "futures_required": False,
        "maximum_open_positions": 1,
        "k": Decimal("1.0"),
        "strike_step_points": 50,
        "strike_halfway_rounding": "AWAY_FROM_SPOT",
        "entry_source_bar_start": time(15, 15),
        "entry_decision_time": time(15, 20),
        "entry_open_times": (time(15, 20), time(15, 25)),
        "entry_retry_on": "INVALID_PRICE_DATA_ONLY",
        "minimum_credit_points": Decimal("12"),
        "maximum_entry_vix_inclusive": Decimal("28"),
        "maximum_loss_points_must_be_positive": True,
        "cost_per_leg_per_lot_round_trip_inr": Decimal("100.00"),
        "leg_count": 4,
        "credit_basis": "AFTER_ENTRY_SLIPPAGE",
        "fill_policy": "NEXT_OPEN_ADVERSE_SLIPPAGE",
        "fill_tick_rounding": "BUY_CEILING_SELL_FLOOR",
        "opening_fill_prior_close_positive": True,
        "opening_fill_prior_volume_positive": True,
        "execution_open_positive": True,
        "all_legs_synchronized": True,
        "execution_bar_future_fields_allowed": False,
        "profit_target_fraction_of_credit": Decimal("0.50"),
        "stop_loss_fraction_of_credit": Decimal("1.00"),
        "target_and_stop_include_charges": False,
        "default_starting_capital_inr": Decimal("500000.00"),
        "capital_basis": "FIXED_RUN_STARTING_CAPITAL",
        "target_trade_risk_fraction": Decimal("0.015"),
        "hard_trade_risk_fraction": Decimal("0.025"),
        "daily_loss_fraction": Decimal("0.015"),
        "monthly_loss_fraction": Decimal("0.04"),
        "maximum_loss_includes_charges": True,
        "margin_multiplier": Decimal("1.5"),
        "margin_capital_fraction": Decimal("0.40"),
        "margin_quality": "ASSUMED",
        "margin_above_limit": "REJECT",
        "time_exit_open_times": (time(14, 30), time(14, 35), time(14, 40)),
        "opening_gap_check_time": time(9, 15),
        "exit_priority": ("EMERGENCY", "STOP", "TIME", "TARGET"),
        "vix_emergency_rise_fraction": Decimal("0.15"),
        "vix_comparison_minutes": 30,
        "vix_fallback_minutes": 5,
        "vix_comparison_resets_each_session": True,
        "feed_lost_after_missing_scheduled_bars": 3,
        "normal_exit_retry_count": 2,
        "late_exit_last_open_time": time(15, 25),
        "missing_exit_policy": "LATE_FILL_THEN_OFFICIAL_SETTLEMENT_THEN_EXCLUSION",
        "settlement_slippage": False,
        "pending_overnight_open_times": (time(9, 15), time(9, 20), time(9, 25)),
        "overnight_pauses_retry_clock": True,
        "opening_stop_over_pending_target_reason": "STOP_GAP",
        "preserve_original_exit_trigger": True,
        "loss_control_basis": "CUMULATIVE_NET_REALIZED_PLUS_SCENARIO_RISK_ASSUMPTIONS",
        "limits_force_open_position_exit": False,
        "losing_streak_before_pause": 3,
        "pause_scheduled_cycles": 2,
        "zero_net_streak_action": "UNCHANGED",
        "win_or_pause_end_streak_action": "RESET",
        "excluded_cycles_in_priced_performance": False,
        "excluded_primary_risk_policy": "MAX_LOSS_INCLUDING_CHARGES",
        "sensitivity_requires_independent_replays": True,
        "reliability_threshold_fraction": Decimal("0.02"),
        "reliability_comparison": "STRICTLY_GREATER_THAN",
        "reliability_denominator": None,
        "assessment_status": ASSESSMENT_STATUS,
        "experimental_filter_previous_session_bar_start": time(15, 25),
        "experimental_filter_reference_fallback_not_before": time(15, 0),
        "experimental_filter_missing_reference": "SKIP_IF_ENABLED",
        "experimental_filter_equality_is_rise": True,
        "development_start": date(2023, 11, 16),
        "development_end": date(2026, 7, 17),
        "later_period_is_clean_holdout": False,
        "random_day_control_protocol": None,
        "single_loss_validation_fraction": Decimal("0.025"),
        "assessment_slippage_percent": Decimal("2.00"),
        "live_trading_approved": False,
        "model3_included": False,
    }
)


def _encode(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date | time):
        return value.isoformat()
    raise TypeError(f"Not a specification value: {type(value).__name__}")


def specification_snapshot() -> dict[str, object]:
    """Return detached JSON data; callers cannot mutate the frozen rules."""
    payload = {
        "schema_version": SPECIFICATION_SCHEMA_VERSION,
        "specification_version": SPECIFICATION_VERSION,
        "algorithm_key": ALGORITHM_KEY,
        "implementation_status_at_freeze": "FOUNDATION_ONLY_NOT_REGISTERED",
        "source_precedence": [asdict(source) for source in SOURCES],
        "rules": dict(FIXED_RULES),
        "rule_references": [asdict(rule) for rule in RULE_REFERENCES],
        "presets": [
            {
                "key": preset.key,
                "name": preset.name,
                "parameters": preset.parameters.model_dump(mode="json"),
            }
            for preset in model2_presets()
        ],
        "scenario_policies": SCENARIO_POLICIES,
    }
    return cast(dict[str, object], json.loads(json.dumps(payload, default=_encode)))


def specification_digest() -> str:
    payload = json.dumps(specification_snapshot(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
