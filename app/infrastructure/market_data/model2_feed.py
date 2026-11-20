"""Read Model 2 v3 snapshots with bounded session caching and field availability."""

from __future__ import annotations

import csv
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import Any

from app.domain.model2.calendar import WeeklyCycle
from app.domain.model2.market import (
    BAR_INTERVAL,
    CompletedObservation,
    HaltObservation,
    IndexName,
    MarketBar,
    Model2Contract,
    ObservationNotAvailable,
    OpenObservation,
    OptionType,
    SettlementObservation,
    exchange_time,
)
from app.infrastructure.datasets.model2_bundle import (
    MODEL2_MANIFEST_FILENAME,
    BundleComponent,
    Model2BundleManifest,
    component_path,
    decode_bar,
    file_state,
    price_rows,
    read_calendar,
    read_contracts,
    read_halts,
    read_settlements,
    verify_model2_files,
)


class Model2BundleFeed:
    MAX_CACHED_SERIES_SESSIONS = 12  # Spot/VIX/four legs over two sessions.

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self.manifest = Model2BundleManifest.model_validate_json(
            (root / MODEL2_MANIFEST_FILENAME).read_text()
        )
        self._verified_states = verify_model2_files(self._root, self.manifest)
        self._components = {item.key: item for item in self.manifest.components}
        calendar = self.manifest.component("calendar")
        contracts = self.manifest.component("contracts")
        assert calendar is not None and contracts is not None
        self.calendar = read_calendar(self._root, calendar)
        values = read_contracts(self._root, contracts)
        self._contracts = {item.contract_id: item for item in values}
        self._contracts_by_key = {
            (item.expiry, item.strike, item.option_type): item for item in values
        }
        for component in self.manifest.components:
            if component.kind == "options" and component.contract_id not in self._contracts:
                raise ValueError("Option component does not identify a known contract.")
        option_types = {
            self._contracts[item.contract_id].option_type
            for item in self.manifest.components
            if item.kind == "options" and item.contract_id is not None
        }
        if option_types != {"CE", "PE"}:
            raise ValueError("Model 2 requires both call and put series.")
        settlements = self.manifest.component("settlements")
        self._settlements = (
            {item.expiry: item for item in read_settlements(self._root, settlements)}
            if settlements
            else {}
        )
        halts = self.manifest.component("halts")
        self._halts = read_halts(self._root, halts) if halts else ()
        self._cache: OrderedDict[
            tuple[tuple[str, str | None], date], MappingProxyType[datetime, MarketBar]
        ] = OrderedDict()
        self._csv_offsets: dict[Path, dict[date, int]] = {}
        self._session_read_count = 0

    @property
    def cached_series_sessions(self) -> int:
        return len(self._cache)

    @property
    def session_read_count(self) -> int:
        return self._session_read_count

    def verify_snapshot(self) -> None:
        for path, state in self._verified_states.items():
            if not path.is_file() or file_state(path) != state:
                raise ValueError("Market files changed after the verified snapshot was loaded.")

    def contract(self, expiry: date, strike: int, option_type: OptionType) -> Model2Contract | None:
        return self._contracts_by_key.get((expiry, strike, option_type))

    def pin_cycle(self, cycle: WeeklyCycle) -> PinnedCycleFeed:
        if cycle.expiry not in self.calendar.weekly_expiries:
            raise ValueError("Cycle expiry is absent from the explicit expiry schedule.")
        expected = self.calendar.previous_session(cycle.expiry)
        expiry_session = self.calendar.session(cycle.expiry)
        if expected.session_date != cycle.entry_session or expiry_session is None:
            raise ValueError("Cycle does not match the snapshotted trading calendar.")
        if (
            cycle.classification != "READY"
            or expected.kind != "NORMAL"
            or expiry_session.kind != "NORMAL"
        ):
            raise ValueError("Only a complete, normal-session cycle can be pinned.")
        return PinnedCycleFeed(self, cycle)

    def _path(self, component: BundleComponent) -> Path:
        path = component_path(self._root, component)
        if self._verified_states.get(path) != file_state(path):
            raise ValueError("Market file changed after snapshot verification.")
        return path

    def _csv_index(self, path: Path, component: BundleComponent) -> dict[date, int]:
        if path in self._csv_offsets:
            return self._csv_offsets[path]
        offsets: dict[date, int] = {}
        previous = None
        with path.open("rb") as source:
            header = next(csv.reader([source.readline().decode("utf-8-sig")]))
            while True:
                offset = source.tell()
                line = source.readline()
                if not line:
                    break
                values = next(csv.reader([line.decode("utf-8")]))
                if len(values) != len(header):
                    raise ValueError("Invalid physical market CSV row.")
                bar = decode_bar(dict(zip(header, values, strict=True)), component)
                if previous is not None and bar.interval_start <= previous:
                    raise ValueError("Duplicate or unordered market CSV timestamps.")
                offsets.setdefault(bar.interval_start.date(), offset)
                previous = bar.interval_start
        self._csv_offsets[path] = offsets
        return offsets

    def _bar(self, component: BundleComponent, start: datetime) -> MarketBar | None:
        path = self._path(component)
        session = self.calendar.session(start.date())
        if session is None or session.kind != "NORMAL" or start not in session.opening_times():
            return None
        key = component.key, start.date()
        if key not in self._cache:
            rows: Iterator[dict[str, Any]]
            offset = 0
            if path.suffix == ".csv":
                found = self._csv_index(path, component).get(start.date())
                if found is None:
                    rows = iter(())
                else:
                    offset = found
                    rows = price_rows(path, session_date=start.date(), csv_offset=offset)
            else:
                rows = price_rows(path, session_date=start.date())
            bars = {}
            previous = None
            scheduled = set(session.opening_times())
            for row in rows:
                bar = decode_bar(row, component)
                if previous is not None and bar.interval_start <= previous:
                    raise ValueError("Duplicate or unordered session timestamps.")
                previous = bar.interval_start
                if bar.interval_start in scheduled:
                    bars[bar.interval_start] = bar
            self._cache[key] = MappingProxyType(bars)
            self._session_read_count += 1
            while len(self._cache) > self.MAX_CACHED_SERIES_SESSIONS:
                self._cache.popitem(last=False)
        self._cache.move_to_end(key)
        return self._cache[key].get(start)

    @staticmethod
    def _clock(when: datetime, as_of: datetime) -> tuple[datetime, datetime]:
        when, as_of = exchange_time(when), exchange_time(as_of)
        if when > as_of:
            raise ObservationNotAvailable("Requested market fields are in the future.")
        return when, as_of

    def index_close(
        self, name: IndexName, end: datetime, *, as_of: datetime
    ) -> CompletedObservation | None:
        end, as_of = self._clock(end, as_of)
        component = self._components.get((name, None))
        if component is None:
            raise ValueError("Unknown index series.")
        bar = self._bar(component, end - BAR_INTERVAL)
        return bar.completed(as_of) if bar else None

    def previous_vix_reference(
        self, entry_session: date, *, as_of: datetime
    ) -> CompletedObservation | None:
        """Approved previous-session close, falling back no earlier than 15:00 start."""
        clock = exchange_time(as_of)
        previous = self.calendar.previous_session(entry_session)
        if previous.kind != "NORMAL" or previous.closes_at is None:
            return None
        end = datetime.combine(previous.session_date, previous.closes_at, clock.tzinfo)
        first_start = datetime.combine(previous.session_date, time(15, 0), clock.tzinfo)
        while end - BAR_INTERVAL >= first_start:
            if end <= clock:
                observation = self.index_close("india_vix", end, as_of=clock)
                if observation is not None and observation.close > 0:
                    return observation
            end -= BAR_INTERVAL
        return None

    def _option_component(self, contract: Model2Contract) -> BundleComponent | None:
        if self._contracts.get(contract.contract_id) != contract:
            raise ValueError("Contract does not match the snapshotted metadata.")
        return self._components.get(("options", contract.contract_id))

    def option_open(
        self, contract: Model2Contract, start: datetime, *, as_of: datetime
    ) -> OpenObservation | None:
        start, as_of = self._clock(start, as_of)
        component = self._option_component(contract)
        if component is None or start.date() > contract.expiry:
            return None
        bar = self._bar(component, start)
        return bar.opening(as_of) if bar else None

    def option_close(
        self, contract: Model2Contract, end: datetime, *, as_of: datetime
    ) -> CompletedObservation | None:
        end, as_of = self._clock(end, as_of)
        component = self._option_component(contract)
        if component is None or end.date() > contract.expiry:
            return None
        bar = self._bar(component, end - BAR_INTERVAL)
        return bar.completed(as_of) if bar else None

    def settlement(self, expiry: date, *, as_of: datetime) -> SettlementObservation | None:
        observed = self._settlements.get(expiry)
        return observed if observed and observed.available_at <= exchange_time(as_of) else None

    def halts(self, *, as_of: datetime) -> tuple[HaltObservation, ...]:
        clock = exchange_time(as_of)
        component = self.manifest.component("halts")
        if component is None:
            return ()
        return tuple(
            HaltObservation(item.starts_at, item.available_at, component.provenance)
            for item in self._halts
            if item.available_at <= clock and item.starts_at <= clock < item.ends_at
        )


@dataclass(frozen=True)
class PinnedCycleFeed:
    _feed: Model2BundleFeed
    cycle: WeeklyCycle

    def contract(self, strike: int, option_type: OptionType) -> Model2Contract | None:
        return self._feed.contract(self.cycle.expiry, strike, option_type)

    def _check(self, contract: Model2Contract, stamp: datetime) -> None:
        if contract.expiry != self.cycle.expiry or exchange_time(stamp).date() not in {
            self.cycle.entry_session,
            self.cycle.expiry,
        }:
            raise ValueError("An overnight position must retain its expiry and two-session window.")

    def option_open(
        self, contract: Model2Contract, start: datetime, *, as_of: datetime
    ) -> OpenObservation | None:
        self._check(contract, start)
        return self._feed.option_open(contract, start, as_of=as_of)

    def option_close(
        self, contract: Model2Contract, end: datetime, *, as_of: datetime
    ) -> CompletedObservation | None:
        self._check(contract, end)
        return self._feed.option_close(contract, end, as_of=as_of)
