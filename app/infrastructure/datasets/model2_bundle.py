"""Capability-based immutable v3 bundles for Model 2; futures are not required."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterator, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.model2.calendar import Model2Calendar, Model2Session
from app.domain.model2.market import (
    BAR_INTERVAL,
    EXCHANGE_ZONE,
    MarketBar,
    Model2Contract,
    Provenance,
    SettlementObservation,
)
from app.infrastructure.datasets.derivatives_bundle import sha256_file

MODEL2_BUNDLE_SCHEMA: Literal["stockbot.derivatives-bundle.v3"] = "stockbot.derivatives-bundle.v3"
MODEL2_MANIFEST_FILENAME = "bundle_manifest.json"
ComponentKind = Literal[
    "spot", "india_vix", "options", "contracts", "calendar", "settlements", "halts", "futures"
]
TimestampSemantics = Literal["interval_start", "interval_end"]
PRICE_KINDS = {"spot", "india_vix", "options", "futures"}


class BundleComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ComponentKind
    path: str = Field(min_length=1)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_reference: str = Field(min_length=1)
    timestamp_semantics: TimestampSemantics | None = None
    contract_id: str | None = None

    @model_validator(mode="after")
    def validate_component(self) -> BundleComponent:
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.path:
            raise ValueError("Bundle paths must remain relative to the bundle directory.")
        if not self.source_reference.strip():
            raise ValueError("A component source reference is required.")
        if self.kind in PRICE_KINDS:
            if self.timestamp_semantics is None or path.suffix not in {".csv", ".parquet"}:
                raise ValueError(
                    "Price files require explicit timestamp semantics and CSV/Parquet format."
                )
        elif self.timestamp_semantics is not None or path.suffix != ".json":
            raise ValueError("Metadata components must be JSON without candle semantics.")
        if (self.kind == "options") != bool(self.contract_id):
            raise ValueError(
                "Every option series, and only an option series, must pin a contract ID."
            )
        return self

    @property
    def key(self) -> tuple[str, str | None]:
        return self.kind, self.contract_id

    @property
    def provenance(self) -> Provenance:
        return Provenance(self.source_reference, self.checksum)


class Model2BundleManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["stockbot.derivatives-bundle.v3"] = MODEL2_BUNDLE_SCHEMA
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    interval_minutes: Literal[5] = 5
    components: tuple[BundleComponent, ...]
    bundle_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> Model2BundleManifest:
        kinds = [item.kind for item in self.components]
        if not {"spot", "india_vix", "contracts", "calendar", "options"} <= set(kinds):
            raise ValueError("Model 2 requires spot, VIX, contracts, calendar and option data.")
        if len({item.key for item in self.components}) != len(self.components):
            raise ValueError("Duplicate bundle component or option contract series.")
        if len({item.path for item in self.components}) != len(self.components):
            raise ValueError("Bundle components must reference distinct paths.")
        payload = self.model_dump(mode="json", exclude={"bundle_checksum"})
        if canonical_checksum(payload) != self.bundle_checksum:
            raise ValueError("Model 2 manifest checksum does not match its contents.")
        return self

    def component(self, kind: ComponentKind) -> BundleComponent | None:
        return next((item for item in self.components if item.kind == kind), None)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(item.kind for item in self.components)


def canonical_checksum(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def component_path(root: Path, component: BundleComponent) -> Path:
    root = root.resolve()
    path = (root / component.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Missing or escaped bundle component: {component.path}.")
    return path


def load_model2_bundle(root: Path) -> Model2BundleManifest:
    manifest = Model2BundleManifest.model_validate_json(
        (root / MODEL2_MANIFEST_FILENAME).read_text()
    )
    verify_model2_files(root, manifest)
    return manifest


def file_state(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def verify_model2_files(
    root: Path, manifest: Model2BundleManifest
) -> dict[Path, tuple[int, int, int, int]]:
    states = {}
    for component in manifest.components:
        path = component_path(root, component)
        if path in states:
            raise ValueError("Bundle components alias the same physical path.")
        before = file_state(path)
        if sha256_file(path) != component.checksum or before != file_state(path):
            raise ValueError(f"Model 2 component checksum mismatch: {component.path}.")
        states[path] = before
    return states


class SessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_date: date
    kind: Literal["NORMAL", "SPECIAL"]
    opens_at: time | None = None
    closes_at: time | None = None


class CalendarPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["stockbot.model2-calendar.v1"]
    version: str = Field(min_length=1)
    coverage_start: date
    coverage_end: date
    expiry_reference: str = Field(min_length=1)
    sessions: tuple[SessionPayload, ...]
    weekly_expiries: tuple[date, ...]


def read_calendar(root: Path, component: BundleComponent) -> Model2Calendar:
    data = CalendarPayload.model_validate_json(component_path(root, component).read_text())
    return Model2Calendar(
        data.version,
        data.coverage_start,
        data.coverage_end,
        tuple(Model2Session(**item.model_dump()) for item in data.sessions),
        data.weekly_expiries,
        component.provenance,
        data.expiry_reference,
    )


class ContractPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    expiry: date
    strike: int = Field(gt=0, strict=True)
    option_type: Literal["CE", "PE"]
    lot_size: int = Field(gt=0, strict=True)
    tick_size_inr: Decimal = Field(gt=0, allow_inf_nan=False)


def read_contracts(root: Path, component: BundleComponent) -> tuple[Model2Contract, ...]:
    raw = json.loads(component_path(root, component).read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("Contract metadata must be a non-empty JSON list.")
    result = []
    for row in raw:
        item = ContractPayload.model_validate(row)
        result.append(
            Model2Contract(
                item.contract_id,
                item.symbol,
                item.expiry,
                item.strike,
                item.option_type,
                item.lot_size,
                item.tick_size_inr,
                component.provenance,
            )
        )
    if len({item.contract_id for item in result}) != len(result):
        raise ValueError("Duplicate contract identity.")
    if len({(item.expiry, item.strike, item.option_type) for item in result}) != len(result):
        raise ValueError("Duplicate expiry/strike/type contract metadata.")
    return tuple(result)


class SettlementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expiry: date
    index_value: Decimal = Field(gt=0, allow_inf_nan=False)
    available_at: datetime


class SettlementFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["stockbot.official-settlements.v1"]
    source_kind: Literal["OFFICIAL_SETTLEMENT"]
    observations: tuple[SettlementPayload, ...]


def read_settlements(root: Path, component: BundleComponent) -> tuple[SettlementObservation, ...]:
    data = SettlementFile.model_validate_json(component_path(root, component).read_text())
    result = tuple(
        SettlementObservation(
            item.expiry, item.index_value, item.available_at, component.provenance
        )
        for item in data.observations
    )
    if len({item.expiry for item in result}) != len(result):
        raise ValueError("Duplicate official settlement date.")
    for item in result:
        if item.available_at < datetime.combine(item.expiry, time(15, 30), EXCHANGE_ZONE):
            raise ValueError("Closing settlement cannot be available before expiry-session close.")
    return result


class HaltPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: datetime
    ends_at: datetime
    available_at: datetime

    @model_validator(mode="after")
    def validate_halt(self) -> HaltPayload:
        for stamp in (self.starts_at, self.ends_at, self.available_at):
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("Halt evidence requires timezone-aware times.")
        if self.ends_at <= self.starts_at:
            raise ValueError("Halt end must follow its start.")
        return self


class HaltFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["stockbot.exchange-halts.v1"]
    source_kind: Literal["EXCHANGE_HALT"]
    observations: tuple[HaltPayload, ...]


def read_halts(root: Path, component: BundleComponent) -> tuple[HaltPayload, ...]:
    return HaltFile.model_validate_json(component_path(root, component).read_text()).observations


def price_rows(
    path: Path, *, session_date: date | None = None, csv_offset: int = 0
) -> Iterator[dict[str, Any]]:
    """Read one series; Parquet session filters and CSV seeks avoid whole-run loads."""
    if path.suffix == ".parquet":
        schema = pq.ParquetFile(path).schema_arrow
        if "timestamp" not in schema.names or not schema.field("timestamp").type.equals(
            pa.string()
        ):
            raise ValueError("Price timestamps must be explicit +05:30 ISO strings.")
        if session_date is None:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=4096):
                yield from batch.to_pylist()
        else:
            table = pq.read_table(
                path,
                filters=[
                    ("timestamp", ">=", session_date.isoformat()),
                    ("timestamp", "<", (session_date + timedelta(days=1)).isoformat()),
                ],
            )
            yield from table.to_pylist()
        return
    with path.open("rb") as source:
        line = source.readline()
        if not line:
            raise ValueError("A market CSV header is required.")
        header = next(csv.reader([line.decode("utf-8-sig")]))
        if csv_offset:
            source.seek(csv_offset)
        while line := source.readline():
            values = next(csv.reader([line.decode("utf-8")]))
            if len(values) != len(header):
                raise ValueError("Market CSV requires one complete record per physical line.")
            row = dict(zip(header, values, strict=True))
            if (
                session_date is not None
                and row.get("timestamp", "")[:10] != session_date.isoformat()
            ):
                if csv_offset:
                    break
                continue
            yield row


def decode_bar(row: dict[str, Any], component: BundleComponent) -> MarketBar:
    try:
        stamp = datetime.fromisoformat(str(row["timestamp"]))
        if stamp.utcoffset() != timedelta(hours=5, minutes=30):
            raise ValueError("Price timestamps require an explicit +05:30 timezone.")
        start = stamp - BAR_INTERVAL if component.timestamp_semantics == "interval_end" else stamp
        volume_raw = row.get("volume")
        volume = None
        if volume_raw not in (None, ""):
            if isinstance(volume_raw, bool):
                raise ValueError("Volume cannot be a boolean.")
            number = Decimal(str(volume_raw))
            if not number.is_finite() or number < 0 or number != number.to_integral_value():
                raise ValueError("Volume must be a non-negative integer.")
            volume = int(number)
        if component.kind == "options" and volume is None:
            raise ValueError("Model 2 option data requires an explicit volume column.")
        opening, high, low, close = (
            Decimal(str(row[key])) for key in ("open", "high", "low", "close")
        )
        return MarketBar(start, opening, high, low, close, volume, component.provenance)
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("Malformed Model 2 market-data row.") from exc


def validate_model2_contents(root: Path, manifest: Model2BundleManifest) -> None:
    contracts_component = manifest.component("contracts")
    calendar_component = manifest.component("calendar")
    assert contracts_component is not None and calendar_component is not None
    contracts = read_contracts(root, contracts_component)
    read_calendar(root, calendar_component)
    by_id = {item.contract_id: item for item in contracts}
    option_types = set()
    for component in manifest.components:
        path = component_path(root, component)
        if component.kind in PRICE_KINDS:
            if component.kind == "options":
                assert component.contract_id is not None
                if component.contract_id not in by_id:
                    raise ValueError("Option series has no matching contract metadata.")
                option_types.add(by_id[component.contract_id].option_type)
            previous = None
            count = 0
            for row in price_rows(path):
                bar = decode_bar(row, component)
                if (
                    component.kind == "options"
                    and component.contract_id is not None
                    and bar.interval_start.date() > by_id[component.contract_id].expiry
                ):
                    raise ValueError("Option data extends beyond its pinned contract expiry.")
                if previous is not None and bar.interval_start <= previous:
                    raise ValueError("Duplicate or unordered price rows.")
                previous = bar.interval_start
                count += 1
            if not count:
                raise ValueError("A declared market series cannot be empty.")
        elif component.kind == "settlements":
            read_settlements(root, component)
        elif component.kind == "halts":
            read_halts(root, component)
    if option_types != {"CE", "PE"}:
        raise ValueError("Model 2 requires both call and put price series.")


def build_model2_bundle(root: Path, components: Sequence[BundleComponent]) -> Model2BundleManifest:
    """Snapshot prepared files; never overwrite a different existing bundle."""
    payload = {
        "schema_version": MODEL2_BUNDLE_SCHEMA,
        "timezone": "Asia/Kolkata",
        "interval_minutes": 5,
        "components": [
            item.model_dump(mode="json")
            for item in sorted(components, key=lambda item: (item.kind, item.contract_id or ""))
        ],
    }
    manifest = Model2BundleManifest.model_validate(
        payload | {"bundle_checksum": canonical_checksum(payload)}
    )
    verify_model2_files(root, manifest)
    validate_model2_contents(root, manifest)
    target = root / MODEL2_MANIFEST_FILENAME
    if target.exists():
        if load_model2_bundle(root) != manifest:
            raise ValueError("An immutable bundle already exists with different inputs.")
        return manifest
    with target.open("x", encoding="utf-8") as output:
        output.write(manifest.model_dump_json(indent=2) + "\n")
    return manifest
