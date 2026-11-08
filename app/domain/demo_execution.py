from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.backtests import SignalDecision

DEMO_EXECUTION_ADAPTER_VERSION = "1.0.0-platform-demo"
DEMO_RESULT_UNIT = "DEMO_UNITS"
DEMO_DECIMAL_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class DemoExecutionAssumptions:
    adapter_version: str = DEMO_EXECUTION_ADAPTER_VERSION
    synthetic: bool = True
    unit: str = DEMO_RESULT_UNIT
    financial_pnl_available: bool = False
    quantity: Decimal = Decimal("1")
    costs_demo_units: Decimal = Decimal("0")
    exit_rule: str = "DEMO_HOLD_INTERVAL_COMPLETE"

    def __post_init__(self) -> None:
        if (
            self.adapter_version != DEMO_EXECUTION_ADAPTER_VERSION
            or not self.synthetic
            or self.unit != DEMO_RESULT_UNIT
            or self.financial_pnl_available
            or self.quantity != Decimal("1")
            or self.costs_demo_units != Decimal("0")
            or self.exit_rule != "DEMO_HOLD_INTERVAL_COMPLETE"
        ):
            raise ValueError("Platform-demo execution safeguards cannot be changed.")

    def snapshot(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "synthetic": self.synthetic,
            "unit": self.unit,
            "financial_pnl_available": self.financial_pnl_available,
            "quantity": str(self.quantity),
            "costs_demo_units": str(self.costs_demo_units),
            "exit_rule": self.exit_rule,
        }


@dataclass(frozen=True)
class DemoLegResult:
    name: str
    demo_units: Decimal
    synthetic: bool = True
    unit: str = DEMO_RESULT_UNIT


@dataclass(frozen=True)
class DemoTradeResult:
    entry_signal_sequence: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_reference: Decimal
    exit_reference: Decimal
    quantity: Decimal
    gross_demo_units: Decimal
    costs_demo_units: Decimal
    net_demo_units: Decimal
    exit_reason: str
    legs: tuple[DemoLegResult, DemoLegResult]
    synthetic: bool = True
    unit: str = DEMO_RESULT_UNIT
    financial_pnl_available: bool = False


@dataclass(frozen=True)
class DemoEquityPointResult:
    timestamp: datetime
    equity_demo_units: Decimal
    drawdown_demo_units: Decimal
    synthetic: bool = True
    unit: str = DEMO_RESULT_UNIT
    financial_pnl_available: bool = False


@dataclass(frozen=True)
class DemoExecutionResult:
    trades: tuple[DemoTradeResult, ...]
    equity_points: tuple[DemoEquityPointResult, ...]

    def metrics(self) -> dict[str, object]:
        outcomes = [trade.net_demo_units for trade in self.trades]
        total = len(outcomes)
        expectancy = (
            (sum(outcomes, Decimal("0")) / total).quantize(DEMO_DECIMAL_QUANTUM)
            if total
            else Decimal("0").quantize(DEMO_DECIMAL_QUANTUM)
        )
        max_drawdown = min(
            (point.drawdown_demo_units for point in self.equity_points),
            default=Decimal("0").quantize(DEMO_DECIMAL_QUANTUM),
        )
        return {
            "total_trades": total,
            "wins": sum(outcome > 0 for outcome in outcomes),
            "losses": sum(outcome < 0 for outcome in outcomes),
            "expectancy_demo_units": str(expectancy),
            "max_drawdown_demo_units": str(max_drawdown),
            "synthetic": True,
            "unit": DEMO_RESULT_UNIT,
            "financial_pnl_available": False,
        }


class ExecutionAdapter(Protocol):
    version: str

    def execute(
        self, decisions: Sequence[SignalDecision], *, hold_candles: int
    ) -> DemoExecutionResult: ...


class PlatformDemoExecutionAdapter:
    """Deterministic plumbing fixture; it does not simulate market execution."""

    version = DEMO_EXECUTION_ADAPTER_VERSION

    def __init__(self, assumptions: DemoExecutionAssumptions | None = None) -> None:
        self.assumptions = assumptions or DemoExecutionAssumptions()

    @staticmethod
    def _reference(decision: SignalDecision) -> Decimal:
        value = decision.inputs.get("close")
        if not isinstance(value, int | float | str | Decimal) or isinstance(value, bool):
            raise ValueError("Demo execution requires a numeric close reference.")
        reference = Decimal(str(value))
        if not reference.is_finite():
            raise ValueError("Demo execution requires a finite close reference.")
        return reference.quantize(DEMO_DECIMAL_QUANTUM)

    def execute(
        self, decisions: Sequence[SignalDecision], *, hold_candles: int
    ) -> DemoExecutionResult:
        state = PlatformDemoExecutionState(self.assumptions, hold_candles=hold_candles)
        for decision in decisions:
            state.consume(decision)
        return state.result()


class PlatformDemoExecutionState:
    """Incremental, deterministic PLATFORM_DEMO execution state.

    The worker uses this while streaming candles so a closed synthetic trade can
    be persisted immediately.  ``result`` intentionally has the same shape as
    the legacy batch adapter, which keeps historical fixtures reproducible.
    """

    def __init__(
        self,
        assumptions: DemoExecutionAssumptions | None = None,
        *,
        hold_candles: int,
    ) -> None:
        if hold_candles < 0:
            raise ValueError("Demo hold_candles cannot be negative.")
        self.assumptions = assumptions or DemoExecutionAssumptions()
        self.hold_candles = hold_candles
        trades: list[DemoTradeResult] = []
        self.trades = trades
        self.equity_points: list[DemoEquityPointResult] = []
        self._index = 0
        self._next_available_index = 0
        self._pending: tuple[int, SignalDecision] | None = None
        self._last_timestamp: datetime | None = None
        self._equity = Decimal("0")
        self._running_peak = Decimal("0")

    @staticmethod
    def _reference(decision: SignalDecision) -> Decimal:
        return PlatformDemoExecutionAdapter._reference(decision)

    def _close_trade(
        self, entry_sequence: int, entry: SignalDecision, exit_decision: SignalDecision
    ) -> DemoTradeResult:
        entry_reference = self._reference(entry)
        exit_reference = self._reference(exit_decision)
        gross = ((exit_reference - entry_reference) * self.assumptions.quantity).quantize(
            DEMO_DECIMAL_QUANTUM
        )
        net = (gross - self.assumptions.costs_demo_units).quantize(DEMO_DECIMAL_QUANTUM)
        first_leg = (net / Decimal("2")).quantize(DEMO_DECIMAL_QUANTUM)
        return DemoTradeResult(
            entry_signal_sequence=entry_sequence,
            entry_timestamp=entry.timestamp,
            exit_timestamp=exit_decision.timestamp,
            entry_reference=entry_reference,
            exit_reference=exit_reference,
            quantity=self.assumptions.quantity,
            gross_demo_units=gross,
            costs_demo_units=self.assumptions.costs_demo_units,
            net_demo_units=net,
            exit_reason=self.assumptions.exit_rule,
            legs=(
                DemoLegResult("DEMO_REFERENCE_A", first_leg),
                DemoLegResult("DEMO_REFERENCE_B", net - first_leg),
            ),
        )

    def _record_trade(self, trade: DemoTradeResult) -> DemoTradeResult:
        self.trades.append(trade)
        self._equity = (self._equity + trade.net_demo_units).quantize(DEMO_DECIMAL_QUANTUM)
        self._running_peak = max(self._running_peak, self._equity)
        self.equity_points.append(
            DemoEquityPointResult(
                timestamp=trade.exit_timestamp,
                equity_demo_units=self._equity,
                drawdown_demo_units=self._equity - self._running_peak,
            )
        )
        return trade

    def consume(self, decision: SignalDecision) -> DemoTradeResult | None:
        if self._last_timestamp is not None and decision.timestamp <= self._last_timestamp:
            raise ValueError("Demo execution decisions must be strictly chronological.")
        self._last_timestamp = decision.timestamp
        index = self._index
        self._index += 1

        if self._pending and index == self._pending[0] + self.hold_candles:
            entry_index, entry = self._pending
            self._pending = None
            if decision.session_date == entry.session_date:
                self._next_available_index = index + 1
                return self._record_trade(self._close_trade(entry_index + 1, entry, decision))

        # A pending entry owns its complete holding window.  Accepted decisions
        # inside that window are deliberately ignored, matching the batch adapter.
        if self._pending is not None:
            return None

        if index < self._next_available_index or decision.decision != "ACCEPTED":
            return None
        if self.hold_candles == 0:
            self._next_available_index = index + 1
            return self._record_trade(self._close_trade(index + 1, decision, decision))
        self._pending = (index, decision)
        return None

    def result(self) -> DemoExecutionResult:
        return DemoExecutionResult(tuple(self.trades), tuple(self.equity_points))
