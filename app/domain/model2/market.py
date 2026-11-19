"""Immutable market values and availability-aware interfaces for Model 2."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Protocol

EXCHANGE_ZONE = timezone(timedelta(hours=5, minutes=30))
BAR_INTERVAL = timedelta(minutes=5)
IndexName = Literal["spot", "india_vix"]
OptionType = Literal["CE", "PE"]


def exchange_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Market times must be timezone-aware.")
    return value.astimezone(EXCHANGE_ZONE)


class ObservationNotAvailable(ValueError):
    """A caller requested fields before their simulated availability time."""


@dataclass(frozen=True)
class Provenance:
    reference: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.reference.strip() or re.fullmatch(r"[0-9a-f]{64}", self.checksum) is None:
            raise ValueError("Market provenance requires a reference and SHA-256 checksum.")


@dataclass(frozen=True)
class Model2Contract:
    contract_id: str
    symbol: str
    expiry: date
    strike: int
    option_type: OptionType
    lot_size: int
    tick_size: Decimal
    provenance: Provenance

    def __post_init__(self) -> None:
        if not self.contract_id.strip() or not self.symbol.strip():
            raise ValueError("Contract identity and symbol are required.")
        if self.option_type not in {"CE", "PE"}:
            raise ValueError("Only CE/PE option contracts are supported.")
        if type(self.strike) is not int or type(self.lot_size) is not int:
            raise ValueError("Contract strike and lot size must be integers.")
        if (
            self.strike <= 0
            or self.lot_size <= 0
            or not self.tick_size.is_finite()
            or self.tick_size <= 0
        ):
            raise ValueError("Contract strike, lot size and rupee tick must be positive.")


@dataclass(frozen=True)
class OpenObservation:
    interval_start: datetime
    interval_end: datetime
    price: Decimal
    provenance: Provenance

    @property
    def available_at(self) -> datetime:
        return self.interval_start


@dataclass(frozen=True)
class CompletedObservation:
    interval_start: datetime
    interval_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    provenance: Provenance

    @property
    def available_at(self) -> datetime:
        return self.interval_end


@dataclass(frozen=True)
class MarketBar:
    """Storage-side value; engine-facing methods return field-specific views."""

    interval_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    provenance: Provenance

    def __post_init__(self) -> None:
        stamp = exchange_time(self.interval_start)
        if stamp != self.interval_start or stamp.second or stamp.microsecond or stamp.minute % 5:
            raise ValueError("Bars must align to exchange-local five-minute boundaries.")
        object.__setattr__(self, "interval_start", stamp)
        values = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("Market prices must be finite and non-negative.")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Invalid OHLC relationships.")
        if self.volume is not None and (type(self.volume) is not int or self.volume < 0):
            raise ValueError("Volume must be a non-negative integer or explicitly absent.")

    @property
    def interval_end(self) -> datetime:
        return self.interval_start + BAR_INTERVAL

    def opening(self, as_of: datetime) -> OpenObservation:
        if exchange_time(as_of) < self.interval_start:
            raise ObservationNotAvailable("Opening price is not available yet.")
        return OpenObservation(self.interval_start, self.interval_end, self.open, self.provenance)

    def completed(self, as_of: datetime) -> CompletedObservation:
        if exchange_time(as_of) < self.interval_end:
            raise ObservationNotAvailable("Closing prices and volume are not available yet.")
        return CompletedObservation(
            self.interval_start,
            self.interval_end,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.provenance,
        )


@dataclass(frozen=True)
class SettlementObservation:
    expiry: date
    index_value: Decimal
    available_at: datetime
    provenance: Provenance

    def __post_init__(self) -> None:
        observed = exchange_time(self.available_at)
        object.__setattr__(self, "available_at", observed)
        if (
            observed.date() < self.expiry
            or not self.index_value.is_finite()
            or self.index_value <= 0
        ):
            raise ValueError("Invalid official settlement observation.")


@dataclass(frozen=True)
class HaltObservation:
    starts_at: datetime
    available_at: datetime
    provenance: Provenance

    def __post_init__(self) -> None:
        exchange_time(self.starts_at)
        exchange_time(self.available_at)


class Model2PriceFeed(Protocol):
    """Queries expose missing data as None and reject requests for future fields."""

    def contract(
        self, expiry: date, strike: int, option_type: OptionType
    ) -> Model2Contract | None: ...

    def index_close(
        self, name: IndexName, end: datetime, *, as_of: datetime
    ) -> CompletedObservation | None: ...

    def option_open(
        self, contract: Model2Contract, start: datetime, *, as_of: datetime
    ) -> OpenObservation | None: ...

    def option_close(
        self, contract: Model2Contract, end: datetime, *, as_of: datetime
    ) -> CompletedObservation | None: ...

    def settlement(self, expiry: date, *, as_of: datetime) -> SettlementObservation | None: ...
