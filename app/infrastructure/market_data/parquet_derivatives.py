"""Read immutable Parquet derivative bundles by requested market session."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from app.domain.backtests import EXCHANGE_OFFSET, MarketCandle
from app.domain.derivatives import OptionCandle, OptionContract


def _normalize_ts(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone(EXCHANGE_OFFSET))
    return ts.astimezone(timezone(EXCHANGE_OFFSET))


@dataclass
class DerivativesSessionData:
    session_date: date
    spot_candles: list[MarketCandle]
    futures_vwap: dict[datetime, float]
    india_vix: dict[datetime, float]
    previous_india_vix_close: float | None
    active_expiry: date
    dataset_dir: Path
    contracts: dict[tuple[int, str], OptionContract]
    option_candle_cache: dict[tuple[date, int, str], dict[datetime, OptionCandle]] = field(
        default_factory=dict
    )

    def get_option_contract(self, strike: int, option_type: str = "CE") -> OptionContract | None:
        return self.contracts.get((strike, option_type))

    def get_option_candles(
        self, strike: int, option_type: str = "CE"
    ) -> dict[datetime, OptionCandle]:
        cache_key = (self.session_date, strike, option_type)
        if cache_key in self.option_candle_cache:
            return self.option_candle_cache[cache_key]
        if self.get_option_contract(strike, option_type) is None:
            return {}
        parquet_file = (
            self.dataset_dir
            / "options"
            / self.active_expiry.isoformat()
            / f"{strike}_{option_type}.parquet"
        )
        if not parquet_file.exists():
            return {}

        data = pq.read_table(parquet_file).to_pydict()
        candles: dict[datetime, OptionCandle] = {}
        timestamps = data.get("timestamp", [])
        volumes = data.get("volume", [0] * len(timestamps))
        open_interests = data.get("open_interest", [0] * len(timestamps))
        for index, raw_timestamp in enumerate(timestamps):
            timestamp = _normalize_ts(datetime.fromisoformat(str(raw_timestamp)))
            if timestamp.date() != self.session_date:
                continue
            candles[timestamp] = OptionCandle(
                timestamp=timestamp,
                open=float(data["open"][index]),
                high=float(data["high"][index]),
                low=float(data["low"][index]),
                close=float(data["close"][index]),
                volume=int(volumes[index]),
                open_interest=int(open_interests[index]),
            )
        self.option_candle_cache[cache_key] = candles
        return candles


class ParquetDerivativesProvider:
    """Read an immutable NIFTY derivatives bundle and synchronize completed candles."""

    def __init__(self, dataset_dir: Path) -> None:
        self.dataset_dir = dataset_dir.resolve()
        self.contracts_master_path = self.dataset_dir / "contracts_master.json"
        self.vix_parquet_path = self.dataset_dir / "vix" / "india_vix_5min.parquet"
        self.futures_parquet_path = self.dataset_dir / "futures" / "nifty_futures_5min.parquet"
        self.contracts_master: list[dict[str, Any]] = []
        self.contracts_by_expiry: dict[date, dict[tuple[int, str], OptionContract]] = {}
        self.available_weekly_expiries: list[date] = []
        self._vix_by_session: dict[date, dict[datetime, float]] | None = None
        self._previous_vix_close_by_session: dict[date, float] | None = None
        self._vix_close_by_session: dict[date, float] | None = None
        self._session_vwap_cache: OrderedDict[date, dict[datetime, float]] = OrderedDict()
        self._session_futures_close_cache: OrderedDict[date, dict[datetime, float]] = OrderedDict()
        self._load_metadata()

    @property
    def ready(self) -> bool:
        return bool(
            self.contracts_master_path.exists()
            and self.vix_parquet_path.exists()
            and self.futures_parquet_path.exists()
            and self.available_weekly_expiries
        )

    @property
    def available_expiries(self) -> list[date]:
        """Deprecated alias retained for historical callers."""

        return self.available_weekly_expiries

    @staticmethod
    def _tick_size_rupees(raw_value: Any) -> Decimal:
        if raw_value is None:
            raise ValueError("Option contract tick size is required.")
        raw = Decimal(str(raw_value))
        if not raw.is_finite() or raw <= 0:
            raise ValueError("Option contract tick size must be positive and finite.")
        return raw / Decimal("100") if raw >= 1 else raw

    def _load_metadata(self) -> None:
        if not self.contracts_master_path.exists():
            return
        self.contracts_master = json.loads(self.contracts_master_path.read_text(encoding="utf-8"))
        for value in self.contracts_master:
            expiry_value = value.get("expiry")
            strike_value = value.get("strike_price")
            option_type = value.get("instrument_type")
            if not expiry_value or strike_value is None or option_type not in {"CE", "PE"}:
                continue
            if not bool(value.get("weekly", False)):
                continue
            expiry = datetime.strptime(str(expiry_value), "%Y-%m-%d").date()
            strike = int(Decimal(str(strike_value)))
            raw_lot = value.get("lot_size", value.get("minimum_lot"))
            if raw_lot is None or isinstance(raw_lot, bool):
                raise ValueError("Option contract lot size is required.")
            lot = Decimal(str(raw_lot))
            if not lot.is_finite() or lot <= 0 or lot != lot.to_integral_value():
                raise ValueError("Option contract lot size must be a positive integer.")
            contract = OptionContract(
                trading_symbol=str(
                    value.get("trading_symbol") or f"NIFTY {strike} {option_type} {expiry}"
                ),
                expiry=expiry,
                strike=strike,
                option_type=str(option_type),
                lot_size=int(lot),
                tick_size=self._tick_size_rupees(value.get("tick_size")),
                weekly=True,
            )
            contracts = self.contracts_by_expiry.setdefault(expiry, {})
            key = (strike, str(option_type))
            if key in contracts:
                raise ValueError(f"Duplicate option contract metadata: {expiry} {key}.")
            contracts[key] = contract
        self.available_weekly_expiries = sorted(self.contracts_by_expiry)

    def get_active_expiry_for_date(
        self, session_date: date, *, allow_same_day: bool = True
    ) -> date | None:
        for expiry in self.available_weekly_expiries:
            if expiry > session_date or (allow_same_day and expiry == session_date):
                return expiry
        return None

    def get_lot_size(self, session_date: date, *, allow_same_day: bool = True) -> int:
        expiry = self.get_active_expiry_for_date(session_date, allow_same_day=allow_same_day)
        if expiry is None:
            raise ValueError("No weekly option expiry is available for the session.")
        contracts = self.contracts_by_expiry.get(expiry, {})
        first = next(iter(contracts.values()), None)
        if first is None:
            raise ValueError("The weekly expiry has no option contracts.")
        return first.lot_size

    def _load_vix_history(self) -> None:
        if self._vix_by_session is not None:
            return
        self._vix_by_session = {}
        self._previous_vix_close_by_session = {}
        self._vix_close_by_session = {}
        if not self.vix_parquet_path.exists():
            return
        data = pq.read_table(self.vix_parquet_path).to_pydict()
        latest_by_session: dict[date, tuple[datetime, float]] = {}
        for index, raw_timestamp in enumerate(data.get("timestamp", [])):
            timestamp = _normalize_ts(datetime.fromisoformat(str(raw_timestamp)))
            close = float(data["close"][index])
            session_date = timestamp.date()
            self._vix_by_session.setdefault(session_date, {})[timestamp] = close
            latest = latest_by_session.get(session_date)
            if latest is None or timestamp > latest[0]:
                latest_by_session[session_date] = (timestamp, close)
        previous_close: float | None = None
        for session_date in sorted(latest_by_session):
            assert self._vix_close_by_session is not None
            self._vix_close_by_session[session_date] = latest_by_session[session_date][1]
            if previous_close is not None:
                self._previous_vix_close_by_session[session_date] = previous_close
            previous_close = self._vix_close_by_session[session_date]

    def vix_close(self, session_date: date) -> float | None:
        self._load_vix_history()
        assert self._vix_close_by_session is not None
        return self._vix_close_by_session.get(session_date)

    def vix_timestamps(self, session_date: date) -> set[datetime]:
        self._load_vix_history()
        assert self._vix_by_session is not None
        return set(self._vix_by_session.get(session_date, {}))

    def previous_vix_close(self, session_date: date) -> float | None:
        self._load_vix_history()
        assert self._previous_vix_close_by_session is not None
        return self._previous_vix_close_by_session.get(session_date)

    def _load_session_vix(self, session_date: date) -> dict[datetime, float]:
        self._load_vix_history()
        assert self._vix_by_session is not None
        return self._vix_by_session.get(session_date, {})

    def _enrich_session(
        self, session_date: date, candles: Iterable[MarketCandle]
    ) -> Iterator[MarketCandle]:
        futures_vwap = self._compute_session_futures_vwap(session_date)
        futures_close = self._session_futures_close_cache.get(session_date, {})
        vix = self._load_session_vix(session_date)
        previous_vix = self.previous_vix_close(session_date)
        for candle in sorted(candles, key=lambda item: item.timestamp):
            yield MarketCandle(
                timestamp=candle.timestamp,
                high=candle.high,
                close=candle.close,
                futures_vwap=futures_vwap.get(candle.timestamp),
                india_vix=vix.get(candle.timestamp),
                previous_india_vix=previous_vix,
                futures_close=futures_close.get(candle.timestamp),
                futures_observed_at=(
                    candle.timestamp if candle.timestamp in futures_close else None
                ),
                india_vix_observed_at=(candle.timestamp if candle.timestamp in vix else None),
            )

    def enrich_spot_candles(self, spot_candles: Iterable[MarketCandle]) -> list[MarketCandle]:
        """Preserve the existing sorting behaviour for non-streaming callers."""
        by_session: dict[date, list[MarketCandle]] = {}
        for candle in spot_candles:
            normalized = self._normalize_spot(candle)
            by_session.setdefault(normalized.timestamp.date(), []).append(normalized)
        enriched: list[MarketCandle] = []
        for session_date in sorted(by_session):
            enriched.extend(self._enrich_session(session_date, by_session[session_date]))
        return enriched

    @staticmethod
    def _normalize_spot(candle: MarketCandle) -> MarketCandle:
        return MarketCandle(
            timestamp=_normalize_ts(candle.timestamp),
            high=candle.high,
            close=candle.close,
            futures_vwap=candle.futures_vwap,
            india_vix=candle.india_vix,
            previous_india_vix=candle.previous_india_vix,
            futures_close=candle.futures_close,
            futures_contract_id=candle.futures_contract_id,
            futures_open_interest=candle.futures_open_interest,
            futures_observed_at=candle.futures_observed_at,
            india_vix_observed_at=candle.india_vix_observed_at,
        )

    def iter_enriched_spot_candles(
        self, spot_candles: Iterable[MarketCandle]
    ) -> Iterator[MarketCandle]:
        """Read chronological input with at most one spot session buffered."""
        normalized = (self._normalize_spot(candle) for candle in spot_candles)
        last_session: date | None = None
        for session_date, group in groupby(normalized, key=lambda item: item.timestamp.date()):
            if last_session is not None and session_date <= last_session:
                raise ValueError("Spot market sessions must be strictly chronological.")
            yield from self._enrich_session(session_date, group)
            last_session = session_date

    def load_session(
        self,
        session_date: date,
        spot_candles: list[MarketCandle],
        *,
        allow_same_day: bool = True,
    ) -> DerivativesSessionData:
        active_expiry = self.get_active_expiry_for_date(session_date, allow_same_day=allow_same_day)
        if active_expiry is None:
            raise ValueError("No eligible weekly option expiry is available for the session.")
        enriched = self.enrich_spot_candles(spot_candles)
        return DerivativesSessionData(
            session_date=session_date,
            spot_candles=enriched,
            futures_vwap={
                candle.timestamp: candle.futures_vwap
                for candle in enriched
                if candle.futures_vwap is not None
            },
            india_vix={
                candle.timestamp: candle.india_vix
                for candle in enriched
                if candle.india_vix is not None
            },
            previous_india_vix_close=self.previous_vix_close(session_date),
            active_expiry=active_expiry,
            dataset_dir=self.dataset_dir,
            contracts=self.contracts_by_expiry.get(active_expiry, {}),
        )

    def _compute_session_futures_vwap(self, session_date: date) -> dict[datetime, float]:
        if session_date in self._session_vwap_cache:
            self._session_vwap_cache.move_to_end(session_date)
            return self._session_vwap_cache[session_date]
        if not self.futures_parquet_path.exists():
            return {}
        next_date = session_date + timedelta(days=1)
        data = pq.read_table(
            self.futures_parquet_path,
            filters=[
                ("timestamp", ">=", session_date.isoformat()),
                ("timestamp", "<", next_date.isoformat()),
            ],
        ).to_pydict()
        rows: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal]] = []
        for index, raw_timestamp in enumerate(data.get("timestamp", [])):
            timestamp = _normalize_ts(datetime.fromisoformat(str(raw_timestamp)))
            if timestamp.date() != session_date:
                continue
            rows.append(
                (
                    timestamp,
                    Decimal(str(data["high"][index])),
                    Decimal(str(data["low"][index])),
                    Decimal(str(data["close"][index])),
                    Decimal(str(data["volume"][index])),
                )
            )
        vwap_map: dict[datetime, float] = {}
        close_map: dict[datetime, float] = {}
        cumulative_volume = Decimal("0")
        cumulative_price_volume = Decimal("0")
        for timestamp, high, low, close, volume in sorted(rows):
            close_map[timestamp] = float(close)
            typical_price = (high + low + close) / Decimal("3")
            cumulative_volume += volume
            cumulative_price_volume += typical_price * volume
            if cumulative_volume > 0:
                vwap_map[timestamp] = float(
                    (cumulative_price_volume / cumulative_volume).quantize(Decimal("0.01"))
                )
        self._session_vwap_cache[session_date] = vwap_map
        self._session_futures_close_cache[session_date] = close_map
        if len(self._session_vwap_cache) > 8:
            evicted_session, _ = self._session_vwap_cache.popitem(last=False)
            self._session_futures_close_cache.pop(evicted_session, None)
        return vwap_map
