from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from app.domain.model2.calendar import Model2Calendar, Model2Session
from app.domain.model2.market import MarketBar, ObservationNotAvailable, Provenance
from app.infrastructure.datasets.model2_bundle import (
    BundleComponent,
    CalendarPayload,
    Model2BundleManifest,
    load_model2_bundle,
)
from app.infrastructure.market_data.model2_feed import Model2BundleFeed


def clock(day=5, at="15:20"):
    return datetime.fromisoformat(f"2026-01-{day:02d}T{at}:00+05:30")


def call(feed):
    return feed.contract(date(2026, 1, 6), 25000, "CE")


def test_open_and_completed_views_have_different_availability():
    bar = MarketBar(
        clock(),
        Decimal("20"),
        Decimal("50"),
        Decimal("10"),
        Decimal("40"),
        999,
        Provenance("fixture", "a" * 64),
    )
    opening = bar.opening(clock())
    assert opening.price == 20
    assert opening.available_at == clock()
    assert not hasattr(opening, "volume") and not hasattr(opening, "close")
    with pytest.raises(ObservationNotAvailable):
        bar.completed(clock())
    completed = bar.completed(clock(at="15:25"))
    assert completed.close == 40 and completed.volume == 999
    with pytest.raises(FrozenInstanceError):
        opening.price = Decimal("1")


@pytest.mark.parametrize("semantics", ["interval_start", "interval_end"])
@pytest.mark.parametrize("spot_format", ["csv", "parquet"])
def test_native_bundle_supports_both_timestamp_conventions_without_futures(
    bundle_factory, semantics, spot_format
):
    sample = bundle_factory(semantics=semantics, spot_format=spot_format)
    feed = Model2BundleFeed(sample.root)
    opening = feed.option_open(call(feed), clock(), as_of=clock())
    previous = feed.option_close(call(feed), clock(), as_of=clock())
    assert opening.price == 20 and opening.interval_start == clock()
    assert previous.interval_end == clock() and previous.interval_start == clock(at="15:15")
    assert previous.volume == 100
    assert feed.index_close("spot", clock(), as_of=clock()).close == 25000
    assert "futures" not in feed.manifest.capabilities
    assert feed.manifest.bundle_checksum == sample.build().bundle_checksum


def test_future_queries_fail_before_reading_data_and_naive_times_fail(bundle_factory):
    feed = Model2BundleFeed(bundle_factory().root)
    for query in (feed.option_open, feed.option_close):
        with pytest.raises(ObservationNotAvailable):
            query(call(feed), clock(at="15:25"), as_of=clock())
    assert feed.session_read_count == 0
    with pytest.raises(ValueError, match="timezone-aware"):
        feed.option_open(call(feed), clock().replace(tzinfo=None), as_of=clock())
    opening = feed.option_open(call(feed), clock(), as_of=clock().astimezone(UTC))
    assert opening.price == 20


def test_poisoned_future_close_and_volume_do_not_change_opening_price(bundle_factory):
    normal = bundle_factory()
    poisoned = bundle_factory(build=False)
    component = poisoned.component("options", "call_short")
    rows = pq.read_table(poisoned.root / component.path).to_pylist()
    for row in rows:
        if row["timestamp"] == clock(at="15:25").isoformat():
            row.update(close="90", high="100", volume=0)
    poisoned.write_rows(component, rows)
    poisoned.build()
    first, second = Model2BundleFeed(normal.root), Model2BundleFeed(poisoned.root)
    assert (
        first.option_open(call(first), clock(), as_of=clock()).price
        == second.option_open(call(second), clock(), as_of=clock()).price
    )
    assert second.option_close(call(second), clock(), as_of=clock()).volume == 100
    assert second.option_close(call(second), clock(at="15:25"), as_of=clock(at="15:25")).volume == 0


def test_pinned_cycle_keeps_exact_expiry_and_cannot_accept_a_different_contract(bundle_factory):
    feed = Model2BundleFeed(bundle_factory().root)
    cycle = feed.calendar.cycles(date(2026, 1, 5), date(2026, 1, 6))[0]
    pinned = feed.pin_cycle(cycle)
    contract = pinned.contract(25000, "CE")
    assert contract.expiry == date(2026, 1, 6)
    assert pinned.option_open(contract, clock(5), as_of=clock(5)).price == 20
    assert pinned.option_open(contract, clock(6, "09:15"), as_of=clock(6, "09:15")).price == 20
    with pytest.raises(ValueError, match="retain its expiry"):
        pinned.option_open(
            feed.contract(date(2026, 1, 13), 25000, "CE"),
            clock(6, "09:15"),
            as_of=clock(6, "09:15"),
        )
    with pytest.raises(ValueError, match="two-session"):
        pinned.option_open(contract, clock(7), as_of=clock(7))
    with pytest.raises(ValueError, match="metadata"):
        feed.option_open(replace(contract, lot_size=75), clock(), as_of=clock())


def test_missing_metadata_missing_bars_and_absent_option_file_stay_distinct(bundle_factory):
    feed = Model2BundleFeed(bundle_factory().root)
    assert feed.contract(date(2026, 1, 6), 99999, "PE") is None
    assert feed.option_open(call(feed), clock(at="09:25"), as_of=clock(at="09:25")) is None
    absent = feed.contract(date(2026, 1, 6), 25100, "CE")
    assert absent is not None
    assert feed.option_open(absent, clock(), as_of=clock()) is None
    # The 15:30-start extra source bar is preserved but not a normal-session fill.
    assert feed.option_open(call(feed), clock(at="15:30"), as_of=clock(at="15:30")) is None


def test_previous_bar_uses_exchange_calendar_across_overnight_and_weekend(bundle_factory):
    feed = Model2BundleFeed(bundle_factory().root)
    assert feed.calendar.previous_completed_at(clock(6, "09:15")) == clock(5, "15:30")
    assert feed.calendar.previous_completed_at(clock(5, "09:15")) == clock(2, "15:30")
    assert feed.calendar.previous_completed_at(clock()) == clock()
    previous = feed.option_close(call(feed), clock(5, "15:30"), as_of=clock(6, "09:15"))
    assert previous.interval_start == clock(5, "15:25")


def test_cache_is_bounded_and_repeated_queries_do_not_rescan_sessions(bundle_factory):
    feed = Model2BundleFeed(bundle_factory().root)
    for _ in range(5):
        feed.index_close("spot", clock(), as_of=clock())
    assert feed.session_read_count == 1
    for day in (2, 5, 6, 7, 8, 9, 12, 13):
        for name in ("spot", "india_vix"):
            feed.index_close(name, clock(day), as_of=clock(day))
    assert feed.cached_series_sessions == 12
    before = feed.session_read_count
    feed.index_close("spot", clock(2), as_of=clock(2))
    assert feed.session_read_count == before + 1


def test_settlement_is_optional_official_and_available_only_at_its_observed_time(bundle_factory):
    no_settlement = Model2BundleFeed(bundle_factory().root)
    assert no_settlement.settlement(date(2026, 1, 6), as_of=clock(6, "15:35")) is None
    assert "settlements" not in no_settlement.manifest.capabilities
    feed = Model2BundleFeed(bundle_factory(optional=True).root)
    assert feed.settlement(date(2026, 1, 6), as_of=clock(6, "15:30")) is None
    observed = feed.settlement(date(2026, 1, 6), as_of=clock(6, "15:35"))
    assert observed.index_value == 25100 and len(observed.provenance.checksum) == 64


def test_halt_availability_does_not_infer_halts_from_missing_prices_or_expose_future_end(
    bundle_factory,
):
    feed = Model2BundleFeed(bundle_factory(optional=True).root)
    assert feed.halts(as_of=clock(6, "10:00")) == ()
    observed = feed.halts(as_of=clock(6, "10:05"))
    assert len(observed) == 1 and observed[0].starts_at == clock(6, "10:00")
    assert not hasattr(observed[0], "ends_at")
    assert feed.halts(as_of=clock(6, "10:30")) == ()
    assert Model2BundleFeed(bundle_factory().root).halts(as_of=clock(6, "10:05")) == ()


def test_calendar_cycles_do_not_disappear_when_contracts_are_missing(bundle_factory):
    sample = bundle_factory(build=False)
    sample.write_json(
        "contracts", [item for item in sample.contracts if item["expiry"] != "2026-01-13"]
    )
    sample.components = [
        item for item in sample.components if item.contract_id not in {"next_call", "next_put"}
    ]
    sample.build()
    feed = Model2BundleFeed(sample.root)
    cycles = feed.calendar.cycles(date(2026, 1, 2), date(2026, 1, 13))
    assert len(cycles) == 2 and cycles[-1].expiry == date(2026, 1, 13)
    assert feed.pin_cycle(cycles[-1]).contract(25000, "CE") is None


def test_calendar_special_sessions_and_range_boundaries_are_explicit(bundle_factory):
    sample = bundle_factory(build=False)
    sample.calendar["sessions"][1].update(kind="SPECIAL", opens_at=None, closes_at=None)
    sample.write_json("calendar", sample.calendar)
    sample.build()
    feed = Model2BundleFeed(sample.root)
    cycle = feed.calendar.cycles(date(2026, 1, 5), date(2026, 1, 6))[0]
    assert cycle.classification == "SPECIAL_SESSION"
    with pytest.raises(ValueError, match="normal-session"):
        feed.pin_cycle(cycle)
    ordinary = Model2BundleFeed(bundle_factory().root)
    for start, end in ((date(2026, 1, 6), date(2026, 1, 6)), (date(2026, 1, 5), date(2026, 1, 5))):
        assert (
            ordinary.calendar.cycles(start, end)[0].classification == "OUTSIDE_COMPLETE_CYCLE_RANGE"
        )
    with pytest.raises(ValueError, match="does not cover"):
        ordinary.calendar.cycles(date(2024, 1, 1), date(2024, 1, 2))
    with pytest.raises(ValueError, match="reversed"):
        ordinary.calendar.cycles(date(2026, 1, 6), date(2026, 1, 5))


def test_calendar_holiday_expiry_is_read_from_schedule_not_a_weekday_rule(bundle_factory):
    sample = bundle_factory(build=False)
    sample.calendar["weekly_expiries"] = ["2026-01-12"]  # Synthetic Monday expiry.
    sample.write_json("calendar", sample.calendar)
    sample.build()
    feed = Model2BundleFeed(sample.root)
    cycle = feed.calendar.cycles(date(2026, 1, 9), date(2026, 1, 12))[0]
    assert cycle.entry_session == date(2026, 1, 9)
    assert cycle.expiry == date(2026, 1, 12)


def test_changed_files_are_rejected_even_if_prices_were_cached(bundle_factory):
    sample = bundle_factory()
    feed = Model2BundleFeed(sample.root)
    feed.option_open(call(feed), clock(), as_of=clock())
    path = sample.root / sample.component("options", "call_short").path
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed"):
        feed.option_open(call(feed), clock(), as_of=clock())
    with pytest.raises(ValueError, match="changed"):
        feed.verify_snapshot()
    with pytest.raises(ValueError, match="checksum"):
        Model2BundleFeed(sample.root)


def test_manifest_rejects_mutation_and_refuses_overwrite_of_another_snapshot(bundle_factory):
    sample = bundle_factory()
    path = sample.root / "bundle_manifest.json"
    original = path.read_bytes()
    payload = json.loads(original)
    payload["components"][0]["source_reference"] = "Changed"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="manifest checksum"):
        load_model2_bundle(sample.root)
    path.write_bytes(original)
    component = sample.component("options", "call_short")
    index = sample.components.index(component)
    sample.components[index] = component.model_copy(update={"source_reference": "Different source"})
    with pytest.raises(ValueError, match="different inputs"):
        sample.build()
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "path", ["../outside.csv", "/tmp/outside.csv", "nested/../../outside.csv", "..\\outside.csv"]
)
def test_relative_component_paths_cannot_escape(path):
    with pytest.raises(ValidationError):
        BundleComponent(
            kind="spot",
            path=path,
            checksum="a" * 64,
            timestamp_semantics="interval_end",
            source_reference="fixture",
        )


def test_symlink_component_cannot_escape_bundle_root(bundle_factory):
    sample = bundle_factory()
    component = sample.component("spot")
    path = sample.root / component.path
    outside = sample.root.parent / "outside.csv"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="escaped"):
        load_model2_bundle(sample.root)


@pytest.mark.parametrize(
    "problem",
    ["duplicate", "unordered", "no_volume", "negative", "naive", "utc", "after_expiry", "empty"],
)
def test_invalid_price_series_fail_before_a_bundle_is_published(bundle_factory, problem):
    sample = bundle_factory(build=False)
    component = sample.component("options", "call_short")
    rows = pq.read_table(sample.root / component.path).to_pylist()
    if problem == "duplicate":
        rows.insert(1, rows[0])
    elif problem == "unordered":
        rows[0], rows[1] = rows[1], rows[0]
    elif problem == "no_volume":
        for row in rows:
            row.pop("volume")
    elif problem == "negative":
        rows[0]["volume"] = -1
    elif problem == "naive":
        rows[0]["timestamp"] = "2026-01-02T09:20:00"
    elif problem == "utc":
        rows[0]["timestamp"] = "2026-01-02T03:50:00+00:00"
    elif problem == "after_expiry":
        rows.append(dict(rows[-1], timestamp="2026-01-07T09:20:00+05:30"))
    else:
        rows = []
    sample.write_rows(component, rows)
    with pytest.raises(ValueError):
        sample.build()
    assert not (sample.root / "bundle_manifest.json").exists()


@pytest.mark.parametrize("problem", ["ordinary_spot_proxy", "early", "duplicate", "naive"])
def test_invalid_settlement_provenance_and_availability_fail(bundle_factory, problem):
    sample = bundle_factory(build=False, optional=True)
    component = sample.component("settlements")
    payload = json.loads((sample.root / component.path).read_text())
    if problem == "ordinary_spot_proxy":
        payload["source_kind"] = "SPOT_CLOSE_PROXY"
    elif problem == "early":
        payload["observations"][0]["available_at"] = "2026-01-06T09:15:00+05:30"
    elif problem == "naive":
        payload["observations"][0]["available_at"] = "2026-01-06T15:35:00"
    else:
        payload["observations"].append(payload["observations"][0])
    sample.write_json("settlements", payload)
    with pytest.raises(ValueError):
        sample.build()


def test_invalid_calendar_cannot_guess_closed_days_or_unqualified_years():
    provenance = Provenance("fixture", "a" * 64)
    session = Model2Session(date(2026, 1, 5), "NORMAL", time(9, 15), time(15, 30))
    with pytest.raises(ValueError, match="previous"):
        Model2Calendar(
            "fixture",
            date(2026, 1, 5),
            date(2026, 1, 6),
            (session,),
            (session.session_date,),
            provenance,
            "fixture",
        )
    with pytest.raises(ValueError, match="SPECIAL"):
        Model2Session(date(2026, 1, 5), "NORMAL", time(9, 15), time(12, 30))


def test_previous_vix_reference_uses_last_valid_bar_not_before_1500(bundle_factory):
    sample = bundle_factory(build=False)
    component = sample.component("india_vix")
    rows = pq.read_table(sample.root / component.path).to_pylist()
    # Keep the previous session's 15:10-start bar, remove later normal bars.
    rows = [
        row
        for row in rows
        if not (
            row["timestamp"][:10] == "2026-01-02" and "15:15" < row["timestamp"][11:16] <= "15:30"
        )
    ]
    sample.write_rows(component, rows)
    sample.build()
    feed = Model2BundleFeed(sample.root)
    reference = feed.previous_vix_reference(date(2026, 1, 5), as_of=clock())
    assert reference.interval_start == clock(2, "15:10")
    assert reference.available_at == clock(2, "15:15")
    # Data outside the last normal interval must not become a closing reference.
    assert reference.close == 20


def test_missing_vix_reference_returns_unknown_without_forward_filling(bundle_factory):
    sample = bundle_factory(build=False)
    component = sample.component("india_vix")
    rows = pq.read_table(sample.root / component.path).to_pylist()
    rows = [
        row
        for row in rows
        if not (row["timestamp"][:10] == "2026-01-02" and row["timestamp"][11:16] >= "15:05")
    ]
    sample.write_rows(component, rows)
    sample.build()
    feed = Model2BundleFeed(sample.root)
    assert feed.previous_vix_reference(date(2026, 1, 5), as_of=clock()) is None


def test_bundle_builder_cli_hashes_and_validates_explicit_files(
    bundle_factory, monkeypatch, capsys
):
    from scripts.build_model2_bundle import main

    sample = bundle_factory(build=False)
    definition = sample.root / "components.json"
    definition.write_text(
        json.dumps(
            [item.model_dump(mode="json", exclude={"checksum"}) for item in sample.components]
        )
    )
    monkeypatch.setattr(
        "sys.argv",
        ["build-model2-bundle", "--bundle-dir", str(sample.root), "--components", str(definition)],
    )
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "stockbot.derivatives-bundle.v3"
    assert "futures" not in output["capabilities"]
    assert Model2BundleFeed(sample.root).manifest.bundle_checksum == output["bundle_checksum"]


def test_missing_timestamp_column_is_a_validation_error_before_publication(bundle_factory):
    sample = bundle_factory(build=False)
    component = sample.component("options", "call_short")
    rows = pq.read_table(sample.root / component.path).to_pylist()
    for row in rows:
        row.pop("timestamp")
    sample.write_rows(component, rows)
    with pytest.raises(ValueError, match="timestamps"):
        sample.build()
    assert not (sample.root / "bundle_manifest.json").exists()


def test_market_bundle_and_calendar_schemas_are_versioned():
    root = Path(__file__).resolve().parents[2] / "contracts/model2"
    for filename, model in (
        ("market-bundle-v3", Model2BundleManifest),
        ("calendar-v1", CalendarPayload),
    ):
        path = root / f"{filename}.schema.json"
        assert json.loads(path.read_text()) == model.model_json_schema()
        assert (
            path.with_suffix(".sha256").read_text().strip()
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )
