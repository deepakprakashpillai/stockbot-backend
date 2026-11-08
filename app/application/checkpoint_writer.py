"""Persist bounded signal checkpoints and deterministic demo result rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.backtests import EquityPoint, SignalEvaluation, Trade, TradeLeg
from app.db.repositories.backtests import BacktestRepository
from app.domain.algorithms import AlgorithmDefinition
from app.domain.backtests import SignalDecision
from app.domain.demo_execution import DemoExecutionAssumptions, PlatformDemoExecutionState
from app.domain.signal_diagnostics import build_signal_diagnostic


def persist_signal_checkpoint(
    session: Session,
    *,
    run_id: UUID,
    first_sequence: int,
    decisions: list[SignalDecision],
    algorithm: AlgorithmDefinition,
    parameter_version_id: str,
    parameters: dict[str, Any],
) -> None:
    session.add_all(
        [
            SignalEvaluation(
                run_id=run_id,
                sequence=first_sequence + offset,
                timestamp=item.timestamp,
                session_date=item.session_date,
                decision=item.decision,
                rejection_reason=item.rejection_reason,
                window=item.window,
                score=item.score,
                inputs=item.inputs,
                diagnostic=build_signal_diagnostic(
                    item,
                    definition=algorithm,
                    parameter_version_id=parameter_version_id,
                    parameter_snapshot=parameters,
                ),
            )
            for offset, item in enumerate(decisions)
        ]
    )
    # Trade linkage resolves the exact saved evaluation sequence, never its timestamp.
    flush = getattr(session, "flush", None)
    if callable(flush):
        flush()


def persist_demo_trades(
    session: Session,
    runs: BacktestRepository,
    *,
    run_id: UUID,
    state: PlatformDemoExecutionState,
    persisted_count: int,
) -> tuple[int, list[object]]:
    complete = state.result()
    events: list[object] = []
    for sequence, demo_trade in enumerate(
        complete.trades[persisted_count:], start=persisted_count + 1
    ):
        scalar = getattr(session, "scalar", None)
        entry_signal_id = (
            scalar(
                select(SignalEvaluation.id).where(
                    SignalEvaluation.run_id == run_id,
                    SignalEvaluation.sequence == demo_trade.entry_signal_sequence,
                )
            )
            if callable(scalar)
            else None
        )
        if callable(scalar) and entry_signal_id is None:
            raise ValueError("The deterministic demo trade entry signal was not persisted.")
        trade = Trade(
            run_id=run_id,
            sequence=sequence,
            entry_signal_evaluation_id=entry_signal_id,
            entry_timestamp=demo_trade.entry_timestamp,
            exit_timestamp=demo_trade.exit_timestamp,
            exit_reason=demo_trade.exit_reason,
            quantity=demo_trade.quantity,
            entry_reference=demo_trade.entry_reference,
            exit_reference=demo_trade.exit_reference,
            gross_demo_units=demo_trade.gross_demo_units,
            costs_demo_units=demo_trade.costs_demo_units,
            net_demo_units=demo_trade.net_demo_units,
            synthetic=True,
            unit="DEMO_UNITS",
            financial_pnl_available=False,
            assumptions=DemoExecutionAssumptions().snapshot(),
            legs=[
                TradeLeg(
                    leg_number=leg_number,
                    name=leg.name,
                    entry_reference=demo_trade.entry_reference,
                    exit_reference=demo_trade.exit_reference,
                    demo_units=leg.demo_units,
                    synthetic=True,
                    unit="DEMO_UNITS",
                    financial_pnl_available=False,
                )
                for leg_number, leg in enumerate(demo_trade.legs, start=1)
            ],
        )
        session.add_all([trade])
        point = complete.equity_points[sequence - 1]
        session.add_all(
            [
                EquityPoint(
                    run_id=run_id,
                    sequence=sequence,
                    timestamp=point.timestamp,
                    equity_demo_units=point.equity_demo_units,
                    drawdown_demo_units=point.drawdown_demo_units,
                    synthetic=True,
                    unit="DEMO_UNITS",
                    financial_pnl_available=False,
                )
            ]
        )
        events.append(
            runs.append_event(
                run_id,
                "BACKTEST_TRADE_CREATED",
                {
                    "trade_sequence": sequence,
                    "mode": "PLATFORM_DEMO",
                    "synthetic": True,
                    "unit": "DEMO_UNITS",
                    "financial_pnl_available": False,
                },
            )
        )
    return len(complete.trades), events
