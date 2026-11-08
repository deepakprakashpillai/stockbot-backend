"""The session pipeline must preserve the existing V1.2 research outcome."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.domain.algorithms import BULL_CALL_SPREAD, BullCallSpreadHardGateParameters
from app.domain.backtests import evaluate_chronologically
from app.domain.option_spread_engine import simulate_option_spread_backtest
from app.domain.option_spread_models import OptionSpreadAssumptions
from app.domain.research_pipeline import run_chronological_option_research
from app.infrastructure.market_data.csv_reader import CsvMarketDataProvider
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider
from tests.market_fixture import market_bundle


def test_session_pipeline_matches_two_pass_reference(tmp_path: Path) -> None:
    bundle = market_bundle(tmp_path)
    spot_path = bundle / "spot/nifty_spot_5min.csv"
    provider = ParquetDerivativesProvider(bundle)
    spot = provider.enrich_spot_candles(
        CsvMarketDataProvider(spot_path).stream(date(2025, 7, 18), date(2025, 7, 21))
    )
    parameters = BullCallSpreadHardGateParameters()
    configured = BULL_CALL_SPREAD.configure(parameters.model_dump(mode="python"))
    assumptions = OptionSpreadAssumptions.from_parameters(
        parameters, starting_capital=Decimal("100000")
    )
    day = date(2025, 7, 21)
    expected_signals = [
        signal
        for signal in evaluate_chronologically(spot, configured)
        if signal.session_date == day
    ]
    expected = simulate_option_spread_backtest(
        expected_signals,
        provider,
        [candle for candle in spot if candle.timestamp.date() == day],
        assumptions,
    )
    observed_signals = []
    actual = run_chronological_option_research(
        provider.iter_enriched_spot_candles(
            CsvMarketDataProvider(spot_path).stream(date(2025, 7, 18), day)
        ),
        configured,
        provider,
        assumptions,
        requested_start=day,
        requested_end=day,
        included_sessions={day},
        on_signal=observed_signals.append,
    )
    assert observed_signals == expected_signals
    assert asdict(actual) == asdict(expected)
