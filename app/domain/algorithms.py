from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScoreModel(StrEnum):
    HARD_GATES_CONFIRMATION_5 = "hard_gates_confirmation_5"
    LEGACY_WEIGHTED_10 = "legacy_weighted_10"


def ui(
    label: str,
    group: str,
    help_text: str,
    *,
    order: int,
    unit: str | None = None,
    control: str | None = None,
    experimental: bool = False,
    editable: bool = True,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "label": label,
        "group": group,
        "section": group,
        "order": order,
        "help": help_text,
        "experimental": experimental,
        "editable": editable,
    }
    if unit is not None:
        metadata["unit"] = unit
    if control is not None:
        metadata["control"] = control
    return metadata


class LegacyBullCallSpreadSignalParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score_model: ScoreModel = Field(
        json_schema_extra=ui(
            "Signal model",
            "Signal model",
            "Both available legacy models are experimental.",
            order=1,
            experimental=True,
        )
    )
    entry_window_1_start: time = Field(
        default=time(9, 30),
        json_schema_extra=ui("Window 1 start", "Entry windows", "IST", order=2),
    )
    entry_window_1_end: time = Field(
        default=time(10, 55),
        json_schema_extra=ui("Window 1 final signal", "Entry windows", "IST", order=3),
    )
    entry_window_2_start: time = Field(
        default=time(13, 30),
        json_schema_extra=ui("Window 2 start", "Entry windows", "IST", order=4),
    )
    entry_window_2_end: time = Field(
        default=time(14, 20),
        json_schema_extra=ui("Window 2 final signal", "Entry windows", "IST", order=5),
    )
    hard_exit_time: time = Field(
        default=time(15, 0),
        frozen=True,
        json_schema_extra=ui(
            "Hard exit", "Fixed rules", "Backend-owned IST rule", experimental=False, order=6
        ),
    )
    minimum_management_minutes: int = Field(
        default=30,
        ge=30,
        le=120,
        json_schema_extra=ui(
            "Minimum management time", "Entry windows", "Minutes before hard exit", order=7
        ),
    )
    rsi_threshold: float = Field(
        default=58,
        gt=0,
        lt=100,
        json_schema_extra={
            **ui("RSI bull threshold", "RSI", "5-minute Wilder/RMA RSI", order=8),
            "unit": "RSI",
        },
    )
    vix_gate_threshold: float = Field(
        default=22,
        gt=0,
        json_schema_extra=ui(
            "VIX safety gate", "India VIX", "Live VIX must be strictly lower", order=9
        ),
    )
    vix_min_decline_percent: float = Field(
        default=2,
        ge=0,
        le=100,
        json_schema_extra={
            **ui("Minimum VIX decline", "India VIX", "Decline needed for points", order=10),
            "unit": "%",
        },
    )
    vix_strong_decline_percent: float = Field(
        default=5,
        ge=0,
        le=100,
        json_schema_extra={
            **ui(
                "Strong VIX decline",
                "India VIX",
                "Decline needed for maximum points",
                order=11,
            ),
            "unit": "%",
        },
    )
    minimum_confirmation_score: int = Field(
        json_schema_extra=ui(
            "Minimum score", "Signal model", "Model-specific entry threshold", order=12
        )
    )

    @model_validator(mode="after")
    def validate_rules(self) -> LegacyBullCallSpreadSignalParameters:
        market_open, market_close = time(9, 15), time(15, 30)
        windows = (
            self.entry_window_1_start,
            self.entry_window_1_end,
            self.entry_window_2_start,
            self.entry_window_2_end,
        )
        if not all(market_open <= value <= market_close for value in windows):
            raise ValueError("Entry windows must remain inside the 09:15–15:30 IST session.")
        if not (
            self.entry_window_1_start
            < self.entry_window_1_end
            < self.entry_window_2_start
            < self.entry_window_2_end
        ):
            raise ValueError("Entry windows must be ordered and must not overlap.")
        if self.hard_exit_time != time(15, 0):
            raise ValueError("Hard exit time is fixed at 15:00 IST for this code version.")
        latest_minutes = self.entry_window_2_end.hour * 60 + self.entry_window_2_end.minute
        exit_minutes = self.hard_exit_time.hour * 60 + self.hard_exit_time.minute
        if exit_minutes - latest_minutes < self.minimum_management_minutes:
            raise ValueError("The final signal must leave the configured management period.")
        if self.vix_strong_decline_percent <= self.vix_min_decline_percent:
            raise ValueError("Strong VIX decline must exceed the minimum decline.")
        maximum = 5 if self.score_model == ScoreModel.HARD_GATES_CONFIRMATION_5 else 10
        if not 0 <= self.minimum_confirmation_score <= maximum:
            raise ValueError(f"Minimum confirmation score must be between 0 and {maximum}.")
        return self


class BullCallSpreadSignalParameters(BaseModel):
    """Approved V1 values from the primary Bull Call Spread document."""

    model_config = ConfigDict(extra="forbid")

    entry_window_1_enabled: bool = Field(
        default=True,
        json_schema_extra=ui(
            "Enable window 1", "Entry schedule", "Allow morning entries.", order=10
        ),
    )
    entry_window_1_start: time = Field(
        default=time(9, 30),
        json_schema_extra=ui("Window 1 start", "Entry schedule", "IST", order=11),
    )
    entry_window_1_end: time = Field(
        default=time(11, 0),
        json_schema_extra=ui("Window 1 end", "Entry schedule", "Inclusive IST", order=12),
    )
    entry_window_2_enabled: bool = Field(
        default=True,
        json_schema_extra=ui(
            "Enable window 2",
            "Entry schedule",
            "Used only when window 1 produced no trade.",
            order=13,
        ),
    )
    entry_window_2_start: time = Field(
        default=time(13, 30),
        json_schema_extra=ui("Window 2 start", "Entry schedule", "IST", order=14),
    )
    entry_window_2_end: time = Field(
        default=time(14, 30),
        json_schema_extra=ui("Window 2 end", "Entry schedule", "Inclusive IST", order=15),
    )
    evaluation_interval_minutes: int = Field(
        default=5,
        ge=5,
        le=60,
        json_schema_extra=ui(
            "Signal interval", "Entry schedule", "Completed-candle interval.", order=16, unit="min"
        ),
    )
    hard_exit_time: time = Field(
        default=time(15, 0),
        json_schema_extra=ui("Hard exit", "Entry schedule", "IST", order=17),
    )
    max_trades_per_day: int = Field(
        default=1,
        ge=1,
        le=1,
        json_schema_extra=ui(
            "Maximum trades per day", "Entry schedule", "Daily entry cap.", order=18
        ),
    )

    rsi_period: int = Field(
        default=14,
        ge=2,
        le=100,
        json_schema_extra=ui(
            "RSI period", "Signal scoring", "Wilder/RMA completed candles.", order=20
        ),
    )
    rsi_threshold: float = Field(
        default=58,
        gt=0,
        lt=100,
        json_schema_extra=ui(
            "RSI threshold", "Signal scoring", "RSI must be strictly above this value.", order=21
        ),
    )
    vix_maximum: float = Field(
        default=22,
        gt=0,
        json_schema_extra=ui(
            "Maximum India VIX", "Signal scoring", "Values above this block entry.", order=22
        ),
    )
    minimum_vix_decline_percent: float = Field(
        default=0,
        ge=0,
        lt=100,
        json_schema_extra=ui(
            "Minimum VIX decline",
            "Signal scoring",
            "Current VIX must decline by more than this percentage from the previous session close.",
            order=23,
            unit="%",
        ),
    )
    pdh_weight: int = Field(
        default=3,
        ge=0,
        le=100,
        json_schema_extra=ui(
            "PDH weight", "Signal scoring", "Points for spot above PDH.", order=24
        ),
    )
    vwap_weight: int = Field(
        default=2,
        ge=0,
        le=100,
        json_schema_extra=ui(
            "VWAP weight", "Signal scoring", "Points for spot above futures VWAP.", order=25
        ),
    )
    rsi_weight: int = Field(
        default=2,
        ge=0,
        le=100,
        json_schema_extra=ui(
            "RSI weight", "Signal scoring", "Points for RSI confirmation.", order=26
        ),
    )
    vix_weight: int = Field(
        default=3,
        ge=0,
        le=100,
        json_schema_extra=ui("VIX weight", "Signal scoring", "Points for VIX decline.", order=27),
    )
    minimum_score: int = Field(
        default=7,
        ge=0,
        json_schema_extra=ui("Minimum score", "Signal scoring", "Entry score threshold.", order=28),
    )

    atm_increment: int = Field(
        default=50,
        gt=0,
        json_schema_extra=ui(
            "ATM increment",
            "Contract selection",
            "Half-up strike rounding increment.",
            order=30,
            unit="points",
        ),
    )
    primary_spread_width: int = Field(
        default=200,
        gt=0,
        json_schema_extra=ui(
            "Primary spread width",
            "Contract selection",
            "Short-call distance above ATM.",
            order=31,
            unit="points",
        ),
    )
    fallback_enabled: bool = Field(
        default=True,
        json_schema_extra=ui(
            "Enable fallback spread",
            "Contract selection",
            "Try the wider spread when debit is too high.",
            order=32,
        ),
    )
    fallback_spread_width: int = Field(
        default=250,
        gt=0,
        json_schema_extra=ui(
            "Fallback spread width",
            "Contract selection",
            "Fallback short-call distance.",
            order=33,
            unit="points",
        ),
    )
    maximum_debit_percent: float = Field(
        default=35,
        gt=0,
        le=100,
        json_schema_extra=ui(
            "Maximum debit",
            "Contract selection",
            "Maximum debit as a percentage of spread width.",
            order=34,
            unit="%",
        ),
    )
    same_day_weekly_expiry: bool = Field(
        default=True,
        json_schema_extra=ui(
            "Allow same-day weekly expiry",
            "Contract selection",
            "Monthly expiry remains blocked.",
            order=35,
        ),
    )
    lots: int = Field(
        default=1,
        ge=1,
        le=2,
        json_schema_extra=ui(
            "Number of lots", "Position", "Contract lot size is read from metadata.", order=40
        ),
    )

    stop_loss_percent: float = Field(
        default=40,
        gt=0,
        lt=100,
        json_schema_extra=ui(
            "Stop loss", "Exit management", "Decline from entry debit.", order=50, unit="%"
        ),
    )
    target_max_profit_percent: float = Field(
        default=50,
        gt=0,
        le=100,
        json_schema_extra=ui(
            "Target",
            "Exit management",
            "Percentage of maximum possible profit.",
            order=51,
            unit="%",
        ),
    )
    trend_spread_decline_percent: float = Field(
        default=25,
        gt=0,
        lt=100,
        json_schema_extra=ui(
            "Trend spread decline",
            "Exit management",
            "Combined with spot below futures VWAP.",
            order=52,
            unit="%",
        ),
    )
    monitoring_interval_minutes: int = Field(
        default=5,
        ge=5,
        le=60,
        json_schema_extra=ui(
            "Monitoring interval",
            "Exit management",
            "Completed-candle interval.",
            order=53,
            unit="min",
        ),
    )

    slippage_percent: float = Field(
        default=5,
        ge=0,
        lt=100,
        json_schema_extra=ui(
            "Slippage per leg",
            "Costs and risk",
            "Adverse percentage applied to each fill.",
            order=60,
            unit="%",
        ),
    )
    flat_round_trip_cost: float = Field(
        default=400,
        ge=0,
        json_schema_extra=ui(
            "Flat round-trip cost",
            "Costs and risk",
            "Deducted once per completed spread.",
            order=61,
            unit="₹",
        ),
    )
    maximum_daily_loss: float = Field(
        default=10_000,
        ge=0,
        json_schema_extra=ui(
            "Maximum daily loss",
            "Costs and risk",
            "Blocks further entries after this net loss.",
            order=62,
            unit="₹",
        ),
    )

    @model_validator(mode="after")
    def validate_v1_rules(self) -> BullCallSpreadSignalParameters:
        market_open, market_close = time(9, 15), time(15, 30)
        windows = (
            self.entry_window_1_start,
            self.entry_window_1_end,
            self.entry_window_2_start,
            self.entry_window_2_end,
            self.hard_exit_time,
        )
        if any(value.tzinfo is not None or value.second or value.microsecond for value in windows):
            raise ValueError(
                "Session times must be local IST times aligned to whole five-minute candles."
            )
        if not all(market_open <= value <= market_close for value in windows):
            raise ValueError("Entry and exit times must remain inside the 09:15–15:30 IST session.")
        if not (
            self.entry_window_1_start
            <= self.entry_window_1_end
            < self.entry_window_2_start
            <= self.entry_window_2_end
            < self.hard_exit_time
        ):
            raise ValueError(
                "Entry windows must be ordered, non-overlapping, and before hard exit."
            )
        if any(value.minute % 5 for value in windows):
            raise ValueError("Entry and exit times must align to five-minute candles.")
        if self.evaluation_interval_minutes % 5 or self.monitoring_interval_minutes % 5:
            raise ValueError(
                "Evaluation and monitoring intervals must be multiples of five minutes."
            )
        maximum_score = self.pdh_weight + self.vwap_weight + self.rsi_weight + self.vix_weight
        if self.minimum_score > maximum_score:
            raise ValueError("Minimum score cannot exceed the sum of the four score weights.")
        if self.primary_spread_width % self.atm_increment:
            raise ValueError("Primary spread width must be a multiple of the ATM increment.")
        if self.fallback_enabled and self.fallback_spread_width <= self.primary_spread_width:
            raise ValueError("Fallback spread width must exceed the primary spread width.")
        if self.fallback_enabled and self.fallback_spread_width % self.atm_increment:
            raise ValueError("Fallback spread width must be a multiple of the ATM increment.")
        return self


class BullCallSpreadHardGateParameters(BullCallSpreadSignalParameters):
    """Partner-review Q04 entry model introduced in Bull Call Spread V1.2."""

    pdh_weight: Literal[0] = Field(
        default=0,
        json_schema_extra=ui(
            "PDH hard gate",
            "Signal scoring",
            "Spot above PDH is mandatory and contributes no score points.",
            order=24,
            editable=False,
        ),
    )
    vwap_weight: Literal[0] = Field(
        default=0,
        json_schema_extra=ui(
            "VWAP hard gate",
            "Signal scoring",
            "Spot above futures VWAP is mandatory and contributes no score points.",
            order=25,
            editable=False,
        ),
    )
    rsi_weight: Literal[2] = Field(
        default=2,
        json_schema_extra=ui(
            "RSI confirmation points",
            "Signal scoring",
            "Fixed points when RSI is strictly above its threshold.",
            order=26,
            editable=False,
        ),
    )
    vix_weight: Literal[3] = Field(
        default=3,
        json_schema_extra=ui(
            "VIX confirmation points",
            "Signal scoring",
            "Fixed points when current VIX is below the previous-session close.",
            order=27,
            editable=False,
        ),
    )
    minimum_score: Literal[3] = Field(
        default=3,
        json_schema_extra=ui(
            "Minimum confirmation score",
            "Signal scoring",
            "Enter when RSI and VIX confirmations total at least three of five points.",
            order=28,
            editable=False,
        ),
    )
    minimum_vix_decline_percent: Literal[0] = Field(
        default=0,
        json_schema_extra=ui(
            "Minimum VIX decline",
            "Signal scoring",
            "Current VIX must be below the previous-session close.",
            order=23,
            unit="%",
            editable=False,
        ),
    )


class BullCallSpreadFuturesParameters(BullCallSpreadHardGateParameters):
    """V1.3 corrects only the instrument used in the two VWAP comparisons."""

    vwap_weight: Literal[0] = Field(
        default=0,
        json_schema_extra=ui(
            "VWAP hard gate",
            "Signal scoring",
            "Futures close above futures VWAP is mandatory and contributes no score points.",
            order=25,
            editable=False,
        ),
    )
    trend_spread_decline_percent: float = Field(
        default=25,
        gt=0,
        lt=100,
        json_schema_extra=ui(
            "Trend spread decline",
            "Exit management",
            "Combined with futures close below futures VWAP.",
            order=52,
            unit="%",
        ),
    )


class PlatformDemoParameters(BaseModel):
    """Synthetic parameters used only to prove the platform pipeline."""

    model_config = ConfigDict(extra="forbid")

    signal_every_n_candles: int = Field(
        default=5,
        ge=1,
        le=10_000,
        json_schema_extra={
            **ui(
                "Evaluation interval",
                "Synthetic fixture",
                "Accept every Nth completed candle in each session.",
                order=1,
                experimental=True,
            ),
            "unit": "candles",
            "synthetic": True,
        },
    )
    hold_candles: int = Field(
        default=3,
        ge=0,
        le=10_000,
        json_schema_extra={
            **ui(
                "Holding interval",
                "Synthetic fixture",
                "Reject this many completed candles after an accepted demo decision.",
                order=2,
                experimental=True,
            ),
            "unit": "candles",
            "synthetic": True,
        },
    )


@dataclass(frozen=True)
class AlgorithmTemplate:
    key: str
    name: str
    description: str
    experimental: bool
    parameters: dict[str, Any]


@dataclass(frozen=True)
class DocumentationInput:
    key: str
    label: str
    capability: str
    unit: str | None
    required: bool
    description: str


@dataclass(frozen=True)
class LogicStep:
    id: str
    order: int
    kind: Literal["input", "gate", "confirmation", "decision", "output"]
    label: str
    description: str
    depends_on: tuple[str, ...] = ()
    outcome_labels: tuple[str, ...] = ()
    fixed_rule_keys: tuple[str, ...] = ()
    parameter_keys: tuple[str, ...] = ()
    experimental: bool = False


@dataclass(frozen=True)
class AlgorithmDocumentation:
    purpose: str
    lifecycle_status: str
    safety_notice: str
    limitations: tuple[str, ...]
    inputs: tuple[DocumentationInput, ...]
    logic_steps: tuple[LogicStep, ...]
    outputs: tuple[str, ...]
    diagnostic_schema_version: str = "1"
    rule_register_version: str = "stockbot.rule-register.v1"
    rules: tuple[AlgorithmRule, ...] = ()


@dataclass(frozen=True)
class AlgorithmRule:
    key: str
    statement: str
    source_document: str
    source_reference: str
    status: Literal["APPROVED", "PENDING_PARTNER", "EXPERIMENT_ONLY", "TECHNICAL_PLATFORM_RULE"]
    implemented_code_version: str | None = None
    parameter_key: str | None = None
    test_references: tuple[str, ...] = ()
    effective_date: str | None = None


@dataclass(frozen=True)
class ConfiguredAlgorithm:
    key: str
    code_version: str
    parameters: BaseModel


@dataclass(frozen=True)
class AlgorithmDefinition:
    key: str
    display_name: str
    description: str
    code_version: str
    schema_version: int
    active: bool
    supported_modes: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    fixed_rules: dict[str, Any]
    templates: tuple[AlgorithmTemplate, ...]
    parameter_model: type[BaseModel]
    documentation: AlgorithmDocumentation

    def configure(self, parameters: dict[str, Any]) -> ConfiguredAlgorithm:
        return ConfiguredAlgorithm(
            key=self.key,
            code_version=self.code_version,
            parameters=self.parameter_model.model_validate(parameters),
        )


def validate_documentation(definition: AlgorithmDefinition) -> None:
    """Fail registry construction when display metadata drifts from code-owned rules."""
    parameter_keys = set(definition.parameter_model.model_fields)
    step_ids = {step.id for step in definition.documentation.logic_steps}
    if len(step_ids) != len(definition.documentation.logic_steps):
        raise ValueError(f"{definition.key}: documentation step IDs must be unique.")
    for step in definition.documentation.logic_steps:
        if not set(step.depends_on) <= step_ids:
            raise ValueError(f"{definition.key}: logic dependencies must reference known steps.")
        if not set(step.fixed_rule_keys) <= set(definition.fixed_rules):
            missing = sorted(set(step.fixed_rule_keys) - set(definition.fixed_rules))
            raise ValueError(
                f"{definition.key}@{definition.code_version}: logic step {step.id} "
                f"references unknown fixed rules: {', '.join(missing)}."
            )
        if not set(step.parameter_keys) <= parameter_keys:
            raise ValueError(f"{definition.key}: logic step references an unknown parameter.")
    visiting: set[str] = set()
    visited: set[str] = set()
    steps = {step.id: step for step in definition.documentation.logic_steps}

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError(f"{definition.key}: documentation logic dependencies must be acyclic.")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in steps[identifier].depends_on:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for step_id in steps:
        visit(step_id)


def _legacy_template(model: ScoreModel, minimum: int, name: str) -> AlgorithmTemplate:
    parameters = LegacyBullCallSpreadSignalParameters(
        score_model=model, minimum_confirmation_score=minimum
    ).model_dump(mode="json")
    return AlgorithmTemplate(
        key=model.value,
        name=name,
        description="Experimental research template; not approved for live trading.",
        experimental=True,
        parameters=parameters,
    )


LEGACY_BULL_CALL_SPREAD = AlgorithmDefinition(
    key="bull_call_spread",
    display_name="Bull Call Spread",
    description="Legacy experimental signal research retained for historical reproducibility.",
    code_version="0.1.0-signal-research",
    schema_version=1,
    active=False,
    supported_modes=("signal_research",),
    required_capabilities=("price_candles", "futures_vwap", "india_vix"),
    fixed_rules={
        "rsi_period": 14,
        "rsi_method": "wilder_rma",
        "rsi_warmup_candles": 28,
        "completed_candles_only": True,
        "pdh_source": "previous_session_high",
        "experimental": True,
        "live_trading_approved": False,
    },
    templates=(
        _legacy_template(ScoreModel.HARD_GATES_CONFIRMATION_5, 3, "Hard gates + confirmation (5)"),
        _legacy_template(ScoreModel.LEGACY_WEIGHTED_10, 7, "Legacy weighted score (10)"),
    ),
    parameter_model=LegacyBullCallSpreadSignalParameters,
    documentation=AlgorithmDocumentation(
        purpose="Experimental NIFTY bullish signal research. It does not place orders or calculate realistic option-spread P&L.",
        lifecycle_status="experimental_research",
        safety_notice="Both documented score paths are experimental and intentionally unresolved; no live-trading approval exists.",
        limitations=(
            "Requires futures VWAP and India VIX capabilities not present in the preserved candle-only dataset.",
            "Signal-only evidence is not option-spread P&L or execution evidence.",
        ),
        inputs=(
            DocumentationInput(
                "price_candles",
                "NIFTY price candles",
                "price_candles",
                None,
                True,
                "Completed five-minute index candles.",
            ),
            DocumentationInput(
                "futures_vwap",
                "Futures VWAP",
                "futures_vwap",
                None,
                True,
                "Required market-context input; unavailable in candle-only datasets.",
            ),
            DocumentationInput(
                "india_vix",
                "India VIX",
                "india_vix",
                None,
                True,
                "Required volatility-context input; unavailable in candle-only datasets.",
            ),
        ),
        logic_steps=(
            LogicStep(
                "market_input",
                1,
                "input",
                "Read completed market inputs",
                "Use completed candles and required context inputs.",
                outcome_labels=("inputs available", "missing capability"),
                fixed_rule_keys=("completed_candles_only",),
                experimental=True,
            ),
            LogicStep(
                "hard_gates",
                2,
                "gate",
                "Apply PDH, VWAP, and VIX gates",
                "The documented hard-gate research path checks market context before confirmation scoring.",
                depends_on=("market_input",),
                fixed_rule_keys=("pdh_source",),
                parameter_keys=("vix_gate_threshold",),
                experimental=True,
            ),
            LogicStep(
                "confirmation_5",
                3,
                "confirmation",
                "Hard gates + confirmation score (maximum 5)",
                "One experimental branch uses PDH/VWAP hard gates and a five-point confirmation score.",
                depends_on=("hard_gates",),
                parameter_keys=("score_model", "minimum_confirmation_score", "rsi_threshold"),
                experimental=True,
            ),
            LogicStep(
                "legacy_weighted_10",
                4,
                "confirmation",
                "Legacy weighted score (maximum 10)",
                "A separate legacy experimental branch retains the earlier weighted score model. It is not merged with the five-point branch.",
                depends_on=("market_input",),
                parameter_keys=(
                    "score_model",
                    "minimum_confirmation_score",
                    "vix_min_decline_percent",
                    "vix_strong_decline_percent",
                ),
                experimental=True,
            ),
            LogicStep(
                "research_decision",
                5,
                "decision",
                "Emit a research decision",
                "Produce a signal-research decision only after the selected experimental branch evaluates.",
                depends_on=("confirmation_5", "legacy_weighted_10"),
                outcome_labels=("accepted", "rejected"),
                experimental=True,
            ),
            LogicStep(
                "signal_output",
                6,
                "output",
                "Return signal evidence",
                "Return research evidence; order placement and option execution remain deferred.",
                depends_on=("research_decision",),
                experimental=True,
            ),
        ),
        outputs=(
            "Accepted or rejected signal-research decision",
            "Diagnostic evidence only; no broker order or financial P&L",
        ),
    ),
)


_bull_call_spread_v1_defaults = BullCallSpreadSignalParameters().model_dump(mode="json")

WEIGHTED_BULL_CALL_SPREAD = AlgorithmDefinition(
    key="bull_call_spread",
    display_name="Bull Call Spread",
    description=(
        "Primary-document NIFTY Bull Call Spread V1 with estimated historical OHLC fills."
    ),
    code_version="1.1.0-bull-call-spread-v1",
    schema_version=2,
    active=False,
    supported_modes=("OPTION_SPREAD_SIMULATION",),
    required_capabilities=(
        "price_candles",
        "futures_vwap",
        "india_vix",
        "option_ohlc",
        "option_contracts",
    ),
    fixed_rules={
        "bull_call_structure": "buy_atm_ce_sell_higher_ce",
        "rsi_method": "wilder_rma",
        "completed_candles_only": True,
        "pdh_source": "previous_session_high",
        "vwap_source": "nifty_futures",
        "vwap_comparison_price": "nifty_spot_close",
        "previous_vix_source": "previous_session_close",
        "monthly_expiry_allowed": False,
        "entry_pricing_basis": "signal_candle_option_close",
        "pricing_quality": "estimated_historical_ohlc",
        "executable_quotes": False,
        "missing_open_position_data": "fail_run",
        "live_trading_approved": False,
    },
    templates=(
        AlgorithmTemplate(
            key="primary_document_defaults",
            name="Bull Call Spread V1 defaults",
            description=(
                "Primary-document defaults with configurable 5% adverse slippage per option leg."
            ),
            experimental=False,
            parameters=_bull_call_spread_v1_defaults,
        ),
    ),
    parameter_model=BullCallSpreadSignalParameters,
    documentation=AlgorithmDocumentation(
        purpose=(
            "Evaluate the primary-document Bull Call Spread V1 and estimate historical INR "
            "outcomes from completed option OHLC candles."
        ),
        lifecycle_status="estimated_historical_research",
        safety_notice=(
            "Results use option candle closes plus configurable slippage. They are estimates, "
            "not executable bid/ask performance, broker orders, or live-trading approval."
        ),
        limitations=(
            "Historical bid/ask quotes, liquidity filters, and non-monthly event dates are deferred.",
            "Monthly expiries remain blocked and missing synchronized data fails an open-position run.",
        ),
        inputs=(
            DocumentationInput(
                "price_candles",
                "NIFTY spot candles",
                "price_candles",
                None,
                True,
                "Completed five-minute spot candles provide price, PDH, and RSI.",
            ),
            DocumentationInput(
                "futures_vwap",
                "NIFTY futures VWAP",
                "futures_vwap",
                None,
                True,
                "Cumulative session VWAP from completed NIFTY futures OHLCV candles.",
            ),
            DocumentationInput(
                "india_vix",
                "India VIX",
                "india_vix",
                None,
                True,
                "Completed VIX candles plus the previous session close.",
            ),
            DocumentationInput(
                "option_ohlc",
                "Weekly option candles",
                "option_ohlc",
                None,
                True,
                "Completed CE candles used for estimated fills and monitoring.",
            ),
            DocumentationInput(
                "option_contracts",
                "Option contract master",
                "option_contracts",
                None,
                True,
                "Weekly expiry, strike, lot-size, and tick-size metadata.",
            ),
        ),
        logic_steps=(
            LogicStep(
                "market_input",
                1,
                "input",
                "Synchronize completed inputs",
                "Read spot, futures VWAP, current VIX, previous VIX close, and warm-up history.",
                outcome_labels=("inputs available", "missing input"),
                fixed_rule_keys=("completed_candles_only", "previous_vix_source"),
            ),
            LogicStep(
                "entry_window",
                2,
                "gate",
                "Check entry schedule",
                "Apply configured windows and the window-two eligibility rule.",
                depends_on=("market_input",),
                parameter_keys=(
                    "entry_window_1_enabled",
                    "entry_window_1_start",
                    "entry_window_1_end",
                    "entry_window_2_enabled",
                    "entry_window_2_start",
                    "entry_window_2_end",
                    "max_trades_per_day",
                ),
            ),
            LogicStep(
                "weighted_score",
                3,
                "confirmation",
                "Calculate the weighted ten-point score",
                "Score PDH, futures VWAP, RSI, and the VIX decline confirmation.",
                depends_on=("entry_window",),
                fixed_rule_keys=("pdh_source", "vwap_source", "vwap_comparison_price"),
                parameter_keys=(
                    "rsi_period",
                    "rsi_threshold",
                    "vix_maximum",
                    "minimum_vix_decline_percent",
                    "pdh_weight",
                    "vwap_weight",
                    "rsi_weight",
                    "vix_weight",
                    "minimum_score",
                ),
            ),
            LogicStep(
                "contract_selection",
                4,
                "decision",
                "Select and validate the weekly call spread",
                "Select ATM/+200 CE, then try +250 only when the debit cap requires it.",
                depends_on=("weighted_score",),
                fixed_rule_keys=("bull_call_structure", "monthly_expiry_allowed"),
                parameter_keys=(
                    "atm_increment",
                    "primary_spread_width",
                    "fallback_enabled",
                    "fallback_spread_width",
                    "maximum_debit_percent",
                    "same_day_weekly_expiry",
                    "lots",
                ),
            ),
            LogicStep(
                "spread_management",
                5,
                "output",
                "Manage and close the spread",
                "Apply stop, target, combined trend reversal, and hard exit in order.",
                depends_on=("contract_selection",),
                fixed_rule_keys=("entry_pricing_basis", "pricing_quality", "executable_quotes"),
                parameter_keys=(
                    "stop_loss_percent",
                    "target_max_profit_percent",
                    "trend_spread_decline_percent",
                    "monitoring_interval_minutes",
                    "hard_exit_time",
                    "slippage_percent",
                    "flat_round_trip_cost",
                    "maximum_daily_loss",
                ),
            ),
        ),
        outputs=(
            "Accepted and rejected signal diagnostics",
            "Estimated INR option-spread trades, legs, costs, equity, and limitations",
        ),
        diagnostic_schema_version="2",
        rules=(
            AlgorithmRule(
                key="completed-candle-time",
                statement="Every strategy input must represent a completed five-minute candle.",
                source_document="BULL CALL SPREADV1.docx",
                source_reference="Entry conditions / completed candle evaluation",
                status="APPROVED",
                implemented_code_version="1.1.0-bull-call-spread-v1",
                test_references=(
                    "tests/test_market_data_correctness.py",
                    "tests/test_backtest_engine.py",
                ),
                effective_date="2026-09-14",
            ),
            AlgorithmRule(
                key="single-trade-per-day",
                statement="Bull Call Spread V1 opens at most one position in a trading day.",
                source_document="BULL CALL SPREADV1.docx",
                source_reference="Trade frequency",
                status="APPROVED",
                implemented_code_version="1.1.0-bull-call-spread-v1",
                parameter_key="max_trades_per_day",
                test_references=("tests/domain/test_option_spread_execution.py",),
                effective_date="2026-09-14",
            ),
            AlgorithmRule(
                key="weighted-score-versus-hard-gates",
                statement="Clarify whether PDH and VWAP are mandatory gates or weighted confirmations.",
                source_document="Bull_Call_Spread_Algorithm_Questions.docx",
                source_reference="Score model answers",
                status="PENDING_PARTNER",
                implemented_code_version="1.1.0-bull-call-spread-v1",
            ),
            AlgorithmRule(
                key="debit-cap-retry",
                statement="Clarify whether a debit-cap rejection ends the day or permits a later entry.",
                source_document="BULL CALL SPREADV1.docx",
                source_reference="Debit cap and skip-trade wording",
                status="PENDING_PARTNER",
                implemented_code_version="1.1.0-bull-call-spread-v1",
            ),
            AlgorithmRule(
                key="monthly-expiry-day",
                statement="Clarify whether the entire monthly-expiry day must be excluded.",
                source_document="Bull_Call_Spread_Algorithm_Questions.docx",
                source_reference="Expiry-day handling",
                status="PENDING_PARTNER",
                implemented_code_version="1.1.0-bull-call-spread-v1",
            ),
            AlgorithmRule(
                key="vix-day-latch",
                statement="Clarify whether one VIX failure blocks entries for the rest of that day.",
                source_document="Bull_Call_Spread_Algorithm_Questions.docx",
                source_reference="VIX gate persistence",
                status="PENDING_PARTNER",
                implemented_code_version="1.1.0-bull-call-spread-v1",
            ),
            AlgorithmRule(
                key="futures-roll-open-interest",
                statement="Clarify the futures roll rule and whether open interest selects the contract.",
                source_document="Bull_Call_Spread_Algorithm_Questions.docx",
                source_reference="Futures contract selection",
                status="PENDING_PARTNER",
                implemented_code_version="1.1.0-bull-call-spread-v1",
            ),
            AlgorithmRule(
                key="whole-day-data-exclusion",
                statement="Exclude a complete session from analysis when required pre-entry data is incomplete.",
                source_document="docs/13-decisions-and-open-questions.md",
                source_reference="2026-09-14 corrective-roadmap decision",
                status="TECHNICAL_PLATFORM_RULE",
                implemented_code_version="1.1.0-bull-call-spread-v1",
                test_references=("tests/test_bull_call_spread_v12_regression.py",),
                effective_date="2026-09-14",
            ),
        ),
    ),
)

_bull_call_spread_v12_defaults = BullCallSpreadHardGateParameters().model_dump(mode="json")
_v12_logic_steps = (
    LogicStep(
        "market_input",
        1,
        "input",
        "Synchronize completed inputs",
        "Read spot, futures VWAP, current VIX, previous VIX close, and warm-up history.",
        outcome_labels=("inputs available", "missing input"),
        fixed_rule_keys=("completed_candles_only", "previous_vix_source"),
    ),
    LogicStep(
        "entry_window",
        2,
        "gate",
        "Check entry schedule",
        "Apply configured windows and the window-two eligibility rule.",
        depends_on=("market_input",),
        parameter_keys=(
            "entry_window_1_enabled",
            "entry_window_1_start",
            "entry_window_1_end",
            "entry_window_2_enabled",
            "entry_window_2_start",
            "entry_window_2_end",
            "max_trades_per_day",
        ),
    ),
    LogicStep(
        "pdh_gate",
        3,
        "gate",
        "Require a previous-day-high breakout",
        "Reject before scoring unless the completed NIFTY spot close is strictly above PDH.",
        depends_on=("entry_window",),
        fixed_rule_keys=("pdh_source", "pdh_hard_gate"),
    ),
    LogicStep(
        "vwap_gate",
        4,
        "gate",
        "Require price above futures VWAP",
        "Reject before scoring unless the completed NIFTY spot close is strictly above futures VWAP.",
        depends_on=("pdh_gate",),
        fixed_rule_keys=("vwap_source", "vwap_comparison_price", "vwap_hard_gate"),
    ),
    LogicStep(
        "confirmation_5",
        5,
        "confirmation",
        "Calculate the five-point confirmation score",
        "Award two RSI points and three VIX-decline points after both price gates pass.",
        depends_on=("vwap_gate",),
        parameter_keys=(
            "rsi_period",
            "rsi_threshold",
            "vix_maximum",
            "minimum_vix_decline_percent",
            "rsi_weight",
            "vix_weight",
            "minimum_score",
        ),
    ),
    LogicStep(
        "contract_selection",
        6,
        "decision",
        "Select and validate the weekly call spread",
        "Select ATM/+200 CE, then try +250 only when the debit cap requires it.",
        depends_on=("confirmation_5",),
        fixed_rule_keys=("bull_call_structure", "monthly_expiry_allowed"),
        parameter_keys=(
            "atm_increment",
            "primary_spread_width",
            "fallback_enabled",
            "fallback_spread_width",
            "maximum_debit_percent",
            "same_day_weekly_expiry",
            "lots",
        ),
    ),
    LogicStep(
        "spread_management",
        7,
        "output",
        "Manage and close the spread",
        "Apply stop, target, combined trend reversal, and hard exit in order.",
        depends_on=("contract_selection",),
        fixed_rule_keys=("entry_pricing_basis", "pricing_quality", "executable_quotes"),
        parameter_keys=(
            "stop_loss_percent",
            "target_max_profit_percent",
            "trend_spread_decline_percent",
            "monitoring_interval_minutes",
            "hard_exit_time",
            "slippage_percent",
            "flat_round_trip_cost",
            "maximum_daily_loss",
        ),
    ),
)

_v12_rules = tuple(
    replace(
        rule,
        statement=(
            "Price above PDH and price above futures VWAP are mandatory hard gates; "
            "RSI and VIX form a five-point confirmation score with a minimum of three."
        ),
        status="PENDING_PARTNER",
        implemented_code_version="1.2.0-bull-call-spread-v1",
        test_references=("tests/test_backtest_engine.py", "tests/test_signal_diagnostics.py"),
        effective_date="2026-09-20",
    )
    if rule.key == "weighted-score-versus-hard-gates"
    else rule
    for rule in WEIGHTED_BULL_CALL_SPREAD.documentation.rules
)

BULL_CALL_SPREAD = replace(
    WEIGHTED_BULL_CALL_SPREAD,
    code_version="1.2.0-bull-call-spread-v1",
    schema_version=3,
    active=True,
    description=(
        "Partner-confirmation candidate for NIFTY Bull Call Spread V1 with PDH/VWAP hard gates and "
        "estimated historical OHLC fills."
    ),
    fixed_rules={
        **WEIGHTED_BULL_CALL_SPREAD.fixed_rules,
        "pdh_hard_gate": "nifty_spot_close_strictly_above_previous_session_high",
        "vwap_hard_gate": "nifty_spot_close_strictly_above_futures_vwap",
        "score_model": "rsi_vix_confirmation_5",
    },
    templates=(
        AlgorithmTemplate(
            key="partner_q04_defaults",
            name="Bull Call Spread V1.2 partner-review candidate",
            description=(
                "Mandatory PDH/VWAP price gates followed by fixed RSI/VIX confirmation points."
            ),
            experimental=True,
            parameters=_bull_call_spread_v12_defaults,
        ),
    ),
    parameter_model=BullCallSpreadHardGateParameters,
    documentation=replace(
        WEIGHTED_BULL_CALL_SPREAD.documentation,
        purpose=(
            "Evaluate the proposed Bull Call Spread V1 entry-gate correction and estimate "
            "historical INR outcomes from completed option OHLC candles."
        ),
        lifecycle_status="pending_partner_confirmation_research",
        logic_steps=_v12_logic_steps,
        diagnostic_schema_version="3",
        rules=_v12_rules,
    ),
)

# Keep BULL_CALL_SPREAD as the immutable V1.2 control for existing callers/replays.
# The catalog's latest version is the narrowly scoped, user-authorized Q10/L04 fix.
FUTURES_BULL_CALL_SPREAD = replace(
    BULL_CALL_SPREAD,
    code_version="1.3.0-bull-call-spread-v1",
    schema_version=4,
    description="Bull Call Spread research with futures-to-futures VWAP comparisons and estimated OHLC fills.",
    parameter_model=BullCallSpreadFuturesParameters,
    fixed_rules={
        **BULL_CALL_SPREAD.fixed_rules,
        "vwap_hard_gate": "nifty_futures_close_strictly_above_futures_vwap",
        "vwap_comparison_price": "nifty_futures_close",
        "trend_reversal_price": "nifty_futures_close",
    },
    templates=(
        AlgorithmTemplate(
            key="futures_vwap_correction",
            name="Bull Call Spread V1.3 — futures VWAP correction",
            description="Corrects entry and trend-reversal price comparisons; retains V1.2 scoring and thresholds.",
            experimental=True,
            parameters=BullCallSpreadFuturesParameters().model_dump(mode="json"),
        ),
    ),
    documentation=replace(
        BULL_CALL_SPREAD.documentation,
        purpose="Research the Q10/L04 futures comparison correction with existing V1.2 parameters.",
        diagnostic_schema_version="4",
        rules=(
            *BULL_CALL_SPREAD.documentation.rules,
            AlgorithmRule(
                key="futures-vwap-comparison-correction",
                statement="Use completed futures close versus futures VWAP for entry and trend reversal; retain spot for PDH and strikes.",
                source_document="Bull_Call_Spread_Algorithm_Questions.docx",
                source_reference="Q10; partner response L04; Deepak's 11 November request to fix the identified mismatch",
                status="EXPERIMENT_ONLY",
                implemented_code_version="1.3.0-bull-call-spread-v1",
                test_references=(
                    "tests/test_futures_vwap_correction.py",
                    "tests/test_financial_run_persistence.py",
                ),
                effective_date="2026-11-11",
            ),
        ),
        limitations=(
            *BULL_CALL_SPREAD.documentation.limitations,
            "Only the VWAP instrument mismatch is corrected; this is not the consolidated November strategy.",
            "Continuous futures contract rollover remains unverified.",
        ),
        inputs=(
            *BULL_CALL_SPREAD.documentation.inputs,
            DocumentationInput(
                "futures_close",
                "NIFTY futures close",
                "futures_vwap",
                "price",
                True,
                "Completed close from the same futures history used to calculate VWAP; checked at preflight.",
            ),
        ),
        logic_steps=tuple(
            replace(
                step, description="Require completed futures close strictly above futures VWAP."
            )
            if step.id == "vwap_gate"
            else replace(
                step,
                description="Read spot, synchronized futures close/VWAP, VIX, and warm-up history.",
            )
            if step.id == "market_input"
            else replace(
                step,
                description="Apply stop, target, futures-below-futures-VWAP trend reversal, and hard exit.",
            )
            if step.id == "spread_management"
            else step
            for step in BULL_CALL_SPREAD.documentation.logic_steps
        ),
    ),
)

# Existing 1.0.0 runs remain reproducible and visibly carry the pre-correction
# lifecycle label. New versions use the corrected 1.1.0 engine contract.
PRE_CORRECTION_BULL_CALL_SPREAD = replace(
    WEIGHTED_BULL_CALL_SPREAD,
    code_version="1.0.0-bull-call-spread-v1",
    active=False,
    description="Legacy estimated-OHLC implementation with known candle-timing risk.",
    documentation=replace(
        WEIGHTED_BULL_CALL_SPREAD.documentation,
        lifecycle_status="legacy_timing_affected_research",
        safety_notice=(
            "This code version may interpret provider candle start timestamps as completion times. "
            "Keep its immutable results for audit only."
        ),
        rules=(),
    ),
)


_platform_demo_parameters = PlatformDemoParameters().model_dump(mode="json")

PLATFORM_DEMO = AlgorithmDefinition(
    key="platform_demo",
    display_name="Platform Demo",
    description=(
        "Synthetic deterministic fixture for proving the backtest platform; not a trading strategy."
    ),
    code_version="1.0.0-platform-demo",
    schema_version=1,
    active=True,
    supported_modes=("PLATFORM_DEMO",),
    required_capabilities=("price_candles",),
    fixed_rules={
        "completed_candles_only": True,
        "session_candle_counter_resets_daily": True,
        "synthetic": True,
        "financial_pnl_available": False,
        "live_trading_approved": False,
    },
    templates=(
        AlgorithmTemplate(
            key="deterministic_fixture",
            name="Deterministic platform fixture",
            description=("Synthetic pipeline test configuration; not approved for live trading."),
            experimental=True,
            parameters=_platform_demo_parameters,
        ),
    ),
    parameter_model=PlatformDemoParameters,
    documentation=AlgorithmDocumentation(
        purpose="Deterministic synthetic fixture that proves the platform persistence and research workflow.",
        lifecycle_status="synthetic_fixture",
        safety_notice="PLATFORM_DEMO is synthetic, uses DEMO_UNITS, and never represents trading performance or financial P&L.",
        limitations=(
            "It is not a trading strategy.",
            "It does not use option contracts, quotes, or live capital.",
        ),
        inputs=(
            DocumentationInput(
                "price_candles",
                "Completed candles",
                "price_candles",
                "candles",
                True,
                "Completed candle count drives the deterministic fixture.",
            ),
        ),
        logic_steps=(
            LogicStep(
                "candle_input",
                1,
                "input",
                "Read completed candles",
                "Count completed candles in each session.",
                fixed_rule_keys=("completed_candles_only",),
                experimental=True,
            ),
            LogicStep(
                "fixture_decision",
                2,
                "decision",
                "Apply deterministic interval",
                "Accept every configured Nth candle while respecting the hold interval.",
                depends_on=("candle_input",),
                parameter_keys=("signal_every_n_candles", "hold_candles"),
                experimental=True,
            ),
            LogicStep(
                "demo_output",
                3,
                "output",
                "Return synthetic platform evidence",
                "Produce synthetic demo records only.",
                depends_on=("fixture_decision",),
                fixed_rule_keys=("synthetic", "financial_pnl_available"),
                experimental=True,
            ),
        ),
        outputs=(
            "Synthetic PLATFORM_DEMO decisions",
            "Synthetic demo trades and DEMO_UNITS records",
        ),
    ),
)

ALGORITHM_REGISTRY = {
    FUTURES_BULL_CALL_SPREAD.key: FUTURES_BULL_CALL_SPREAD,
    PLATFORM_DEMO.key: PLATFORM_DEMO,
}

ALGORITHM_VERSION_REGISTRY = {
    (LEGACY_BULL_CALL_SPREAD.key, LEGACY_BULL_CALL_SPREAD.code_version): LEGACY_BULL_CALL_SPREAD,
    (
        PRE_CORRECTION_BULL_CALL_SPREAD.key,
        PRE_CORRECTION_BULL_CALL_SPREAD.code_version,
    ): PRE_CORRECTION_BULL_CALL_SPREAD,
    (
        WEIGHTED_BULL_CALL_SPREAD.key,
        WEIGHTED_BULL_CALL_SPREAD.code_version,
    ): WEIGHTED_BULL_CALL_SPREAD,
    (BULL_CALL_SPREAD.key, BULL_CALL_SPREAD.code_version): BULL_CALL_SPREAD,
    (FUTURES_BULL_CALL_SPREAD.key, FUTURES_BULL_CALL_SPREAD.code_version): FUTURES_BULL_CALL_SPREAD,
    (PLATFORM_DEMO.key, PLATFORM_DEMO.code_version): PLATFORM_DEMO,
}

for _definition in ALGORITHM_VERSION_REGISTRY.values():
    validate_documentation(_definition)


def get_algorithm(
    key: str,
    *,
    code_version: str | None = None,
    include_inactive: bool = False,
) -> AlgorithmDefinition | None:
    definition = (
        ALGORITHM_VERSION_REGISTRY.get((key, code_version))
        if code_version is not None
        else ALGORITHM_REGISTRY.get(key)
    )
    if definition is None or (not definition.active and not include_inactive):
        return None
    return definition
