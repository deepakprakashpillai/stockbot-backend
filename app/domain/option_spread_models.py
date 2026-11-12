"""Immutable inputs and outputs for estimated option-spread simulation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum

from app.domain.algorithms import BullCallSpreadFuturesParameters, BullCallSpreadSignalParameters

INR_RESULT_UNIT = "INR"
FINANCIAL_DECIMAL_QUANTUM = Decimal("0.01")
SPREAD_EXECUTION_ADAPTER_VERSION = "2.0.0-estimated-ohlc-v1"
PRICING_QUALITY = "estimated_historical_ohlc"


class OpenPositionDataGapError(ValueError):
    def __init__(self, timestamp: datetime, missing: list[str]) -> None:
        self.timestamp = timestamp
        self.missing = missing
        super().__init__(
            f"Required synchronized data is missing after entry at {timestamp.isoformat()}: "
            + ", ".join(missing)
        )


class OptionExecutionCancelled(RuntimeError):
    """Raised only when the durable run record requests cancellation."""


class OptionExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TARGET_REACHED = "TARGET_REACHED"
    TREND_REVERSAL = "TREND_REVERSAL"
    SESSION_SQUARE_OFF = "SESSION_SQUARE_OFF"
    VIX_SPIKE = "VIX_SPIKE"


@dataclass(frozen=True)
class StatutoryCharges:
    """Compatibility shape; V1 uses only the primary document's flat cost."""

    brokerage: Decimal
    stt: Decimal = Decimal("0.00")
    exchange_txn_charges: Decimal = Decimal("0.00")
    gst: Decimal = Decimal("0.00")
    sebi_charges: Decimal = Decimal("0.00")
    stamp_duty: Decimal = Decimal("0.00")
    total_charges: Decimal = Decimal("0.00")

    def snapshot(self) -> dict[str, str]:
        return {
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_txn_charges": str(self.exchange_txn_charges),
            "gst": str(self.gst),
            "sebi_charges": str(self.sebi_charges),
            "stamp_duty": str(self.stamp_duty),
            "total_charges": str(self.total_charges),
        }


class StatutoryCostCalculator:
    """Legacy calculator retained for historical imports; V1 does not use it."""

    def __init__(
        self,
        *,
        brokerage_per_order: Decimal = Decimal("20.00"),
        stt_rate: Decimal = Decimal("0.001"),
        exchange_rate: Decimal = Decimal("0.000505"),
        gst_rate: Decimal = Decimal("0.18"),
        sebi_rate: Decimal = Decimal("0.000001"),
        stamp_rate: Decimal = Decimal("0.00003"),
    ) -> None:
        self.brokerage_per_order = brokerage_per_order
        self.stt_rate = stt_rate
        self.exchange_rate = exchange_rate
        self.gst_rate = gst_rate
        self.sebi_rate = sebi_rate
        self.stamp_rate = stamp_rate

    def calculate(
        self,
        buy_entry_turnover: Decimal,
        sell_entry_turnover: Decimal,
        buy_exit_turnover: Decimal,
        sell_exit_turnover: Decimal,
        orders_count: int = 4,
    ) -> StatutoryCharges:
        buy_turnover = buy_entry_turnover + buy_exit_turnover
        sell_turnover = sell_entry_turnover + sell_exit_turnover
        total_turnover = buy_turnover + sell_turnover
        brokerage = (self.brokerage_per_order * Decimal(orders_count)).quantize(
            FINANCIAL_DECIMAL_QUANTUM
        )
        stt = (sell_turnover * self.stt_rate).quantize(FINANCIAL_DECIMAL_QUANTUM)
        exchange = (total_turnover * self.exchange_rate).quantize(FINANCIAL_DECIMAL_QUANTUM)
        sebi = (total_turnover * self.sebi_rate).quantize(FINANCIAL_DECIMAL_QUANTUM)
        stamp = (buy_turnover * self.stamp_rate).quantize(FINANCIAL_DECIMAL_QUANTUM)
        gst = ((brokerage + exchange + sebi) * self.gst_rate).quantize(FINANCIAL_DECIMAL_QUANTUM)
        total = (brokerage + stt + exchange + gst + sebi + stamp).quantize(
            FINANCIAL_DECIMAL_QUANTUM
        )
        return StatutoryCharges(
            brokerage=brokerage,
            stt=stt,
            exchange_txn_charges=exchange,
            gst=gst,
            sebi_charges=sebi,
            stamp_duty=stamp,
            total_charges=total,
        )


@dataclass(frozen=True)
class OptionSpreadAssumptions:
    adapter_version: str = SPREAD_EXECUTION_ADAPTER_VERSION
    pricing_quality: str = PRICING_QUALITY
    executable_quotes: bool = False
    synthetic: bool = False
    unit: str = INR_RESULT_UNIT
    financial_pnl_available: bool = True
    starting_capital: Decimal = Decimal("500000.00")
    lots: int = 1
    slippage_percent: Decimal = Decimal("5.00")
    stop_loss_percent: Decimal = Decimal("40.00")
    target_max_profit_percent: Decimal = Decimal("50.00")
    trend_spread_decline_percent: Decimal = Decimal("25.00")
    flat_round_trip_cost: Decimal = Decimal("400.00")
    maximum_daily_loss: Decimal = Decimal("10000.00")
    square_off_time: time = time(15, 0)
    monitoring_interval_minutes: int = 5
    primary_spread_width: int = 200
    fallback_spread_width: int = 250
    fallback_enabled: bool = True
    maximum_debit_percent: Decimal = Decimal("35.00")
    atm_increment: int = 50
    same_day_weekly_expiry: bool = True
    max_trades_per_day: int = 1
    use_futures_price: bool = False

    @classmethod
    def from_parameters(
        cls, parameters: BullCallSpreadSignalParameters, *, starting_capital: Decimal
    ) -> OptionSpreadAssumptions:
        return cls(
            starting_capital=starting_capital,
            lots=parameters.lots,
            slippage_percent=Decimal(str(parameters.slippage_percent)),
            stop_loss_percent=Decimal(str(parameters.stop_loss_percent)),
            target_max_profit_percent=Decimal(str(parameters.target_max_profit_percent)),
            trend_spread_decline_percent=Decimal(str(parameters.trend_spread_decline_percent)),
            flat_round_trip_cost=Decimal(str(parameters.flat_round_trip_cost)),
            maximum_daily_loss=Decimal(str(parameters.maximum_daily_loss)),
            square_off_time=parameters.hard_exit_time,
            monitoring_interval_minutes=parameters.monitoring_interval_minutes,
            primary_spread_width=parameters.primary_spread_width,
            fallback_spread_width=parameters.fallback_spread_width,
            fallback_enabled=parameters.fallback_enabled,
            maximum_debit_percent=Decimal(str(parameters.maximum_debit_percent)),
            atm_increment=parameters.atm_increment,
            same_day_weekly_expiry=parameters.same_day_weekly_expiry,
            max_trades_per_day=parameters.max_trades_per_day,
            use_futures_price=isinstance(parameters, BullCallSpreadFuturesParameters),
        )

    def snapshot(self) -> dict[str, object]:
        return {
            **({"vwap_comparison_price": "futures_close"} if self.use_futures_price else {}),
            "adapter_version": self.adapter_version,
            "pricing_quality": self.pricing_quality,
            "executable_quotes": self.executable_quotes,
            "synthetic": self.synthetic,
            "unit": self.unit,
            "financial_pnl_available": self.financial_pnl_available,
            "starting_capital": str(self.starting_capital),
            "lots": self.lots,
            "slippage_percent": str(self.slippage_percent),
            "stop_loss_percent": str(self.stop_loss_percent),
            "target_max_profit_percent": str(self.target_max_profit_percent),
            "trend_spread_decline_percent": str(self.trend_spread_decline_percent),
            "flat_round_trip_cost": str(self.flat_round_trip_cost),
            "maximum_daily_loss": str(self.maximum_daily_loss),
            "square_off_time": self.square_off_time.strftime("%H:%M:%S"),
            "monitoring_interval_minutes": self.monitoring_interval_minutes,
            "primary_spread_width": self.primary_spread_width,
            "fallback_spread_width": self.fallback_spread_width,
            "fallback_enabled": self.fallback_enabled,
            "maximum_debit_percent": str(self.maximum_debit_percent),
            "atm_increment": self.atm_increment,
            "same_day_weekly_expiry": self.same_day_weekly_expiry,
            "max_trades_per_day": self.max_trades_per_day,
            "limitations": [
                "Fills use completed option candle closes plus adverse percentage slippage.",
                "Historical executable bid/ask quotes, liquidity filters, and non-monthly events are unavailable.",
            ],
        }


@dataclass(frozen=True)
class OptionSpreadLegResult:
    contract_symbol: str
    expiry: str
    strike_price: Decimal
    option_type: str
    action: str
    entry_reference_close: Decimal
    exit_reference_close: Decimal
    entry_price: Decimal
    exit_price: Decimal
    lot_size: int
    quantity: int
    gross_pnl_inr: Decimal
    synthetic: bool = False
    unit: str = INR_RESULT_UNIT


@dataclass(frozen=True)
class OptionSpreadTradeResult:
    entry_signal_sequence: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    expiry: str
    spread_width_points: Decimal
    buy_leg: OptionSpreadLegResult
    sell_leg: OptionSpreadLegResult
    lot_size: int
    lots_traded: int
    total_quantity: int
    entry_net_debit_points: Decimal
    exit_net_credit_points: Decimal
    entry_net_debit_inr: Decimal
    exit_net_credit_inr: Decimal
    maximum_loss_inr: Decimal
    maximum_profit_inr: Decimal
    stop_level_points: Decimal
    target_level_points: Decimal
    trend_reversal_level_points: Decimal
    gross_pnl_inr: Decimal
    statutory_charges: StatutoryCharges
    net_pnl_inr: Decimal
    return_on_risk_pct: Decimal
    exit_reason: str
    pricing_quality: str = PRICING_QUALITY
    executable_quotes: bool = False
    synthetic: bool = False
    unit: str = INR_RESULT_UNIT
    financial_pnl_available: bool = True


@dataclass(frozen=True)
class OptionSpreadExecutionRejection:
    entry_signal_sequence: int
    timestamp: datetime
    reason: str
    details: dict[str, object]


@dataclass(frozen=True)
class OptionSpreadEquityPointResult:
    timestamp: datetime
    equity_inr: Decimal
    drawdown_inr: Decimal
    drawdown_pct: Decimal
    synthetic: bool = False
    unit: str = INR_RESULT_UNIT
    financial_pnl_available: bool = True


@dataclass(frozen=True)
class OptionSpreadExecutionResult:
    trades: tuple[OptionSpreadTradeResult, ...]
    equity_points: tuple[OptionSpreadEquityPointResult, ...]
    rejections: tuple[OptionSpreadExecutionRejection, ...] = ()

    def metrics(self, starting_capital: Decimal | None = None) -> dict[str, object]:
        capital = starting_capital or (
            self.equity_points[0].equity_inr if self.equity_points else Decimal("0.00")
        )
        outcomes = [trade.net_pnl_inr for trade in self.trades]
        net_profit = sum(outcomes, Decimal("0.00")).quantize(FINANCIAL_DECIMAL_QUANTUM)
        gross_profit = sum(
            (trade.gross_pnl_inr for trade in self.trades), Decimal("0.00")
        ).quantize(FINANCIAL_DECIMAL_QUANTUM)
        costs = sum(
            (trade.statutory_charges.total_charges for trade in self.trades),
            Decimal("0.00"),
        ).quantize(FINANCIAL_DECIMAL_QUANTUM)
        wins = sum(value > 0 for value in outcomes)
        losses = sum(value < 0 for value in outcomes)
        total = len(outcomes)
        max_drawdown = min(
            (point.drawdown_inr for point in self.equity_points),
            default=Decimal("0.00"),
        )
        max_drawdown_pct = min(
            (point.drawdown_pct for point in self.equity_points),
            default=Decimal("0.00"),
        )
        ending_equity = (capital + net_profit).quantize(FINANCIAL_DECIMAL_QUANTUM)
        return_pct = (
            (net_profit / capital * Decimal("100")).quantize(Decimal("0.01"))
            if capital
            else Decimal("0.00")
        )
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": str(
                (Decimal(wins) / Decimal(total) * Decimal("100")).quantize(Decimal("0.01"))
                if total
                else Decimal("0.00")
            ),
            "gross_profit_inr": str(gross_profit),
            "total_charges_inr": str(costs),
            "net_profit_inr": str(net_profit),
            "starting_capital_inr": str(capital),
            "ending_equity_inr": str(ending_equity),
            "return_pct": str(return_pct),
            "max_drawdown_inr": str(max_drawdown),
            "max_drawdown_pct": str(max_drawdown_pct),
            "execution_rejections": len(self.rejections),
            "execution_rejections_by_reason": dict(
                Counter(item.reason for item in self.rejections)
            ),
            "pricing_quality": PRICING_QUALITY,
            "pricing_model_version": SPREAD_EXECUTION_ADAPTER_VERSION,
            "executable_quotes": False,
            "unit": INR_RESULT_UNIT,
            "financial_pnl_available": True,
            "synthetic": False,
            "limitations": [
                "Estimated historical option fills use candle close plus adverse slippage.",
                "These results are not executable bid/ask performance.",
            ],
        }
