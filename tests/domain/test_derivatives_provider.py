from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.backtests import MarketCandle, SignalDecision
from app.domain.derivatives_provider import ParquetDerivativesProvider
from app.domain.option_spread_execution import (
    OptionSpreadAssumptions,
    simulate_option_spread_backtest,
)
from tests.market_fixture import IST, market_bundle

DATASET_DIR = Path("data/datasets/upstox_nifty_derivatives")


def test_session_reads_keep_vwap_and_option_caches_bounded(tmp_path: Path) -> None:
    provider = ParquetDerivativesProvider(market_bundle(tmp_path))
    for day in (date(2025, 7, 18), date(2025, 7, 21)):
        stamp = datetime.combine(day, time(9, 20), IST)
        session = provider.load_session(day, [MarketCandle(stamp, 25000.0, 25000.0, None, None)])
        assert session.futures_vwap[stamp] == 24900.0
        assert session.spot_candles[0].futures_close == 24900.0
        assert session.spot_candles[0].futures_observed_at == stamp
        assert session.spot_candles[0].india_vix_observed_at == stamp
        assert session.spot_candles[0].futures_contract_id is None
        assert session.spot_candles[0].futures_open_interest is None
        assert session.option_candle_cache == {}
        assert stamp in session.get_option_candles(25000, "CE")

    for offset in range(20):
        provider._compute_session_futures_vwap(date(2025, 7, 18) + timedelta(days=offset))
    assert len(provider._session_vwap_cache) <= 8
    assert len(provider._session_futures_close_cache) <= 8
    assert provider.previous_vix_close(date(2025, 7, 21)) == 20.0
    assert provider.vix_close(date(2025, 7, 21)) == 19.0
    assert len(provider.vix_timestamps(date(2025, 7, 21))) == 75
    assert len(provider._vix_by_session or {}) == 2


def test_streamed_enrichment_matches_sorted_enrichment(tmp_path: Path) -> None:
    provider = ParquetDerivativesProvider(market_bundle(tmp_path))
    candles = [
        MarketCandle(datetime.combine(day, time(9, 20), IST), 25000.0, 25000.0, None, None)
        for day in (date(2025, 7, 18), date(2025, 7, 21))
    ]
    assert list(provider.iter_enriched_spot_candles(iter(candles))) == provider.enrich_spot_candles(
        candles
    )
    with pytest.raises(ValueError, match="strictly chronological"):
        list(provider.iter_enriched_spot_candles(reversed(candles)))


def test_parquet_derivatives_provider_real_data() -> None:
    if not DATASET_DIR.exists():
        pytest.skip("Optional local derivatives market dataset is not installed.")

    provider = ParquetDerivativesProvider(DATASET_DIR)
    assert len(provider.available_expiries) >= 2
    assert date(2025, 7, 24) in provider.available_expiries

    # Test loading session on 2025-07-21
    session_d = date(2025, 7, 21)
    sample_candles = [
        MarketCandle(
            timestamp=datetime(2025, 7, 21, 10, 5),
            high=25060.0,
            close=25055.8,
            futures_vwap=None,
            india_vix=None,
        )
    ]
    session_data = provider.load_session(session_d, sample_candles)
    assert session_data.active_expiry == date(2025, 7, 24)

    # Check that VWAP was computed
    assert len(session_data.futures_vwap) > 0
    t1 = provider.load_session(session_d, sample_candles).spot_candles[0].timestamp
    assert t1 in session_data.futures_vwap
    assert session_data.futures_vwap[t1] > 24000.0

    # Check that VIX was loaded
    assert len(session_data.india_vix) > 0
    assert t1 in session_data.india_vix
    assert 9.0 <= session_data.india_vix[t1] <= 25.0

    # Check that real option candles load for ATM strike 25050 CE
    option_candles = session_data.get_option_candles(25050, "CE")
    assert len(option_candles) > 0
    assert t1 in option_candles
    assert option_candles[t1].close > 0


def test_simulate_spread_on_real_option_candles() -> None:
    if not DATASET_DIR.exists():
        pytest.skip("Optional local derivatives market dataset is not installed.")

    provider = ParquetDerivativesProvider(DATASET_DIR)
    session_d = date(2025, 7, 21)
    entry_ts = datetime(2025, 7, 21, 10, 5)
    exit_ts = datetime(2025, 7, 21, 14, 30)

    spot_candles = [
        MarketCandle(
            timestamp=entry_ts, high=25060.0, close=25055.8, futures_vwap=25000.0, india_vix=11.5
        ),
        MarketCandle(
            timestamp=exit_ts, high=25100.0, close=25090.0, futures_vwap=25020.0, india_vix=11.4
        ),
    ]

    signal = SignalDecision(
        timestamp=entry_ts,
        session_date=session_d,
        decision="BUY",
        rejection_reason=None,
        window="window_1",
        score=4,
        inputs={},
    )

    assumptions = OptionSpreadAssumptions(
        starting_capital=Decimal("500000.00"),
        lots=1,
        slippage_percent=Decimal("5.00"),
        maximum_debit_percent=Decimal("100.00"),
        square_off_time=time(14, 30),
    )

    result = simulate_option_spread_backtest(
        signals=[signal],
        derivatives_provider=provider,
        spot_candles=spot_candles,
        assumptions=assumptions,
        strike_width=200,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.lot_size == 75
    assert trade.lots_traded == 1
    assert "25050" in trade.buy_leg.contract_symbol
    assert "25250" in trade.sell_leg.contract_symbol
    assert trade.entry_net_debit_inr > 0
    assert trade.statutory_charges.total_charges == Decimal("400.00")
    assert trade.unit == "INR"
    assert trade.financial_pnl_available is True
