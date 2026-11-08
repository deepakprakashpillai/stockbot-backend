"""Persist completed option trades, equity, and linked execution evidence."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.db.models.backtests import BacktestRun, EquityPoint, SignalEvaluation, Trade, TradeLeg
from app.domain.algorithms import AlgorithmDefinition
from app.domain.backtests import SignalDecision
from app.domain.option_spread_execution import OptionSpreadAssumptions
from app.domain.option_spread_models import OptionSpreadExecutionResult
from app.domain.signal_diagnostics import build_signal_diagnostic

if TYPE_CHECKING:
    from app.application.backtests import BacktestService


def persist_option_results(
    service: BacktestService,
    run: BacktestRun,
    execution: OptionSpreadExecutionResult,
    assumptions: OptionSpreadAssumptions,
    algorithm: AlgorithmDefinition,
    snapshot_algorithm: dict[str, Any],
    parameters: dict[str, Any],
    market_ordered_journal: bool,
    accepted_count: int,
    processed: int,
    starting_capital: Decimal,
    metrics: dict[str, object],
    terminal_events: list[Any],
) -> None:
    assert execution is not None
    scalar = getattr(service.session, "scalar", None)
    if callable(scalar):
        for rejection in execution.rejections:
            evaluation = scalar(
                select(SignalEvaluation).where(
                    SignalEvaluation.run_id == run.id,
                    SignalEvaluation.sequence == rejection.entry_signal_sequence,
                )
            )
            if evaluation is None:
                raise ValueError("An option execution rejection lost its signal linkage.")
            if market_ordered_journal:
                continue
            evaluation.decision = "REJECTED"
            evaluation.rejection_reason = rejection.reason
            evaluation.inputs = {
                **evaluation.inputs,
                "execution_rejection": rejection.details,
            }
            evaluation.diagnostic = build_signal_diagnostic(
                SignalDecision(
                    timestamp=evaluation.timestamp,
                    session_date=evaluation.session_date,
                    decision="REJECTED",
                    rejection_reason=rejection.reason,
                    window=evaluation.window,
                    score=evaluation.score,
                    inputs=evaluation.inputs,
                ),
                definition=algorithm,
                parameter_version_id=str(snapshot_algorithm["version_id"]),
                parameter_snapshot=parameters,
            )
    service.session.query(EquityPoint).filter(EquityPoint.run_id == run.id).delete()
    service.session.query(Trade).filter(Trade.run_id == run.id).delete()
    persisted_trades = []
    for sequence, item in enumerate(execution.trades, start=1):
        scalar = getattr(service.session, "scalar", None)
        entry_signal_id = (
            scalar(
                select(SignalEvaluation.id).where(
                    SignalEvaluation.run_id == run.id,
                    SignalEvaluation.sequence == item.entry_signal_sequence,
                )
            )
            if callable(scalar)
            else None
        )
        trade = Trade(
            run_id=run.id,
            sequence=sequence,
            entry_signal_evaluation_id=entry_signal_id,
            entry_timestamp=item.entry_timestamp,
            exit_timestamp=item.exit_timestamp,
            exit_reason=item.exit_reason,
            quantity=Decimal(item.total_quantity),
            entry_reference=item.entry_net_debit_points,
            exit_reference=item.exit_net_credit_points,
            gross_demo_units=item.gross_pnl_inr,
            costs_demo_units=item.statutory_charges.total_charges,
            net_demo_units=item.net_pnl_inr,
            synthetic=False,
            unit="INR",
            financial_pnl_available=True,
            assumptions={
                **assumptions.snapshot(),
                "expiry": item.expiry,
                "lot_size": item.lot_size,
                "lot_size_source": "selected_option_contract_metadata",
                "lots_traded": item.lots_traded,
                "total_quantity": item.total_quantity,
                "spread_width_points": str(item.spread_width_points),
                "entry_net_debit_inr": str(item.entry_net_debit_inr),
                "exit_net_credit_inr": str(item.exit_net_credit_inr),
                "maximum_loss_inr": str(item.maximum_loss_inr),
                "maximum_profit_inr": str(item.maximum_profit_inr),
                "stop_level_points": str(item.stop_level_points),
                "target_level_points": str(item.target_level_points),
                "trend_reversal_level_points": str(item.trend_reversal_level_points),
                "return_on_risk_pct": str(item.return_on_risk_pct),
                "buy_leg": {
                    "entry_reference_close": str(item.buy_leg.entry_reference_close),
                    "exit_reference_close": str(item.buy_leg.exit_reference_close),
                },
                "sell_leg": {
                    "entry_reference_close": str(item.sell_leg.entry_reference_close),
                    "exit_reference_close": str(item.sell_leg.exit_reference_close),
                },
            },
            legs=[
                TradeLeg(
                    leg_number=1,
                    name=item.buy_leg.contract_symbol,
                    entry_reference=item.buy_leg.entry_price,
                    exit_reference=item.buy_leg.exit_price,
                    demo_units=item.buy_leg.gross_pnl_inr,
                    synthetic=False,
                    unit="INR",
                    financial_pnl_available=True,
                ),
                TradeLeg(
                    leg_number=2,
                    name=item.sell_leg.contract_symbol,
                    entry_reference=item.sell_leg.entry_price,
                    exit_reference=item.sell_leg.exit_price,
                    demo_units=item.sell_leg.gross_pnl_inr,
                    synthetic=False,
                    unit="INR",
                    financial_pnl_available=True,
                ),
            ],
        )
        persisted_trades.append(trade)
    service.session.add_all(persisted_trades)
    flush = getattr(service.session, "flush", None)
    if callable(flush):
        flush()
    terminal_events.extend(
        service.runs.append_event(
            run.id,
            "BACKTEST_TRADE_CREATED",
            {
                "trade_id": str(item.id),
                "trade_sequence": item.sequence,
                "mode": "OPTION_SPREAD_SIMULATION",
                "synthetic": False,
                "unit": "INR",
                "financial_pnl_available": True,
                "pricing_quality": assumptions.pricing_quality,
                "executable_quotes": False,
            },
        )
        for item in persisted_trades
    )
    service.session.add_all(
        [
            EquityPoint(
                run_id=run.id,
                sequence=sequence,
                timestamp=item.timestamp,
                equity_demo_units=item.equity_inr,
                drawdown_demo_units=item.drawdown_inr,
                synthetic=False,
                unit="INR",
                financial_pnl_available=True,
            )
            for sequence, item in enumerate(execution.equity_points, start=1)
        ]
    )
    metrics.update(execution.metrics(starting_capital=starting_capital))
    metrics["accepted_signals"] = (
        accepted_count if market_ordered_journal else len(execution.trades)
    )
    if market_ordered_journal:
        metrics["opened_spreads"] = len(execution.trades)
        metrics["execution_rejections"] = len(execution.rejections)
    metrics["rejected_evaluations"] = (
        processed - accepted_count if market_ordered_journal else processed - len(execution.trades)
    )
