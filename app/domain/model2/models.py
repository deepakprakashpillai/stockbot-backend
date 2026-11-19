"""Strategy-owned four-leg selections and price evidence, without persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.model2.calendar import WeeklyCycle
from app.domain.model2.market import CompletedObservation, Model2Contract, OpenObservation
from app.domain.model2.numeric import finite_decimal


class LegRole(StrEnum):
    SHORT_CALL = "SHORT_CALL"
    LONG_CALL = "LONG_CALL"
    SHORT_PUT = "SHORT_PUT"
    LONG_PUT = "LONG_PUT"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class SelectedLeg:
    role: LegRole
    contract: Model2Contract

    @property
    def entry_side(self) -> Side:
        return Side.SELL if self.role in {LegRole.SHORT_CALL, LegRole.SHORT_PUT} else Side.BUY


@dataclass(frozen=True)
class CondorSelection:
    cycle: WeeklyCycle
    spot: Decimal
    vix: Decimal
    width: int
    legs: tuple[SelectedLeg, ...]

    def __post_init__(self) -> None:
        finite_decimal(self.spot, "Selection spot", positive=True)
        finite_decimal(self.vix, "Selection VIX", positive=True)
        object.__setattr__(self, "legs", tuple(self.legs))
        if self.cycle.classification != "READY" or self.width not in {150, 200}:
            raise ValueError("A condor needs a complete cycle and an approved width.")
        if tuple(leg.role for leg in self.legs) != tuple(LegRole):
            raise ValueError("Exactly four uniquely ordered condor roles are required.")
        if len({leg.contract.contract_id for leg in self.legs}) != 4:
            raise ValueError("All four option contracts must be distinct.")
        if any(leg.contract.expiry != self.cycle.expiry for leg in self.legs):
            raise ValueError("Every leg must retain the cycle expiry.")
        if len({leg.contract.lot_size for leg in self.legs}) != 1:
            raise ValueError("All legs must have the same historical lot size.")
        short_call, long_call, short_put, long_put = (leg.contract for leg in self.legs)
        if (
            short_call.option_type,
            long_call.option_type,
            short_put.option_type,
            long_put.option_type,
        ) != ("CE", "CE", "PE", "PE"):
            raise ValueError("Condor roles must match call/put types.")
        if (
            long_call.strike - short_call.strike != self.width
            or short_put.strike - long_put.strike != self.width
            or short_call.strike <= short_put.strike
        ):
            raise ValueError("Invalid condor wing geometry.")

    @property
    def quantity(self) -> int:
        return self.legs[0].contract.lot_size  # Exactly one lot.


@dataclass(frozen=True)
class MissingContract:
    role: LegRole
    strike: int
    option_type: str


@dataclass(frozen=True)
class SelectionDecision:
    selection: CondorSelection | None
    reason: str | None
    missing: tuple[MissingContract, ...] = ()


@dataclass(frozen=True)
class FilterDecision:
    allowed: bool
    state: str
    experimental: bool


@dataclass(frozen=True)
class LegOpeningQuote:
    contract_id: str
    opening: OpenObservation | None
    preceding: CompletedObservation | None


@dataclass(frozen=True)
class OpeningQuotes:
    execution_at: datetime
    preceding_end: datetime
    as_of: datetime
    legs: tuple[LegOpeningQuote, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "legs", tuple(self.legs))


@dataclass(frozen=True)
class LegFill:
    role: LegRole
    contract: Model2Contract
    side: Side
    reference_price: Decimal
    fill_price: Decimal
    quantity: int


@dataclass(frozen=True)
class EntryEstimate:
    selection: CondorSelection
    execution_at: datetime
    legs: tuple[LegFill, ...]
    credit_points: Decimal
    charges_inr: Decimal
    maximum_loss_points: Decimal
    maximum_loss_inr: Decimal | None
    assumed_margin_inr: Decimal | None
    hard_limit_inr: Decimal
    margin_limit_inr: Decimal
    target_risk_exceeded: bool
    slippage_percent: Decimal


@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    reason: str | None
    retryable: bool
    estimate: EntryEstimate | None
    invalid_contract_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CloseValuation:
    exit_debit_points: Decimal
    gain_points: Decimal
    gross_pnl_inr: Decimal
    charges_inr: Decimal
    net_pnl_inr: Decimal
    leg_gross_pnl_inr: tuple[Decimal, ...]
    target_reached: bool
    stop_reached: bool
    exit_fill_prices: tuple[Decimal, ...] = ()
