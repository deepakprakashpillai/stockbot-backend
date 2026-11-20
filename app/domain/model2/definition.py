"""Catalog metadata for the approved Model 2 implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from app.domain.algorithms import AlgorithmDefinition
from app.domain.model2.approved_specification import approved_specification_snapshot
from app.domain.model2.execution import ALGORITHM_CODE_VERSION, ALGORITHM_KEY
from app.domain.model2.parameters import Model2Parameters

EXECUTION_DESCRIPTOR: dict[str, Any] = {
    "mode": "OPTION_PORTFOLIO_SIMULATION",
    "leg_count": 4,
    "holding_period": "OVERNIGHT",
    "result_path": "option-positions",
    "scenario_group_required": True,
    "pricing_quality": "estimated_historical_ohlc_open",
    "executable_quotes": False,
    "capital_usage": "FIXED_RISK_LIMITS",
    "live_trading_approved": False,
}


def model2_definition() -> AlgorithmDefinition:
    from app.domain.algorithms import (
        AlgorithmDefinition,
        AlgorithmDocumentation,
        AlgorithmRule,
        AlgorithmTemplate,
        DocumentationInput,
        LogicStep,
    )

    return AlgorithmDefinition(
        key=ALGORITHM_KEY,
        display_name="Model 2 Overnight Iron Condor",
        description="One-lot NIFTY expiry-eve iron condor. Four synchronized option legs, fixed risk limits and three missing-data scenarios.",
        code_version=ALGORITHM_CODE_VERSION,
        schema_version=1,
        active=True,
        supported_modes=("OPTION_PORTFOLIO_SIMULATION",),
        required_capabilities=("model2_bundle_v3",),
        fixed_rules=cast(dict[str, Any], approved_specification_snapshot()["rules"])
        | {
            "execution_descriptor": EXECUTION_DESCRIPTOR,
            "pricing_quality": "estimated_historical_ohlc_open",
            "executable_quotes": False,
            "live_trading_approved": False,
            "synthetic": False,
        },
        templates=tuple(
            AlgorithmTemplate(
                key="primary_200" if width == 200 else "alternative_150",
                name=f"{'Primary' if width == 200 else 'Alternative'} {width}-point wings",
                description="Exactly one historical lot; 2% adverse fill slippage; experimental VIX filter OFF.",
                experimental=False,
                parameters=Model2Parameters(
                    wing_width_points=cast(Literal[150, 200], width)
                ).model_dump(mode="json"),
            )
            for width in (200, 150)
        ),
        parameter_model=Model2Parameters,
        documentation=AlgorithmDocumentation(
            purpose="Historical research for the partner-approved expiry-eve iron condor.",
            lifecycle_status="approved_historical_research",
            safety_notice="Estimated candle-opening fills; historical backtesting only.",
            limitations=(
                "Opening fills are OHLC estimates, not executable bid/ask quotes.",
                "Margin is ASSUMED; transaction charges are a placeholder.",
                "Excluded outcomes stay outside priced performance; compare all three independent scenarios.",
                "Development window: 16 November 2023 to 17 July 2026; later dates have known prior exposure.",
                "Random-day control and paper/live qualification remain separate.",
            ),
            inputs=(
                DocumentationInput(
                    "market_bundle",
                    "Model 2 data bundle",
                    "model2_bundle_v3",
                    None,
                    True,
                    "Spot, India VIX, CE/PE prices, contracts, immutable sessions/expiries; futures optional.",
                ),
            ),
            logic_steps=(
                LogicStep(
                    "cycle",
                    1,
                    "input",
                    "Choose the expiry cycle",
                    "Use the actual expiry and preceding normal trading session; retain all skipped cycles.",
                ),
                LogicStep(
                    "entry",
                    2,
                    "decision",
                    "Enter four legs",
                    "At 15:20 use the completed 15:15 candle, select exact contracts and apply the credit/risk/margin checks.",
                    depends_on=("cycle",),
                ),
                LogicStep(
                    "monitor",
                    3,
                    "gate",
                    "Monitor through overnight expiry",
                    "Completed-candle decisions precede actual opening fills; emergency, stop, time and target priority. Retain pending exits overnight.",
                    depends_on=("entry",),
                ),
                LogicStep(
                    "results",
                    4,
                    "output",
                    "Compare three independent scenarios",
                    "Late fill, then official intrinsic settlement, then exclusion. Separate priced performance from assumed risk amounts.",
                    depends_on=("monitor",),
                ),
            ),
            outputs=(
                "Four-leg positions and cycle reasons",
                "Separate risk ledger",
                "Primary, zero-risk and halted scenario results",
            ),
            rules=(
                AlgorithmRule(
                    "model2_baseline",
                    "Approved spec v1.4 with opened-cycle reliability policy.",
                    "Partner answers received 17 November 2026",
                    "R2-1–R2-10",
                    "APPROVED",
                    ALGORITHM_CODE_VERSION,
                    test_references=("tests/model2/test_clock.py",),
                    effective_date="2026-11-17",
                ),
            ),
        ),
    )
