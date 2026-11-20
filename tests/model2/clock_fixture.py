"""Synthetic complete sessions with independent hand-declared price changes."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.domain.model2.calendar import Model2Calendar, Model2Session
from app.domain.model2.execution import ReplayRequest, market_at
from app.domain.model2.market import (
    BAR_INTERVAL,
    HaltObservation,
    MarketBar,
    Model2Contract,
    ObservationNotAvailable,
    Provenance,
    SettlementObservation,
)
from app.domain.model2.parameters import Model2Parameters

SOURCE = Provenance("Synthetic complete clock fixture; not historical market results", "c" * 64)


@dataclass
class ClockFeed:
    calendar: Model2Calendar
    contracts: dict[tuple[date, int, str], Model2Contract] = field(default_factory=dict)
    prices: dict[tuple[str, datetime], tuple[str, str, int] | None] = field(default_factory=dict)
    indexes: dict[tuple[str, datetime], str | None] = field(default_factory=dict)
    settlements: dict[date, SettlementObservation] = field(default_factory=dict)
    halt_records: list[tuple[HaltObservation, datetime]] = field(default_factory=list)
    calls: list[tuple[str, datetime, datetime]] = field(default_factory=list)

    def contract(self, expiry, strike, option_type):
        return self.contracts.get((expiry, strike, option_type))

    def index_close(self, name, end, *, as_of):
        self.guard(name, end, as_of)
        start = end - BAR_INTERVAL
        raw = self.indexes.get((name, start), "25000" if name == "spot" else "16")
        if raw is None:
            return None
        value = Decimal(raw)
        return MarketBar(start, value, value, value, value, None, SOURCE).completed(as_of)

    def guard(self, kind, when, as_of):
        self.calls.append((kind, when, as_of))
        if when > as_of:
            raise ObservationNotAvailable("Fixture detected a future field query.")

    def bar(self, contract, start):
        default = "30" if "SHORT" in contract.contract_id else "10"
        row = self.prices.get((contract.contract_id, start), (default, default, 100))
        if row is None:
            return None
        opening, closing, volume = row
        opening, closing = Decimal(opening), Decimal(closing)
        return MarketBar(
            start, opening, max(opening, closing), min(opening, closing), closing, volume, SOURCE
        )

    def option_open(self, contract, start, *, as_of):
        self.guard("open:" + contract.contract_id, start, as_of)
        row = self.bar(contract, start)
        return row.opening(as_of) if row is not None else None

    def option_close(self, contract, end, *, as_of):
        self.guard("close:" + contract.contract_id, end, as_of)
        row = self.bar(contract, end - BAR_INTERVAL)
        return row.completed(as_of) if row is not None else None

    def settlement(self, expiry, *, as_of):
        self.guard("settlement", as_of, as_of)
        observed = self.settlements.get(expiry)
        return observed if observed is not None and observed.available_at <= as_of else None

    def halts(self, *, as_of):
        return tuple(
            record
            for record, end in self.halt_records
            if record.starts_at <= as_of < end and record.available_at <= as_of
        )

    def set_prices(
        self,
        expiry,
        at,
        *,
        short_open="30",
        long_open="10",
        short_close=None,
        long_close=None,
        volume=100,
    ):
        for contract in self.contracts.values():
            if contract.expiry != expiry:
                continue
            short = "SHORT" in contract.contract_id
            opening = short_open if short else long_open
            closing = (short_close if short else long_close) or opening
            self.prices[(contract.contract_id, at)] = (opening, closing, volume)

    def no_quotes_after_entry(self, expiry):
        for day in (self.calendar.previous_session(expiry).session_date, expiry):
            session = self.calendar.session(day)
            for at in session.opening_times():
                if day != expiry and at.time() <= time(15, 20):
                    continue
                for contract in self.contracts.values():
                    if contract.expiry == expiry:
                        self.prices[(contract.contract_id, at)] = None


def fixture(
    *, expiries=(date(2026, 1, 6),), width=200, slip="0", filter_on=False, capital="500000"
):
    first = expiries[0] - timedelta(days=4)
    last = expiries[-1]
    days = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            days.append(Model2Session(day, "NORMAL", time(9, 15), time(15, 30)))
        day += timedelta(days=1)
    calendar = Model2Calendar(
        "clock-fixture.v1",
        first,
        last,
        tuple(days),
        tuple(expiries),
        SOURCE,
        "Synthetic weekly expiry schedule",
    )
    feed = ClockFeed(calendar)
    for expiry in expiries:
        for role, strike, kind in (
            ("SHORT_CALL", 25250, "CE"),
            ("LONG_CALL", 25250 + width, "CE"),
            ("SHORT_PUT", 24750, "PE"),
            ("LONG_PUT", 24750 - width, "PE"),
        ):
            feed.contracts[(expiry, strike, kind)] = Model2Contract(
                f"{expiry}:{role}", role, expiry, strike, kind, 65, Decimal("0.05"), SOURCE
            )
    start = calendar.previous_session(expiries[0]).session_date
    request = ReplayRequest(
        "synthetic-dataset@immutable-1",
        start,
        last,
        Decimal(capital),
        Model2Parameters(
            wing_width_points=width,
            slippage_percent=Decimal(slip),
            vix_state_filter_enabled=filter_on,
        ),
        tuple((expiry, market_at(expiry, 15, 35)) for expiry in expiries),
    )
    return request, feed, calendar
