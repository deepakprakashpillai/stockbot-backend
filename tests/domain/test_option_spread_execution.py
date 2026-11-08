from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from app.domain.backtests import MarketCandle, SignalDecision
from app.domain.derivatives_provider import OptionCandle, OptionContract
from app.domain.option_spread_execution import (
    OptionExitReason,
    OptionSpreadAssumptions,
    StatutoryCostCalculator,
    simulate_option_spread_backtest,
)


def test_statutory_cost_calculator_exact_values() -> None:
    calculator = StatutoryCostCalculator()
    # Scenario:
    # Buy 1 lot (75 qty) @ 100 = 7500 turnover
    # Sell 1 lot (75 qty) @ 40 = 3000 turnover
    # Exit Sell Leg 1 @ 150 = 11250 turnover
    # Exit Buy Leg 2 @ 20 = 1500 turnover
    # Total buy turnover: 7500 + 1500 = 9000
    # Total sell turnover: 3000 + 11250 = 14250
    # Total turnover: 23250
    charges = calculator.calculate(
        buy_entry_turnover=Decimal("7500.00"),
        sell_entry_turnover=Decimal("3000.00"),
        buy_exit_turnover=Decimal("1500.00"),
        sell_exit_turnover=Decimal("11250.00"),
        orders_count=4,
    )

    # Brokerage: 4 * 20 = 80.00
    assert charges.brokerage == Decimal("80.00")
    # STT: 14250 * 0.001 = 14.25
    assert charges.stt == Decimal("14.25")
    # Exchange txn: 23250 * 0.000505 = 11.74125 -> 11.74
    assert charges.exchange_txn_charges == Decimal("11.74")
    # Stamp Duty: 9000 * 0.00003 = 0.27
    assert charges.stamp_duty == Decimal("0.27")
    # GST: (80 + 11.74 + 0.02) * 0.18 = 16.52
    assert charges.gst == Decimal("16.52")
    # Total charges
    assert charges.total_charges == (
        charges.brokerage
        + charges.stt
        + charges.exchange_txn_charges
        + charges.gst
        + charges.sebi_charges
        + charges.stamp_duty
    )


def test_option_spread_simulation_target_hit() -> None:
    session_d = date(2025, 7, 21)
    t1 = datetime(2025, 7, 21, 10, 0)
    t2 = datetime(2025, 7, 21, 10, 30)

    spot_candles = [
        MarketCandle(
            timestamp=t1, high=25010.0, close=25000.0, futures_vwap=24950.0, india_vix=11.5
        ),
        MarketCandle(
            timestamp=t2, high=25150.0, close=25120.0, futures_vwap=24980.0, india_vix=11.2
        ),
    ]

    signal = SignalDecision(
        timestamp=t1,
        session_date=session_d,
        decision="BUY",
        rejection_reason=None,
        window="window_1",
        score=4,
        inputs={},
    )

    # Mock provider
    provider = MagicMock()
    provider.get_lot_size.return_value = 75

    session_data = MagicMock()
    session_data.active_expiry = date(2025, 7, 24)

    # ATM is 25000, Sell is 25200
    buy_candles = {
        t1: OptionCandle(
            timestamp=t1,
            open=100.0,
            high=105.0,
            low=95.0,
            close=100.0,
            volume=1000,
            open_interest=5000,
        ),
        t2: OptionCandle(
            timestamp=t2,
            open=280.0,
            high=305.0,
            low=275.0,
            close=300.0,
            volume=2000,
            open_interest=6000,
        ),
    }
    sell_candles = {
        t1: OptionCandle(
            timestamp=t1,
            open=40.0,
            high=45.0,
            low=35.0,
            close=40.0,
            volume=1000,
            open_interest=5000,
        ),
        t2: OptionCandle(
            timestamp=t2,
            open=60.0,
            high=65.0,
            low=55.0,
            close=60.0,
            volume=2000,
            open_interest=6000,
        ),
    }

    session_data.get_option_candles.side_effect = lambda strike, opt_type: (
        buy_candles if strike == 25000 else sell_candles
    )
    session_data.get_option_contract.side_effect = lambda strike, option_type: OptionContract(
        trading_symbol=f"NIFTY_{strike}_{option_type}",
        expiry=date(2025, 7, 24),
        strike=strike,
        option_type=option_type,
        lot_size=75,
        tick_size=Decimal("0.05"),
        weekly=True,
    )
    provider.load_session.return_value = session_data

    assumptions = OptionSpreadAssumptions(
        starting_capital=Decimal("500000.00"),
        lots=1,
        slippage_percent=Decimal("5.00"),
        target_max_profit_percent=Decimal("50.00"),
        stop_loss_percent=Decimal("40.00"),
    )

    journal: list[dict] = []
    result = simulate_option_spread_backtest(
        signals=[signal],
        derivatives_provider=provider,
        spot_candles=spot_candles,
        assumptions=assumptions,
        strike_width=200,
        observer=journal.append,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]

    # Entry: buy 100 × 1.05 and sell 40 × 0.95, rounded to the ₹0.05 tick.
    assert trade.entry_net_debit_points == Decimal("67.00")
    # The exit uses the same adverse percentage pricing and exceeds the V1 target.
    assert trade.exit_reason == OptionExitReason.TARGET_REACHED
    assert trade.exit_timestamp.replace(tzinfo=None) == t2
    assert trade.gross_pnl_inr == Decimal("11625.00")
    assert trade.statutory_charges.total_charges == Decimal("400.00")
    assert trade.net_pnl_inr == Decimal("11225.00")
    opened = next(event["payload"] for event in journal if event["event_type"] == "POSITION_OPENED")
    cap = opened["spread_attempts"][0]["checks"][1]
    assert (cap["observed_value"], cap["comparator"], cap["threshold_value"], cap["status"]) == (
        "67.00",
        "<=",
        "70.00",
        "passed",
    )
    checkpoint = next(
        event["payload"] for event in journal if event["event_type"] == "POSITION_CHECK"
    )
    assert checkpoint["action"] == "TARGET_REACHED"
    assert [item["status"] for item in checkpoint["checks"]] == [
        "failed",
        "passed",
        "not_evaluated",
        "not_evaluated",
        "not_evaluated",
    ]
    assert checkpoint["checks"][1]["observed_value"] == "240.00"

    # Metrics
    metrics = result.metrics(starting_capital=Decimal("500000.00"))
    assert metrics["total_trades"] == 1
    assert metrics["wins"] == 1
    assert metrics["unit"] == "INR"
    assert Decimal(str(metrics["ending_equity_inr"])) > Decimal("500000.00")
