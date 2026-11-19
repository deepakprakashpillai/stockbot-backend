from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from app.domain.model2.approved_specification import (
    APPROVAL_SOURCE,
    APPROVED_RULES,
    approved_specification_digest,
    approved_specification_snapshot,
)
from app.domain.model2.market import ObservationNotAvailable, SettlementObservation
from app.domain.model2.models import LegRole, Side
from app.domain.model2.parameters import Model2Parameters
from app.domain.model2.pricing import (
    adverse_fill,
    collect_opening_quotes,
    evaluate_entry,
    value_liquidation,
    value_settlement,
)
from app.domain.model2.risk import margin_allowed, risk_budget
from app.domain.model2.selection import (
    round_short_strike,
    select_condor,
    short_strikes,
    vix_state_filter,
)
from app.domain.model2.specification import specification_digest, specification_snapshot
from app.infrastructure.market_data.model2_feed import Model2BundleFeed

D = Decimal
ROOT = Path(__file__).resolve().parents[2]


def vector(identifier):
    data = json.loads((ROOT / "tests/fixtures/model2/acceptance_cases_v1.json").read_text())
    return next(case for case in data["cases"] if case["id"] == identifier)


def book(selection):
    return SimpleNamespace(
        contract=lambda expiry, strike, kind: next(
            (
                leg.contract
                for leg in selection.legs
                if (leg.contract.expiry, leg.contract.strike, leg.contract.option_type)
                == (expiry, strike, kind)
            ),
            None,
        )
    )


@pytest.mark.parametrize(
    "raw,kind,expected",
    [
        ("25025", "CE", 25050),
        ("24975", "PE", 24950),
        ("25024.999", "CE", 25000),
        ("24975.001", "PE", 25000),
    ],
)
def test_independent_strike_rounding_boundaries(raw, kind, expected):
    assert round_short_strike(D(raw), kind) == expected


def test_vix_distance_uses_declared_formula_with_stable_precision():
    with localcontext() as context:
        context.prec = 10
        assert short_strikes(D("25000"), D("16")) == (25250, 24750)
    assert short_strikes(D("25000"), D("20")) == (25300, 24700)


@pytest.mark.parametrize("width", [150, 200])
def test_selection_retains_all_four_metadata_roles_and_exact_width(selected_factory, width):
    selected = selected_factory(width=width, lot_size=75)
    decision = select_condor(
        selected.cycle,
        selected.spot,
        selected.vix,
        Model2Parameters(wing_width_points=width),
        book(selected),
    )
    assert decision.reason is None and decision.selection == selected
    assert tuple(leg.role for leg in decision.selection.legs) == tuple(LegRole)
    assert decision.selection.quantity == 75


def test_missing_contracts_are_reported_without_trying_nearby_strikes(selected_factory):
    selected = selected_factory()
    requested = []

    def lookup(expiry, strike, kind):
        requested.append((strike, kind))
        return None if strike == 25450 else book(selected).contract(expiry, strike, kind)

    result = select_condor(
        selected.cycle,
        selected.spot,
        selected.vix,
        Model2Parameters(),
        SimpleNamespace(contract=lookup),
    )
    assert result.reason == "MISSING_CONTRACT" and result.selection is None
    assert result.missing[0].role == LegRole.LONG_CALL
    assert requested == [(25250, "CE"), (25450, "CE"), (24750, "PE"), (24550, "PE")]


def test_vix_and_calendar_gates_are_independent_of_contract_availability(selected_factory):
    selected = selected_factory(vix=D("28"))
    none = SimpleNamespace(contract=lambda *_: None)
    assert (
        select_condor(selected.cycle, selected.spot, D("28"), Model2Parameters(), none).reason
        == "MISSING_CONTRACT"
    )
    assert (
        select_condor(selected.cycle, selected.spot, D("28.01"), Model2Parameters(), none).reason
        == "VIX_LIMIT"
    )
    assert (
        select_condor(
            replace(selected.cycle, classification="SPECIAL_SESSION"),
            selected.spot,
            selected.vix,
            Model2Parameters(),
            none,
        ).reason
        == "SPECIAL_SESSION"
    )


def test_wrong_contract_identity_lot_sizes_and_geometry_fail(selected_factory):
    selected = selected_factory()
    with pytest.raises(ValueError, match="substituted"):
        select_condor(
            selected.cycle,
            selected.spot,
            selected.vix,
            Model2Parameters(),
            SimpleNamespace(contract=lambda *_: selected.legs[0].contract),
        )
    legs = list(selected.legs)
    legs[1] = replace(legs[1], contract=replace(legs[1].contract, lot_size=75))
    with pytest.raises(ValueError, match="lot size"):
        replace(selected, legs=tuple(legs))
    with pytest.raises(ValueError, match="roles"):
        replace(selected, legs=selected.legs[:3])


@pytest.mark.parametrize(
    "enabled,current,previous,allowed,state",
    [
        (False, "16", None, True, "OFF"),
        (True, "16", "16", True, "ROSE_OR_EQUAL"),
        (True, "16", "15", True, "ROSE_OR_EQUAL"),
        (True, "15", "16", False, "FELL"),
        (True, "16", None, False, "UNKNOWN"),
    ],
)
def test_experimental_filter_is_explicit_and_equality_is_a_rise(
    enabled, current, previous, allowed, state
):
    decision = vix_state_filter(enabled, D(current), D(previous) if previous else None)
    assert (decision.allowed, decision.state, decision.experimental) == (allowed, state, enabled)


def test_adverse_fill_vector_covers_both_sides_and_tick_rounding(selected_factory, opening_factory):
    case = vector("adverse_slippage")
    selected = selected_factory(width=150)
    decision = evaluate_entry(
        selected,
        opening_factory(selected, prices=("20", "8", "20", "8")),
        Model2Parameters(wing_width_points=150),
        D("500000"),
    )
    assert decision.accepted
    entry = decision.estimate
    assert [str(leg.fill_price) for leg in entry.legs] == ["19.60", "8.20", "19.60", "8.20"]
    assert entry.credit_points == D(case["expected"]["net_credit"])
    closed = value_liquidation(entry, (D("20"), D("8"), D("20"), D("8")), D("2"))
    assert closed.exit_debit_points == D("25.20")
    assert closed.gross_pnl_inr == D("-156.00") and closed.net_pnl_inr == D("-556.00")


@pytest.mark.parametrize(
    "side,reference,expected",
    [(Side.BUY, "1.01", "1.05"), (Side.SELL, "1.01", "1.00"), (Side.SELL, "0.01", "0.00")],
)
def test_tick_rounding_is_always_adverse(side, reference, expected):
    assert adverse_fill(D(reference), D("0.05"), side, D("0")) == D(expected)


@pytest.mark.parametrize("quantity,expected", [(65, "10800"), (75, "12400")])
def test_historical_quantity_drives_maximum_loss(quantity, expected):
    assert risk_budget(D("160"), quantity, D("500000")).maximum_loss == D(expected)


def test_credit_floor_does_not_override_the_binding_hard_cap(selected_factory, opening_factory):
    case = vector("cap_rejection")
    selected = selected_factory()
    decision = evaluate_entry(
        selected, opening_factory(selected), Model2Parameters(slippage_percent=0), D("500000")
    )
    assert not decision.accepted and not decision.retryable and decision.reason == "RISK_CAP"
    assert decision.estimate.credit_points == 12
    assert decision.estimate.maximum_loss_inr == D(case["expected"]["maximum_loss"])
    equal = evaluate_entry(
        selected, opening_factory(selected), Model2Parameters(slippage_percent=0), D("504800")
    )
    assert equal.accepted and equal.estimate.maximum_loss_inr == equal.estimate.hard_limit_inr


def test_one_lot_exception_and_narrow_wings_still_check_actual_quantity(
    selected_factory, opening_factory
):
    selected = selected_factory(width=150)
    accepted = evaluate_entry(
        selected,
        opening_factory(selected),
        Model2Parameters(wing_width_points=150, slippage_percent=0),
        D("500000"),
    )
    assert accepted.accepted and accepted.estimate.target_risk_exceeded
    assert accepted.estimate.maximum_loss_inr == D("9370")
    larger = selected_factory(width=150, lot_size=100)
    assert (
        evaluate_entry(
            larger,
            opening_factory(larger),
            Model2Parameters(wing_width_points=150, slippage_percent=0),
            D("500000"),
        ).reason
        == "RISK_CAP"
    )


@pytest.mark.parametrize("capital,allowed", [("37500", True), ("37499.99", False)])
def test_margin_boundary_includes_charges_and_never_clips(capital, allowed):
    assert margin_allowed(D("10000"), D(capital)) is allowed


def test_minimum_credit_and_nonpositive_maximum_loss_have_no_retry(
    selected_factory, opening_factory
):
    selected = selected_factory(width=150)
    parameters = Model2Parameters(wing_width_points=150, slippage_percent=0)
    below = evaluate_entry(
        selected,
        opening_factory(selected, prices=("20", "14.05", "20", "14")),
        parameters,
        D("500000"),
    )
    assert below.reason == "MINIMUM_CREDIT" and not below.retryable
    invalid = evaluate_entry(
        selected,
        opening_factory(selected, prices=("100", "25", "100", "25")),
        parameters,
        D("500000"),
    )
    assert (
        invalid.reason == "NONPOSITIVE_MAX_LOSS_POINTS"
        and invalid.estimate.maximum_loss_inr is None
    )


@pytest.mark.parametrize(
    "problem", ["missing_open", "zero_open", "zero_volume", "missing_previous", "stale_open"]
)
def test_only_invalid_price_data_allows_entry_retry(selected_factory, opening_factory, problem):
    selected = selected_factory(width=150)
    quotes = opening_factory(selected)
    first = quotes.legs[0]
    if problem == "missing_open":
        first = replace(first, opening=None)
    elif problem == "zero_open":
        first = replace(first, opening=replace(first.opening, price=D("0")))
    elif problem == "zero_volume":
        first = replace(first, preceding=replace(first.preceding, volume=0))
    elif problem == "missing_previous":
        first = replace(first, preceding=None)
    else:
        first = replace(
            first,
            opening=replace(
                first.opening, interval_start=quotes.execution_at - timedelta(minutes=5)
            ),
        )
    decision = evaluate_entry(
        selected,
        replace(quotes, legs=(first, *quotes.legs[1:])),
        Model2Parameters(wing_width_points=150, slippage_percent=0),
        D("500000"),
    )
    assert decision.reason == "NO_VALID_ENTRY" and decision.retryable
    assert decision.invalid_contract_ids == (selected.legs[0].contract.contract_id,)


def test_future_volume_and_misidentified_quotes_cannot_authorize_a_fill(
    selected_factory, opening_factory
):
    selected = selected_factory(width=150)
    quotes = opening_factory(selected)
    first = quotes.legs[0]
    future = replace(
        first,
        preceding=replace(first.preceding, interval_end=quotes.execution_at + timedelta(minutes=5)),
    )
    with pytest.raises(ObservationNotAvailable):
        evaluate_entry(
            selected,
            replace(quotes, legs=(future, *quotes.legs[1:])),
            Model2Parameters(wing_width_points=150),
            D("500000"),
        )
    with pytest.raises(ValueError, match="identities"):
        evaluate_entry(
            selected,
            replace(quotes, legs=tuple(reversed(quotes.legs))),
            Model2Parameters(wing_width_points=150),
            D("500000"),
        )


def test_target_and_stop_use_credit_points_before_charges(selected_factory, opening_factory):
    selected = selected_factory(width=150)
    entry = evaluate_entry(
        selected,
        opening_factory(selected),
        Model2Parameters(wing_width_points=150, slippage_percent=0),
        D("500000"),
    ).estimate
    target = value_liquidation(entry, (D("5"), D("2"), D("5"), D("2")), D("0"))
    assert target.gain_points == 6 and target.target_reached and not target.stop_reached
    assert target.net_pnl_inr == D("-10.00")  # Gross target can still lose after costs.
    stop = value_liquidation(entry, (D("14"), D("2"), D("14"), D("2")), D("0"))
    assert stop.gain_points == -12 and stop.stop_reached and not stop.target_reached
    bad_fill = value_liquidation(entry, (D("120"), D("1"), D("120"), D("1")), D("0"))
    assert bad_fill.net_pnl_inr < -entry.hard_limit_inr  # Never clamp realized estimates.


def test_official_intrinsic_settlement_has_no_slippage_and_charges_once(
    selected_factory, opening_factory
):
    selected = selected_factory(width=150)
    entry = evaluate_entry(
        selected,
        opening_factory(selected, prices=("20", "10", "20", "10")),
        Model2Parameters(wing_width_points=150, slippage_percent=0),
        D("500000"),
    ).estimate
    stamp = datetime.fromisoformat("2026-01-06T15:35:00+05:30")
    official = SettlementObservation(
        selected.cycle.expiry, D("25000"), stamp, selected.legs[0].contract.provenance
    )
    value = value_settlement(entry, official, as_of=stamp)
    assert value.exit_debit_points == 0 and value.gross_pnl_inr == 1300 and value.net_pnl_inr == 900
    assert sum(value.leg_gross_pnl_inr) == value.gross_pnl_inr
    with pytest.raises(ObservationNotAvailable):
        value_settlement(entry, official, as_of=stamp - timedelta(minutes=5))


def test_collector_and_strategy_integrate_with_the_36b_feed(bundle_factory):
    sample = bundle_factory(build=False)
    strikes = {"call_short": 25300, "call_wing": 25500, "put_short": 24700, "put_wing": 24500}
    for row in sample.contracts:
        if row["contract_id"] in strikes:
            row["strike"] = strikes[row["contract_id"]]
    sample.write_json("contracts", sample.contracts)
    for identifier in strikes:
        component = sample.component("options", identifier)
        rows = pq.read_table(sample.root / component.path).to_pylist()
        for row in rows:
            value = "40" if identifier.endswith("short") else "10"
            row.update(open=value, high=value, low=value, close=value)
        sample.write_rows(component, rows)
    sample.build()
    feed = Model2BundleFeed(sample.root)
    cycle = feed.calendar.cycles(datetime(2026, 1, 5).date(), datetime(2026, 1, 6).date())[0]
    at = datetime.fromisoformat("2026-01-05T15:20:00+05:30")
    parameters = Model2Parameters(vix_state_filter_enabled=True)
    selected = select_condor(
        cycle,
        feed.index_close("spot", at, as_of=at).close,
        feed.index_close("india_vix", at, as_of=at).close,
        parameters,
        feed,
    ).selection
    quotes = collect_opening_quotes(feed, feed.calendar, selected, at)
    result = evaluate_entry(
        selected,
        quotes,
        parameters,
        D("500000"),
        previous_vix=feed.previous_vix_reference(cycle.entry_session, as_of=at).close,
    )
    assert result.accepted and result.estimate.credit_points == 58
    assert result.estimate.maximum_loss_inr == 9630


def test_approval_revision_is_new_and_old_snapshot_is_unchanged():
    assert specification_snapshot()["rules"]["reliability_denominator"] is None
    assert (
        specification_digest() == "1b6f466bf000757eebc2ce67fc0554f917b58887701aab42a224fc9068fcb30c"
    )
    path = ROOT / "contracts/model2" / APPROVAL_SOURCE.path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == APPROVAL_SOURCE.sha256
    assert APPROVED_RULES["reliability_denominator"] == "OPENED_CYCLES_INCLUDING_EXCLUDED"
    current = approved_specification_snapshot()
    frozen = json.loads((ROOT / "contracts/model2/specification-v1.4-r1.json").read_text())
    assert frozen["specification"] == current
    assert frozen["specification_sha256"] == approved_specification_digest()


def test_strategy_modules_do_not_import_frameworks_storage_or_provider_sdks():
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "celery",
        "redis",
        "pyarrow",
        "app.infrastructure",
        "app.api",
        "app.db",
    )
    for path in (ROOT / "app/domain/model2").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(name.startswith(forbidden) for name in names), path


def test_exit_cannot_switch_slippage_scenario_mid_position(selected_factory, opening_factory):
    selected = selected_factory(width=150)
    entry = evaluate_entry(
        selected,
        opening_factory(selected),
        Model2Parameters(wing_width_points=150, slippage_percent=0),
        D("500000"),
    ).estimate
    references = (D("5"), D("2"), D("5"), D("2"))
    assert value_liquidation(entry, references) == value_liquidation(entry, references, D("0"))
    with pytest.raises(ValueError, match="immutable entry scenario"):
        value_liquidation(entry, references, D("2"))


def test_approved_case_pack_and_source_map_are_consistent():
    snapshot = approved_specification_snapshot()
    cases = json.loads((ROOT / "tests/fixtures/model2/acceptance_cases_r1.json").read_text())
    assert cases["specification_sha256"] == approved_specification_digest()
    expected = {
        identifier
        for rule in snapshot["rule_references"]
        for identifier in rule["acceptance_cases"]
    }
    assert expected == {case["id"] for case in cases["cases"]}
    assert "reliability_pending" not in expected
    contract = ROOT / "contracts/model2/specification-v1.4-r1.json"
    assert (
        contract.with_suffix(".sha256").read_text().strip()
        == hashlib.sha256(contract.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "vix,previous,reason",
    [
        ("28.01", None, "VIX_LIMIT"),
        ("16", None, "VIX_FILTER_UNKNOWN"),
        ("16", "17", "VIX_FILTER_FELL"),
    ],
)
def test_policy_rejections_do_not_retry_bad_entry_data(
    selected_factory, opening_factory, vix, previous, reason
):
    selected = selected_factory(width=150, vix=D(vix))
    decision = evaluate_entry(
        selected,
        opening_factory(selected),
        Model2Parameters(wing_width_points=150, vix_state_filter_enabled=True),
        D("500000"),
        previous_vix=D(previous) if previous else None,
    )
    assert decision.reason == reason and not decision.retryable


@pytest.mark.parametrize(
    "at", ["2026-01-05T15:15:00+05:30", "2026-01-05T15:30:00+05:30", "2026-01-06T15:20:00+05:30"]
)
def test_entry_window_cannot_be_bypassed_with_valid_prices(selected_factory, opening_factory, at):
    selected = selected_factory(width=150)
    quotes = opening_factory(selected, execution_at=datetime.fromisoformat(at))
    with pytest.raises(ValueError, match="expiry-eve"):
        evaluate_entry(selected, quotes, Model2Parameters(wing_width_points=150), D("500000"))


@pytest.mark.parametrize(
    "reference,tick,slip",
    [
        (D("0"), D("0.05"), D("0")),
        (D("1"), D("0"), D("0")),
        (D("1"), D("0.05"), D("0.5")),
        (1.0, D("0.05"), D("0")),
    ],
)
def test_invalid_fill_values_are_not_implicitly_coerced(reference, tick, slip):
    with pytest.raises(ValueError):
        adverse_fill(reference, tick, Side.BUY, slip)
