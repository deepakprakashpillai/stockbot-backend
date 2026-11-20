"""Source-defined strike/contract selection and the explicitly experimental filter."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Protocol

from app.domain.model2.calendar import WeeklyCycle
from app.domain.model2.market import Model2Contract, OptionType
from app.domain.model2.models import (
    CondorSelection,
    FilterDecision,
    LegRole,
    MissingContract,
    SelectedLeg,
    SelectionDecision,
)
from app.domain.model2.numeric import finite_decimal
from app.domain.model2.parameters import Model2Parameters


class ContractLookup(Protocol):
    def contract(
        self, expiry: date, strike: int, option_type: OptionType
    ) -> Model2Contract | None: ...


def round_short_strike(value: Decimal, option_type: OptionType) -> int:
    finite_decimal(value, "Raw strike", positive=True)
    if option_type not in {"CE", "PE"}:
        raise ValueError("Unknown option type.")
    rounding = ROUND_HALF_UP if option_type == "CE" else ROUND_HALF_DOWN
    return int((value / 50).to_integral_value(rounding=rounding)) * 50


def short_strikes(spot: Decimal, vix: Decimal) -> tuple[int, int]:
    finite_decimal(spot, "Spot", positive=True)
    finite_decimal(vix, "VIX", positive=True)
    with localcontext() as context:
        context.prec = 42
        sd = vix / 100 / Decimal(252).sqrt()
        call = round_short_strike(spot * (1 + sd), "CE")
        put = round_short_strike(spot * (1 - sd), "PE")
    return call, put


def select_condor(
    cycle: WeeklyCycle,
    spot: Decimal,
    vix: Decimal,
    parameters: Model2Parameters,
    lookup: ContractLookup,
) -> SelectionDecision:
    finite_decimal(spot, "Spot", positive=True)
    finite_decimal(vix, "VIX", positive=True)
    if cycle.classification != "READY":
        return SelectionDecision(None, cycle.classification)
    if vix > 28:
        return SelectionDecision(None, "VIX_LIMIT")
    call, put = short_strikes(spot, vix)
    width = parameters.wing_width_points
    requested: tuple[tuple[LegRole, int, OptionType], ...] = (
        (LegRole.SHORT_CALL, call, "CE"),
        (LegRole.LONG_CALL, call + width, "CE"),
        (LegRole.SHORT_PUT, put, "PE"),
        (LegRole.LONG_PUT, put - width, "PE"),
    )
    selected = []
    missing = []
    for role, strike, kind in requested:
        contract = lookup.contract(cycle.expiry, strike, kind)
        if contract is None:
            missing.append(MissingContract(role, strike, kind))
        else:
            if (contract.expiry, contract.strike, contract.option_type) != (
                cycle.expiry,
                strike,
                kind,
            ):
                raise ValueError("Contract lookup returned a substituted or mismatched contract.")
            selected.append(SelectedLeg(role, contract))
    if missing:
        return SelectionDecision(None, "MISSING_CONTRACT", tuple(missing))
    return SelectionDecision(CondorSelection(cycle, spot, vix, width, tuple(selected)), None)


def vix_state_filter(enabled: bool, current: Decimal, previous: Decimal | None) -> FilterDecision:
    if not enabled:
        return FilterDecision(True, "OFF", False)
    finite_decimal(current, "Current VIX", positive=True)
    if previous is None:
        return FilterDecision(False, "UNKNOWN", True)
    finite_decimal(previous, "Previous VIX", positive=True)
    rose = current >= previous
    return FilterDecision(rose, "ROSE_OR_EQUAL" if rose else "FELL", True)
