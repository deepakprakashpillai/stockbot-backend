from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class BacktestPreflightRequest(BaseModel):
    dataset_version_id: UUID
    algorithm_version_id: UUID
    start_date: date
    end_date: date
    starting_capital: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    warning_acknowledged: bool = False
    accepted_coverage_digest: str | None = Field(default=None, min_length=64, max_length=64)


CoverageStatus = Literal["COMPLETE", "PARTIAL", "BLOCKED"]
DayClassification = Literal[
    "INCLUDED",
    "EXCLUDED_DATA_GAP",
    "EXCLUDED_WARMUP_GAP",
    "NO_QUALIFYING_SIGNAL",
    "SIGNAL_REJECTED",
    "TRADED",
]


class CoverageExclusionResponse(BaseModel):
    session_date: date
    classification: Literal["EXCLUDED_DATA_GAP", "EXCLUDED_WARMUP_GAP"]
    reason_code: str
    details: dict[str, Any] = Field(default_factory=dict)


class BacktestPreflightResponse(BaseModel):
    allowed: bool
    mode: str
    dataset_version_id: UUID
    algorithm_version_id: UUID
    requested_start_date: date
    requested_end_date: date
    usable_session_count: int
    estimated_row_count: int
    required_capabilities: list[str]
    missing_capabilities: list[str]
    acknowledgement_required: bool
    warnings: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    coverage_status: CoverageStatus = "COMPLETE"
    coverage_digest: str = ""
    requested_session_count: int = Field(default=0, ge=0)
    included_session_count: int = Field(default=0, ge=0)
    excluded_session_count: int = Field(default=0, ge=0)
    excluded_sessions: list[CoverageExclusionResponse] = Field(default_factory=list)


class BacktestCreateRequest(BacktestPreflightRequest):
    pass


class ReasonSummaryResponse(BaseModel):
    raw_code: str
    user_facing_label: str
    supporting_points: list[str] = Field(max_length=3)
    structured_explanation_available: bool


class RunOutcomeSummaryResponse(BaseModel):
    net_outcome: Decimal
    max_drawdown_amount: Decimal
    trade_count: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    mode: str = "PLATFORM_DEMO"
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False


class BacktestRunResponse(BaseModel):
    id: UUID
    dataset_version_id: UUID
    algorithm_version_id: UUID
    start_date: date
    end_date: date
    mode: str
    starting_capital: Decimal
    status: str
    progress: int
    cancellation_requested: bool
    error_code: str | None
    error_message: str | None
    snapshot: dict[str, Any]
    pricing_quality: str | None = None
    executable_quotes: bool | None = None
    estimate_limitations: list[str] = Field(default_factory=list)
    outcome_summary: RunOutcomeSummaryResponse | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    active_attempt_id: UUID | None = None
    coverage_status: CoverageStatus | None = None
    coverage_digest: str | None = None
    included_session_count: int | None = None
    excluded_session_count: int | None = None
    compatibility_warnings: list[str] = Field(default_factory=list)


class BacktestCatalogRunResponse(BaseModel):
    id: UUID
    dataset_version_id: UUID
    dataset_name: str
    dataset_version_number: int
    algorithm_version_id: UUID
    algorithm_key: str
    algorithm_display_name: str
    algorithm_version_name: str
    algorithm_version_number: int
    code_version: str
    start_date: date
    end_date: date
    mode: str
    status: str
    progress: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: int | None
    metrics_summary: dict[str, Any]
    outcome_summary: RunOutcomeSummaryResponse | None = None
    synthetic: bool
    unit: str
    financial_pnl_available: bool


class BacktestHistoryBucketResponse(BaseModel):
    date: date
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    active: int = Field(ge=0)


class BacktestHistoryUsageResponse(BaseModel):
    key: str
    label: str
    count: int = Field(ge=0)


class BacktestHistorySummaryResponse(BaseModel):
    total_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    active_runs: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=100)
    median_duration_seconds: int | None
    status_counts: dict[str, int]
    activity: list[BacktestHistoryBucketResponse]
    dataset_usage: list[BacktestHistoryUsageResponse]
    algorithm_usage: list[BacktestHistoryUsageResponse]
    synthetic: bool
    unit: str
    financial_pnl_available: bool


class BacktestRunListResponse(BaseModel):
    items: list[BacktestCatalogRunResponse]
    next_cursor: UUID | None
    total_count: int = Field(ge=0)


class SignalEvaluationResponse(BaseModel):
    id: UUID
    sequence: int
    timestamp: datetime
    session_date: date
    decision: str
    rejection_reason: str | None
    window: str | None
    score: int | None
    inputs: dict[str, Any]
    entry_trade_ids: list[UUID] = Field(default_factory=list)


class SignalEvaluationListResponse(BaseModel):
    items: list[SignalEvaluationResponse]
    next_cursor: UUID | None


class SignalMarketContextResponse(BaseModel):
    key: str
    label: str
    exact_value: Any | None = None
    display_unit: str | None = None
    source: str
    available: bool


class SignalEvaluationStepResponse(BaseModel):
    step_id: str
    logic_step_id: str
    order: int = Field(ge=1)
    kind: Literal["input", "gate", "confirmation", "decision", "output"]
    label: str
    status: Literal["passed", "failed", "not_evaluated", "informational"]
    observed_value: Any | None = None
    comparator: str | None = None
    threshold_value: Any | None = None
    unit: str | None = None
    score_contribution: int
    reason_code: str | None = None
    evidence: dict[str, Any]
    experimental: bool


class SignalDiagnosticResponse(BaseModel):
    schema_version: str
    algorithm_key: str
    code_version: str
    parameter_version_id: str
    algorithm_logic_document_version: str
    decision: str
    primary_reason: str
    window: str | None
    score: int | None
    market_context: list[SignalMarketContextResponse]
    evaluation_steps: list[SignalEvaluationStepResponse]
    parameter_snapshot: dict[str, Any]
    related_trade_id: UUID | None = None
    limitations: list[str]


class SignalRunIdentityResponse(BaseModel):
    run_id: UUID
    dataset_version_id: UUID
    algorithm_version_id: UUID
    algorithm_key: str
    code_version: str
    mode: str
    synthetic: bool = True
    unit: str | None = None
    financial_pnl_available: bool = False


class RelatedSignalTradeResponse(BaseModel):
    id: UUID
    sequence: int = Field(gt=0)
    entry_timestamp: datetime
    exit_timestamp: datetime
    exit_reason: str
    net_demo_units: Decimal
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False


class ExecutionRejectionResponse(BaseModel):
    timestamp: datetime
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class SignalDecisionDetailResponse(BaseModel):
    signal: SignalEvaluationResponse
    run_identity: SignalRunIdentityResponse
    diagnostic: SignalDiagnosticResponse | None = None
    structured_explanation_available: bool
    raw_evidence: dict[str, Any] | None = None
    limitations: list[str]
    previous_signal_id: UUID | None = None
    next_signal_id: UUID | None = None
    related_trade: RelatedSignalTradeResponse | None = None
    execution_rejection: ExecutionRejectionResponse | None = None


class LiveRunSnapshotResponse(BaseModel):
    """A bounded, durable checkpoint used to recover a live Studio viewport."""

    run_id: UUID
    status: str
    progress: int
    processed_candles: int
    total_candles: int
    last_processed_candle_at: datetime | None
    latest_signal_sequence: int
    latest_trade_sequence: int
    latest_equity_sequence: int
    latest_event_sequence: int
    checkpointed_at: datetime | None
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False


class RunEventResponse(BaseModel):
    id: UUID
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunEventListResponse(BaseModel):
    items: list[RunEventResponse]
    next_cursor: int | None


class ExecutionAttemptResponse(BaseModel):
    id: UUID
    attempt_number: int = Field(gt=0)
    status: str
    worker_owner: str | None
    legacy: bool
    reason: str | None
    metadata_snapshot: dict[str, Any]
    started_at: datetime
    completed_at: datetime | None


class ExecutionAttemptListResponse(BaseModel):
    active_attempt_id: UUID | None
    items: list[ExecutionAttemptResponse]


class JournalEventResponse(BaseModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    sequence: int = Field(gt=0)
    timestamp: datetime
    session_date: date
    event_type: str
    position_state: Literal["OPEN", "CHECKING", "CLOSED"] | None = None
    signal_sequence: int | None = None
    trade_sequence: int | None = None
    payload: dict[str, Any]
    created_at: datetime


class JournalEventListResponse(BaseModel):
    next_market_cursor: str | None = None
    run_id: UUID
    attempt_id: UUID | None
    items: list[JournalEventResponse]
    next_cursor: int | None
    legacy_detail_unavailable: bool = False


class RunDayClassificationResponse(BaseModel):
    id: UUID
    session_date: date
    classification: DayClassification
    included: bool
    primary_reason: str | None
    evidence: dict[str, Any]


class RunDayClassificationListResponse(BaseModel):
    run_id: UUID
    attempt_id: UUID | None
    items: list[RunDayClassificationResponse]
    next_cursor: date | None
    counts: dict[str, int]


class RunMetricsResponse(BaseModel):
    run_id: UUID
    metrics: dict[str, Any]


class DiagnosticGroupResponse(BaseModel):
    category: str
    sample_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    gross_outcome: Decimal
    costs: Decimal
    net_outcome: Decimal


class RunDiagnosticsResponse(BaseModel):
    run_id: UUID
    unit: str
    groups: dict[str, list[DiagnosticGroupResponse]]
    adverse_movement_amount: Decimal
    cost_drag_amount: Decimal


class AnalyticsSafeguardsResponse(BaseModel):
    mode: str = "PLATFORM_DEMO"
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False
    realized_only: bool = True
    attribution: str = "EXIT_TIMESTAMP"
    timezone: str


class AnalyticsRatioResponse(BaseModel):
    value: Decimal | None
    availability: Literal["AVAILABLE", "UNAVAILABLE"]
    reason: str | None


class AnalyticsSummaryDataResponse(BaseModel):
    total_trades: int
    wins: int
    losses: int
    breakevens: int
    eligible_sessions: int
    active_sessions: int
    gross_outcome: Decimal | None = None
    total_costs: Decimal
    net_outcome: Decimal
    cumulative_net_outcome: Decimal
    win_rate: Decimal
    loss_rate: Decimal
    breakeven_rate: Decimal
    average_trade: Decimal
    median_trade: Decimal
    average_win: Decimal
    average_loss: Decimal
    largest_win: Decimal
    largest_loss: Decimal
    expectancy: Decimal
    profit_factor: AnalyticsRatioResponse
    payoff_ratio: AnalyticsRatioResponse
    max_consecutive_wins: int
    max_consecutive_losses: int
    average_duration_seconds: Decimal
    median_duration_seconds: Decimal
    average_winning_duration_seconds: Decimal
    average_losing_duration_seconds: Decimal
    max_drawdown_amount: Decimal
    drawdown_peak_date: date | None
    drawdown_trough_date: date | None
    drawdown_recovery_date: date | None
    drawdown_peak_timestamp: datetime | None
    drawdown_trough_timestamp: datetime | None
    drawdown_recovery_timestamp: datetime | None


class RunAnalyticsSummaryResponse(BaseModel):
    run_id: UUID
    schema_version: str
    calculator_version: str
    source_fingerprint: str
    safeguards: AnalyticsSafeguardsResponse
    summary: AnalyticsSummaryDataResponse


class AnalyticsPeriodResponse(BaseModel):
    granularity: Literal["TRADE", "DAY", "MONTH"]
    period_start: date
    period_end: date
    partial: bool
    eligible_session_count: int
    trade_count: int
    wins: int
    losses: int
    breakevens: int
    gross_outcome: Decimal | None = None
    costs: Decimal | None = None
    net_outcome: Decimal | None = None
    cumulative_net_outcome: Decimal
    equity_end: Decimal
    local_max_drawdown: Decimal
    run_drawdown_end: Decimal
    sequence: int | None = None
    entry_timestamp: datetime | None = None
    exit_timestamp: datetime | None = None
    exit_reason: str | None = None
    trade_id: UUID | None = None
    entry_signal_evaluation_id: UUID | None = None
    entry_reason_summary: ReasonSummaryResponse | None = None
    exit_reason_summary: ReasonSummaryResponse | None = None


class AnalyticsPeriodListResponse(BaseModel):
    run_id: UUID
    granularity: Literal["TRADE", "DAY", "MONTH"]
    purpose: Literal["chart", "table"]
    items: list[AnalyticsPeriodResponse]
    next_cursor: int | date | None
    original_count: int
    returned_count: int
    aggregated: bool
    resolution: str
    warnings: list[str] = Field(default_factory=list)
    safeguards: AnalyticsSafeguardsResponse


class AnalyticsDefinitionResponse(BaseModel):
    key: str
    label: str
    definition: str
    formula: str
    availability: str
    unit_semantics: str


class AnalyticsDefinitionsResponse(BaseModel):
    schema_version: str
    safeguards: AnalyticsSafeguardsResponse
    items: list[AnalyticsDefinitionResponse]


class DemoTradeLegResponse(BaseModel):
    id: UUID
    leg_number: int = Field(ge=1, le=2)
    name: str
    entry_reference: Decimal
    exit_reference: Decimal
    demo_units: Decimal
    gross_outcome: Decimal | None = None
    synthetic: bool = True
    unit: Literal["DEMO_UNITS", "INR"] = "DEMO_UNITS"
    financial_pnl_available: bool = False


class DemoTradeResponse(BaseModel):
    id: UUID
    sequence: int = Field(gt=0)
    entry_signal_evaluation_id: UUID | None = None
    entry_timestamp: datetime
    exit_timestamp: datetime
    exit_reason: str
    entry_reason_summary: ReasonSummaryResponse | None = None
    exit_reason_label: str | None = None
    quantity: Decimal
    entry_reference: Decimal
    exit_reference: Decimal
    gross_demo_units: Decimal
    costs_demo_units: Decimal
    net_demo_units: Decimal
    gross_outcome: Decimal | None = None
    costs: Decimal | None = None
    net_outcome: Decimal | None = None
    pricing_quality: str | None = None
    executable_quotes: bool | None = None
    estimate_limitations: list[str] = Field(default_factory=list)
    synthetic: bool = True
    unit: Literal["DEMO_UNITS", "INR"] = "DEMO_UNITS"
    financial_pnl_available: bool = False
    assumptions: dict[str, Any]
    legs: list[DemoTradeLegResponse] = Field(min_length=2, max_length=2)


class DemoTradeListResponse(BaseModel):
    items: list[DemoTradeResponse]
    next_cursor: int | None


class DemoEquityPointResponse(BaseModel):
    id: UUID
    sequence: int = Field(gt=0)
    timestamp: datetime
    equity_demo_units: Decimal
    drawdown_demo_units: Decimal = Field(le=0)
    equity_amount: Decimal | None = None
    drawdown_amount: Decimal | None = Field(default=None, le=0)
    synthetic: bool = True
    unit: Literal["DEMO_UNITS", "INR"] = "DEMO_UNITS"
    financial_pnl_available: bool = False


class DemoEquityPointListResponse(BaseModel):
    items: list[DemoEquityPointResponse]
    next_cursor: int | None


class ComparisonRequest(BaseModel):
    run_ids: list[UUID] = Field(min_length=2, max_length=10)


class ComparisonIssue(BaseModel):
    code: str
    message: str
    run_ids: list[UUID] = Field(default_factory=list)


class ComparisonPreflightResponse(BaseModel):
    allowed: bool
    run_ids: list[UUID]
    mode: str | None = "PLATFORM_DEMO"
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False
    issues: list[ComparisonIssue]
    warnings: list[ComparisonIssue]
    performance_comparable: bool = False
    comparison_scope: Literal["PERFORMANCE", "DIAGNOSTIC_ONLY"] = "DIAGNOSTIC_ONLY"


class ComparisonRunResponse(BaseModel):
    run_id: UUID
    dataset_version_id: UUID
    algorithm_version_id: UUID
    start_date: date
    end_date: date
    status: str
    code_version: str
    parameters: dict[str, Any]
    assumptions: dict[str, Any]
    metrics: dict[str, Any]
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False


class ComparisonResponse(BaseModel):
    run_ids: list[UUID]
    mode: str = "PLATFORM_DEMO"
    synthetic: bool = True
    unit: str = "DEMO_UNITS"
    financial_pnl_available: bool = False
    notice: str
    parameter_differences: dict[str, dict[str, Any]]
    assumption_differences: dict[str, dict[str, Any]]
    runs: list[ComparisonRunResponse]
    performance_comparable: bool = False
    comparison_scope: Literal["PERFORMANCE", "DIAGNOSTIC_ONLY"] = "DIAGNOSTIC_ONLY"


ComparisonSeriesName = Literal[
    "equity",
    "drawdown",
    "trade_outcomes",
    "signal_decisions",
    "rejection_reasons",
]


class ComparisonSeriesRequest(ComparisonRequest):
    """A bounded, lazy read for visual comparison data."""

    series: list[ComparisonSeriesName] = Field(min_length=1, max_length=5)
    start: datetime | None = None
    end: datetime | None = None
    resolution: Literal["auto", "raw"] = "auto"
    max_points_per_run: int = Field(default=500, ge=10, le=1_000)


class ComparisonSeriesPoint(BaseModel):
    timestamp: datetime
    sequence: int = Field(gt=0)
    value: Decimal


class ComparisonCategoryCount(BaseModel):
    category: str
    count: int = Field(ge=0)


class ComparisonRunSeriesResponse(BaseModel):
    run_id: UUID
    equity: list[ComparisonSeriesPoint] = Field(default_factory=list)
    drawdown: list[ComparisonSeriesPoint] = Field(default_factory=list)
    trade_outcomes: list[ComparisonSeriesPoint] = Field(default_factory=list)
    signal_decisions: list[ComparisonCategoryCount] = Field(default_factory=list)
    rejection_reasons: list[ComparisonCategoryCount] = Field(default_factory=list)
    original_point_counts: dict[str, int] = Field(default_factory=dict)
    returned_point_counts: dict[str, int] = Field(default_factory=dict)


class ComparisonSeriesResponse(BaseModel):
    comparison_hash: str
    run_ids: list[UUID]
    mode: str
    synthetic: bool
    unit: str
    financial_pnl_available: bool
    requested_series: list[ComparisonSeriesName]
    effective_start: datetime | None
    effective_end: datetime | None
    resolution: str
    max_points_per_run: int
    warnings: list[ComparisonIssue]
    category_definitions: dict[str, list[str]]
    runs: list[ComparisonRunSeriesResponse]


class ExportMetadata(BaseModel):
    schema_version: Literal["stockbot.platform-demo-export.v1", "stockbot.backtest-export.v2"]
    exported_at: datetime
    kind: Literal["summary", "trades", "signals", "analytics"]
    run_id: UUID
    mode: str
    synthetic: bool
    unit: Literal["DEMO_UNITS", "INR"]
    financial_pnl_available: bool
    notice: str
    snapshot: dict[str, Any]
    research_view: AnalyticsExportContext | None = None


class AnalyticsExportContext(BaseModel):
    """Immutable context needed to reproduce one exact analytics export."""

    granularity: Literal["TRADE", "DAY", "MONTH"]
    start_date: date | None
    end_date: date | None
    include_zero_periods: bool
    sort: Literal["PERIOD_ASC", "PERIOD_DESC"]
    schema_version: str
    calculator_version: str
    source_fingerprint: str
    timezone: str
    attribution: Literal["EXIT_TIMESTAMP"]
    reconciliation: Literal["PASSED", "NOT_APPLICABLE"]


class JsonExportResponse(BaseModel):
    metadata: ExportMetadata
    rows: list[dict[str, Any]]


class ExportJobCreateRequest(BaseModel):
    format: Literal["ZIP", "XLSX"] = "ZIP"


class ExportJobResponse(BaseModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID | None
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    format: Literal["ZIP", "XLSX"]
    sha256: str | None
    row_counts: dict[str, int]
    error_code: str | None
    error_message: str | None
    download_ready: bool
    created_at: datetime
    completed_at: datetime | None


class ExperimentPlanCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    hypothesis: str = Field(min_length=1, max_length=5000)
    baseline_algorithm_version_id: UUID
    dataset_version_id: UUID
    parameter_key: str = Field(min_length=1, max_length=100)
    parameter_values: list[Any] = Field(min_length=1, max_length=9)
    development_start_date: date
    development_end_date: date
    validation_start_date: date
    validation_end_date: date
    coverage_digest: str = Field(min_length=64, max_length=64)
    starting_capital: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    notes: str | None = Field(default=None, max_length=5000)


class ExperimentPlanResponse(BaseModel):
    id: UUID
    name: str
    hypothesis: str
    algorithm_key: str
    baseline_algorithm_version_id: UUID
    dataset_version_id: UUID
    parameter_key: str
    parameter_values: list[Any]
    development_start_date: date
    development_end_date: date
    validation_start_date: date
    validation_end_date: date
    coverage_digest: str
    starting_capital: Decimal
    notes: str | None
    status: str
    validation_unlocked_at: datetime | None
    selected_algorithm_version_id: UUID | None
    created_at: datetime


class ExperimentPreflightResponse(BaseModel):
    allowed: bool
    coverage_digest: str
    variants: list[dict[str, Any]]
    issues: list[str]


class ExperimentValidationUnlockRequest(BaseModel):
    selected_algorithm_version_id: UUID
    accepted_coverage_digest: str = Field(min_length=64, max_length=64)


class ExperimentRunResponse(BaseModel):
    id: UUID
    backtest_run_id: UUID
    variant_index: int = Field(ge=0, lt=10)
    parameter_value: Any
    phase: Literal["DEVELOPMENT", "VALIDATION"]
    algorithm_version_id: UUID
    run_status: str
    outcome_metrics: dict[str, Any]


class ExperimentBatchResponse(BaseModel):
    plan: ExperimentPlanResponse
    runs: list[ExperimentRunResponse]
