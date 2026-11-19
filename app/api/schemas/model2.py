from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.backtests import BacktestRunResponse


class ScenarioMemberResponse(BaseModel):
    scope: str | None = None
    reliability_status: str | None = None
    priced_cycle_count: int | None = None
    excluded_cycle_count: int | None = None
    ordinal: int
    policy: Literal["MAX_LOSS_RISK", "ZERO_RISK", "HALT_AT_FIRST_EXCLUSION"]
    run: BacktestRunResponse
    primary: bool


class ResearchScenarioGroupResponse(BaseModel):
    id: UUID
    status: str
    cancellation_requested: bool
    created_at: datetime
    completed_at: datetime | None
    request_digest: str
    snapshot: dict[str, Any]
    members: list[ScenarioMemberResponse] = Field(min_length=3, max_length=3)


class ResearchScenarioGroupListResponse(BaseModel):
    items: list[ResearchScenarioGroupResponse]
    next_cursor: UUID | None


class Model2CycleResponse(BaseModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    expiry: date
    entry_date: date
    state: str
    reason: str | None
    last_sequence: int
    trade_id: UUID | None
    payload: dict[str, Any]


class Model2CycleListResponse(BaseModel):
    items: list[Model2CycleResponse]
    active_attempt_id: UUID | None
    next_cursor: date | None


class Model2JournalEventResponse(BaseModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    sequence: int = Field(gt=0)
    timestamp: datetime
    session_date: date
    event_type: str
    position_state: str | None
    signal_sequence: int | None
    trade_sequence: int | None
    payload: dict[str, Any]
    created_at: datetime


class Model2JournalListResponse(BaseModel):
    run_id: UUID
    attempt_id: UUID | None
    items: list[Model2JournalEventResponse]
    next_cursor: str | None


class OptionLegResponse(BaseModel):
    role: Literal["SHORT_CALL", "LONG_CALL", "SHORT_PUT", "LONG_PUT"]
    contract_id: str
    symbol: str
    option_type: Literal["CE", "PE"]
    strike: int
    expiry: date
    lot_size: int
    tick_size: Decimal
    quantity: int
    side: Literal["BUY", "SELL"]
    entry_reference_price: Decimal
    entry_fill_price: Decimal
    exit_fill_price: Decimal | None
    gross_pnl_inr: Decimal | None = None


class OptionPositionResponse(BaseModel):
    id: UUID
    trade_id: UUID | None
    state: str
    expiry: date
    entry_at: datetime
    exit_at: datetime | None
    finalized_at: datetime | None
    reason: str | None
    original_trigger: dict[str, Any] | None
    entry_credit_points: Decimal
    maximum_loss_inr: Decimal | None
    assumed_margin_inr: Decimal | None
    margin_source: Literal["ASSUMED"] = "ASSUMED"
    charges_inr: Decimal
    gross_pnl_inr: Decimal | None
    net_pnl_inr: Decimal | None
    priced_performance_included: bool
    flags: list[str]
    legs: list[OptionLegResponse] = Field(min_length=4, max_length=4)


class OptionPositionListResponse(BaseModel):
    items: list[OptionPositionResponse]
    active_attempt_id: UUID | None
    next_cursor: date | None


class Model2RiskEntryResponse(BaseModel):
    cycle_id: UUID
    booking_date: date
    source: Literal["PRICED_RESULT", "ASSUMED_MAX_LOSS", "ASSUMED_ZERO", "HALTED_AT_EXCLUSION"]
    risk_amount: Decimal | None
    priced_performance_amount: Decimal | None
    counters: dict[str, Any]


class Model2RiskLedgerResponse(BaseModel):
    items: list[Model2RiskEntryResponse]
    active_attempt_id: UUID | None
    next_cursor: date | None


class Model2AssessmentResponse(BaseModel):
    run_id: UUID
    policy_version: str
    run_status: str
    scope: str
    reliability: dict[str, Any] | None
    overall_status: str
    criteria: list[dict[str, Any]]
    summary: dict[str, Any] | None
    priced_statistics: dict[str, Any]
    limitations: list[str]


class Model2DatasetManifestResponse(BaseModel):
    dataset_version_id: UUID
    input_checksum: str
    bundle_checksum: str
    calendar_version: str
    calendar_sha256: str
    components: list[dict[str, Any]]
    official_settlement_available: bool
    halt_source_available: bool
    limitations: list[str]


class ScenarioComparisonOutcomeResponse(BaseModel):
    policy: Literal["MAX_LOSS_RISK", "ZERO_RISK", "HALT_AT_FIRST_EXCLUSION"]
    run_id: UUID
    cycle_id: UUID | None
    state: str
    reason: str | None
    priced_net_pnl_inr: Decimal | None
    risk_contribution: dict[str, Any] | None
    scope: str | None


class ScenarioComparisonCycleResponse(BaseModel):
    expiry: date
    outcomes: list[ScenarioComparisonOutcomeResponse] = Field(min_length=3, max_length=3)


class ScenarioCycleComparisonResponse(BaseModel):
    group_id: UUID
    items: list[ScenarioComparisonCycleResponse]
    next_cursor: date | None
    scenario_populations_are_independent: bool = True
