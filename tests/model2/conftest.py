from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.domain.model2.market import BAR_INTERVAL, EXCHANGE_ZONE
from app.infrastructure.datasets.derivatives_bundle import sha256_file
from app.infrastructure.datasets.model2_bundle import BundleComponent, build_model2_bundle

DAYS = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9, 12, 13)]
TIMES = [
    time(9, 15),
    time(9, 20),
    time(15, 10),
    time(15, 15),
    time(15, 20),
    time(15, 25),
    time(15, 30),
]


@dataclass
class SampleBundle:
    root: Path
    components: list[BundleComponent]
    calendar: dict[str, Any]
    contracts: list[dict[str, Any]]

    def component(self, kind: str, contract_id: str | None = None) -> BundleComponent:
        return next(item for item in self.components if item.key == (kind, contract_id))

    def refresh(self, component: BundleComponent) -> None:
        index = self.components.index(component)
        self.components[index] = component.model_copy(
            update={"checksum": sha256_file(self.root / component.path)}
        )

    def write_json(self, kind: str, payload: object) -> None:
        component = self.component(kind)
        (self.root / component.path).write_text(json.dumps(payload, default=str))
        self.refresh(component)

    def write_rows(self, component: BundleComponent, rows: list[dict[str, Any]]) -> None:
        path = self.root / component.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".csv":
            with path.open("w", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=list(rows[0])
                    if rows
                    else ["timestamp", "open", "high", "low", "close", "volume"],
                )
                writer.writeheader()
                writer.writerows(rows)
        else:
            table = (
                pa.Table.from_pylist(rows)
                if rows
                else pa.table({"timestamp": pa.array([], type=pa.string())})
            )
            pq.write_table(table, path)
        self.refresh(component)

    def build(self):
        return build_model2_bundle(self.root, self.components)


@pytest.fixture
def bundle_factory(tmp_path):
    counter = 0

    def create(*, semantics="interval_end", spot_format="csv", build=True, optional=False):
        nonlocal counter
        counter += 1
        root = tmp_path / f"bundle-{counter}"
        root.mkdir()
        specs = [
            ("call_short", "2026-01-06", 25000, "CE"),
            ("call_wing", "2026-01-06", 25200, "CE"),
            ("put_short", "2026-01-06", 24900, "PE"),
            ("put_wing", "2026-01-06", 24700, "PE"),
            ("next_call", "2026-01-13", 25000, "CE"),
            ("next_put", "2026-01-13", 24900, "PE"),
            ("unavailable_contract", "2026-01-06", 25100, "CE"),
        ]
        contracts = [
            {
                "contract_id": key,
                "symbol": f"FIXTURE {key}",
                "expiry": expiry,
                "strike": strike,
                "option_type": kind,
                "lot_size": 65,
                "tick_size_inr": "0.05",
            }
            for key, expiry, strike, kind in specs
        ]
        calendar = {
            "schema_version": "stockbot.model2-calendar.v1",
            "version": "fixture-calendar.v1",
            "coverage_start": "2026-01-02",
            "coverage_end": "2026-01-13",
            "expiry_reference": "Explicit synthetic expiry fixture; not an exchange assertion",
            "sessions": [
                {
                    "session_date": str(day),
                    "kind": "NORMAL",
                    "opens_at": "09:15",
                    "closes_at": "15:30",
                }
                for day in DAYS
            ],
            "weekly_expiries": ["2026-01-06", "2026-01-13"],
        }
        (root / "contracts.json").write_text(json.dumps(contracts))
        (root / "calendar.json").write_text(json.dumps(calendar))
        components = [
            BundleComponent(
                kind=kind,
                path=f"{kind}.json",
                checksum=sha256_file(root / f"{kind}.json"),
                source_reference="Synthetic test fixture",
            )
            for kind in ("contracts", "calendar")
        ]
        sample = SampleBundle(root, components, calendar, contracts)
        series = [
            ("spot", None, f"spot.{spot_format}", DAYS),
            ("india_vix", None, "india_vix.parquet", DAYS),
        ]
        series += [
            ("options", key, f"options/{key}.parquet", [day for day in DAYS if str(day) <= expiry])
            for key, expiry, _, _ in specs
            if key != "unavailable_contract"
        ]
        for kind, contract_id, path, days in series:
            rows = []
            for day in days:
                for opening in TIMES:
                    stamp = datetime.combine(day, opening, EXCHANGE_ZONE)
                    if semantics == "interval_end":
                        stamp += BAR_INTERVAL
                    value = "25000" if kind == "spot" else "20"
                    rows.append(
                        {
                            "timestamp": stamp.isoformat(),
                            "open": value,
                            "high": value,
                            "low": value,
                            "close": value,
                            "volume": 100,
                        }
                    )
            component = BundleComponent(
                kind=kind,
                contract_id=contract_id,
                path=path,
                checksum="0" * 64,
                timestamp_semantics=semantics,
                source_reference="Synthetic test fixture",
            )
            sample.components.append(component)
            sample.write_rows(component, rows)
        if optional:
            payloads = {
                "settlements": {
                    "schema_version": "stockbot.official-settlements.v1",
                    "source_kind": "OFFICIAL_SETTLEMENT",
                    "observations": [
                        {
                            "expiry": "2026-01-06",
                            "index_value": "25100",
                            "available_at": "2026-01-06T15:35:00+05:30",
                        }
                    ],
                },
                "halts": {
                    "schema_version": "stockbot.exchange-halts.v1",
                    "source_kind": "EXCHANGE_HALT",
                    "observations": [
                        {
                            "starts_at": "2026-01-06T10:00:00+05:30",
                            "ends_at": "2026-01-06T10:30:00+05:30",
                            "available_at": "2026-01-06T10:05:00+05:30",
                        }
                    ],
                },
            }
            for kind, payload in payloads.items():
                path = root / f"{kind}.json"
                path.write_text(json.dumps(payload))
                sample.components.append(
                    BundleComponent(
                        kind=kind,
                        path=path.name,
                        checksum=sha256_file(path),
                        source_reference="Synthetic official-source fixture, not real settlement/halt evidence",
                    )
                )
        if build:
            sample.build()
        return sample

    return create


@pytest.fixture
def selected_factory():
    from decimal import Decimal

    from app.domain.model2.calendar import WeeklyCycle
    from app.domain.model2.market import Model2Contract, Provenance
    from app.domain.model2.models import CondorSelection, LegRole, SelectedLeg

    def create(*, width=200, lot_size=65, vix=Decimal("16")):
        cycle = WeeklyCycle(date(2026, 1, 6), date(2026, 1, 5), "READY")
        source = Provenance("Synthetic strategy fixture", "a" * 64)
        specs = ((25250, "CE"), (25250 + width, "CE"), (24750, "PE"), (24750 - width, "PE"))
        legs = tuple(
            SelectedLeg(
                role,
                Model2Contract(
                    role.value,
                    role.value,
                    cycle.expiry,
                    strike,
                    kind,
                    lot_size,
                    Decimal("0.05"),
                    source,
                ),
            )
            for role, (strike, kind) in zip(LegRole, specs, strict=True)
        )
        return CondorSelection(cycle, Decimal("25000"), vix, width, legs)

    return create


@pytest.fixture
def opening_factory():
    from decimal import Decimal

    from app.domain.model2.market import MarketBar, Provenance
    from app.domain.model2.models import LegOpeningQuote, OpeningQuotes

    def create(selection, *, prices=("20", "14", "20", "14"), execution_at=None, volume=100):
        at = execution_at or datetime(2026, 1, 5, 15, 20, tzinfo=EXCHANGE_ZONE)
        previous = (
            datetime(2026, 1, 5, 15, 30, tzinfo=EXCHANGE_ZONE) if at.time() == time(9, 15) else at
        )
        source = Provenance("Synthetic opening fixture", "b" * 64)
        quotes = []
        for leg, raw in zip(selection.legs, prices, strict=True):
            value = Decimal(raw)
            preceding = MarketBar(
                previous - BAR_INTERVAL, value, value, value, value, volume, source
            ).completed(at)
            opening = MarketBar(at, value, value, value, value, 999, source).opening(at)
            quotes.append(LegOpeningQuote(leg.contract.contract_id, opening, preceding))
        return OpeningQuotes(at, previous, at, tuple(quotes))

    return create
