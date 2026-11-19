"""Synchronized four-leg estimated fills and exact credit-spread arithmetic."""

from __future__ import annotations

from datetime import datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.domain.model2.approved_specification import decimal_rule
from app.domain.model2.calendar import Model2Calendar
from app.domain.model2.market import (
    BAR_INTERVAL,
    Model2PriceFeed,
    ObservationNotAvailable,
    SettlementObservation,
    exchange_time,
)
from app.domain.model2.models import (
    CloseValuation,
    CondorSelection,
    EntryDecision,
    EntryEstimate,
    LegFill,
    LegOpeningQuote,
    OpeningQuotes,
    Side,
)
from app.domain.model2.numeric import finite_decimal, inr
from app.domain.model2.parameters import Model2Parameters
from app.domain.model2.risk import risk_budget, round_trip_charges
from app.domain.model2.selection import vix_state_filter


def adverse_fill(
    reference: Decimal, tick: Decimal, side: Side, slippage_percent: Decimal
) -> Decimal:
    finite_decimal(reference, "Fill reference", positive=True)
    finite_decimal(tick, "Contract tick", positive=True)
    finite_decimal(slippage_percent, "Slippage")
    if slippage_percent not in (Decimal(0), Decimal(1), Decimal(2)) or side not in {
        Side.BUY,
        Side.SELL,
    }:
        raise ValueError("Only approved slippage values and explicit buy/sell sides are supported.")
    factor = 1 + slippage_percent / 100 if side == Side.BUY else 1 - slippage_percent / 100
    rounding = ROUND_CEILING if side == Side.BUY else ROUND_FLOOR
    return (reference * factor / tick).to_integral_value(rounding=rounding) * tick


def collect_opening_quotes(
    feed: Model2PriceFeed,
    calendar: Model2Calendar,
    selection: CondorSelection,
    execution_at: datetime,
) -> OpeningQuotes:
    at = exchange_time(execution_at)
    if at.date() not in {selection.cycle.entry_session, selection.cycle.expiry}:
        raise ValueError("Opening quotes must belong to the pinned two-session position.")
    previous = calendar.previous_completed_at(at)
    return OpeningQuotes(
        at,
        previous,
        at,
        tuple(
            LegOpeningQuote(
                leg.contract.contract_id,
                feed.option_open(leg.contract, at, as_of=at),
                feed.option_close(leg.contract, previous, as_of=at),
            )
            for leg in selection.legs
        ),
    )


def invalid_opening_contracts(selection: CondorSelection, quotes: OpeningQuotes) -> tuple[str, ...]:
    at, previous, as_of = (
        exchange_time(stamp) for stamp in (quotes.execution_at, quotes.preceding_end, quotes.as_of)
    )
    if as_of != at or previous > at:
        raise ObservationNotAvailable("Fill decisions must use their own opening-time information.")
    if at.time() != time(9, 15) and previous != at:
        raise ValueError("Intraday fills require the immediately preceding completed candle.")
    if at.time() == time(9, 15) and not (
        previous.date() < at.date() and previous.time() == time(15, 30)
    ):
        raise ValueError("The session open needs the previous session's final completed candle.")
    expected_ids = tuple(leg.contract.contract_id for leg in selection.legs)
    if tuple(leg.contract_id for leg in quotes.legs) != expected_ids:
        raise ValueError("Quotes must retain the four selected contract identities and role order.")
    invalid = []
    for quote in quotes.legs:
        opening, preceding = quote.opening, quote.preceding
        if (
            opening is not None
            and opening.available_at > as_of
            or preceding is not None
            and preceding.available_at > as_of
        ):
            raise ObservationNotAvailable("A quote contains fields unavailable at the fill time.")
        if (
            opening is None
            or preceding is None
            or opening.interval_start != at
            or opening.interval_end != at + BAR_INTERVAL
            or preceding.interval_end != previous
            or preceding.interval_start != previous - BAR_INTERVAL
            or not opening.price.is_finite()
            or opening.price <= 0
            or not preceding.close.is_finite()
            or preceding.close <= 0
            or preceding.volume is None
            or preceding.volume <= 0
        ):
            invalid.append(quote.contract_id)
    return tuple(invalid)


def _opening_fills(
    selection: CondorSelection, quotes: OpeningQuotes, parameters: Model2Parameters
) -> tuple[LegFill, ...]:
    result = []
    for selected, observed in zip(selection.legs, quotes.legs, strict=True):
        assert observed.opening is not None
        side = selected.entry_side
        reference = observed.opening.price
        result.append(
            LegFill(
                selected.role,
                selected.contract,
                side,
                reference,
                adverse_fill(
                    reference, selected.contract.tick_size, side, parameters.slippage_percent
                ),
                selection.quantity,
            )
        )
    return tuple(result)


def evaluate_entry(
    selection: CondorSelection,
    quotes: OpeningQuotes,
    parameters: Model2Parameters,
    starting_capital: Decimal,
    *,
    previous_vix: Decimal | None = None,
) -> EntryDecision:
    """Evaluate fill/capital rules after the caller's scheduled-cycle risk check."""
    if selection.width != parameters.wing_width_points:
        raise ValueError("Saved parameters do not match the selected wing width.")
    if exchange_time(quotes.execution_at).date() != selection.cycle.entry_session or exchange_time(
        quotes.execution_at
    ).time() not in {time(15, 20), time(15, 25)}:
        raise ValueError("Entries may execute only at the approved expiry-eve opening times.")
    if selection.vix > 28:
        return EntryDecision(False, "VIX_LIMIT", False, None)
    filtered = vix_state_filter(parameters.vix_state_filter_enabled, selection.vix, previous_vix)
    if not filtered.allowed:
        return EntryDecision(False, "VIX_FILTER_" + filtered.state, False, None)
    invalid = invalid_opening_contracts(selection, quotes)
    if invalid:
        return EntryDecision(False, "NO_VALID_ENTRY", True, None, invalid)
    fills = _opening_fills(selection, quotes, parameters)
    credit = sum(
        (leg.fill_price if leg.side == Side.SELL else -leg.fill_price for leg in fills),
        Decimal("0"),
    )
    loss_points = Decimal(selection.width) - credit
    budget = risk_budget(loss_points, selection.quantity, starting_capital)
    estimate = EntryEstimate(
        selection,
        quotes.execution_at,
        fills,
        credit,
        round_trip_charges(),
        loss_points,
        budget.maximum_loss,
        budget.assumed_margin,
        budget.limits.hard,
        budget.limits.margin,
        budget.target_exceeded,
        parameters.slippage_percent,
    )
    reason = "MINIMUM_CREDIT" if credit < 12 else budget.reason
    return EntryDecision(reason is None, reason, False, estimate)


def _close_valuation(entry: EntryEstimate, closing_values: tuple[Decimal, ...]) -> CloseValuation:
    if len(closing_values) != 4:
        raise ValueError("Four closing values are required.")
    debit = sum(
        (
            value if leg.side == Side.SELL else -value
            for leg, value in zip(entry.legs, closing_values, strict=True)
        ),
        Decimal("0"),
    )
    gain = entry.credit_points - debit
    per_leg = tuple(
        (value - leg.fill_price) * leg.quantity * (1 if leg.side == Side.BUY else -1)
        for leg, value in zip(entry.legs, closing_values, strict=True)
    )
    gross = inr(sum(per_leg, Decimal("0")))
    return CloseValuation(
        debit,
        gain,
        gross,
        entry.charges_inr,
        gross - entry.charges_inr,
        per_leg,
        gain >= entry.credit_points * decimal_rule("profit_target_fraction_of_credit"),
        gain <= -entry.credit_points * decimal_rule("stop_loss_fraction_of_credit"),
        closing_values,
    )


def value_liquidation(
    entry: EntryEstimate,
    reference_prices: tuple[Decimal, ...],
    slippage_percent: Decimal | None = None,
) -> CloseValuation:
    """Value synchronized closes or actual exit opens; lifecycle decides which."""
    if len(reference_prices) != 4:
        raise ValueError("Four synchronized closing references are required.")
    if slippage_percent is None:
        slippage_percent = entry.slippage_percent
    elif slippage_percent != entry.slippage_percent:
        raise ValueError("Exit slippage must match the immutable entry scenario.")
    closing = tuple(
        adverse_fill(
            reference,
            leg.contract.tick_size,
            Side.BUY if leg.side == Side.SELL else Side.SELL,
            slippage_percent,
        )
        for leg, reference in zip(entry.legs, reference_prices, strict=True)
    )
    return _close_valuation(entry, closing)


def value_settlement(
    entry: EntryEstimate, observation: SettlementObservation, *, as_of: datetime
) -> CloseValuation:
    if observation.expiry != entry.selection.cycle.expiry:
        raise ValueError("Settlement expiry does not match the position.")
    if observation.available_at > exchange_time(as_of):
        raise ObservationNotAvailable("Official settlement is not available yet.")
    values = tuple(
        max(
            Decimal("0"),
            observation.index_value - leg.contract.strike
            if leg.contract.option_type == "CE"
            else leg.contract.strike - observation.index_value,
        )
        for leg in entry.legs
    )
    return _close_valuation(entry, values)
