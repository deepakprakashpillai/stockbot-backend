"""Reads project saved evidence only; no strategy rules are reconstructed."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.model2.execution import MODE
from app.application.model2.groups import Model2ServiceError
from app.db.models.backtests import BacktestRun, RunMetrics
from app.db.models.model2 import BacktestCycle


def get_run(session: Session, run_id: UUID) -> BacktestRun:
    run = session.get(BacktestRun, run_id)
    if run is None:
        raise Model2ServiceError("BACKTEST_NOT_FOUND", "The requested run does not exist.", 404)
    if run.mode != MODE:
        raise Model2ServiceError(
            "MODEL2_REPRESENTATION_REQUIRED",
            "This endpoint is for Model 2 four-leg portfolio results.",
            409,
        )
    return run


def position(row: BacktestCycle) -> dict[str, Any] | None:
    payload = row.payload
    raw = payload.get("result") or {}
    entry = raw.get("entry") or payload.get("entry")
    if entry is None:
        return None
    value = raw.get("valuation")
    exits = value.get("exit_fill_prices", []) if value else []
    legs = []
    for index, leg in enumerate(entry["legs"]):
        contract = leg["contract"]
        legs.append(
            {
                "role": leg["role"],
                "contract_id": contract["contract_id"],
                "symbol": contract["symbol"],
                "option_type": contract["option_type"],
                "strike": contract["strike"],
                "expiry": contract["expiry"],
                "lot_size": contract["lot_size"],
                "tick_size": contract["tick_size"],
                "quantity": leg["quantity"],
                "side": leg["side"],
                "entry_reference_price": leg["reference_price"],
                "entry_fill_price": leg["fill_price"],
                "exit_fill_price": exits[index] if exits else None,
                "gross_pnl_inr": value["leg_gross_pnl_inr"][index] if value else None,
            }
        )
    return {
        "id": row.id,
        "trade_id": row.trade_id,
        "state": row.state,
        "expiry": row.expiry,
        "entry_at": entry["execution_at"],
        "exit_at": raw.get("exit_at"),
        "finalized_at": raw.get("finalized_at"),
        "reason": row.reason,
        "original_trigger": raw.get("trigger") or payload.get("trigger"),
        "entry_credit_points": entry["credit_points"],
        "maximum_loss_inr": entry["maximum_loss_inr"],
        "assumed_margin_inr": entry["assumed_margin_inr"],
        "charges_inr": entry["charges_inr"],
        "gross_pnl_inr": value["gross_pnl_inr"] if value else None,
        "net_pnl_inr": value["net_pnl_inr"] if value else None,
        "priced_performance_included": value is not None,
        "flags": raw.get("flags", []),
        "legs": legs,
    }


def cycle_page(
    session: Session, run: BacktestRun, cursor: date | None, limit: int, *, positions: bool = False
) -> tuple[list[BacktestCycle], date | None]:
    if run.active_attempt_id is None:
        return [], None
    query = select(BacktestCycle).where(
        BacktestCycle.run_id == run.id, BacktestCycle.attempt_id == run.active_attempt_id
    )
    if cursor:
        query = query.where(BacktestCycle.expiry > cursor)
    if positions:
        query = query.where(BacktestCycle.payload["entry"].astext.is_not(None))
    rows = list(session.scalars(query.order_by(BacktestCycle.expiry).limit(limit + 1)))
    return rows[:limit], rows[limit - 1].expiry if len(rows) > limit else None


def assessment_data(session: Session, run_id: UUID) -> dict[str, Any]:
    from app.db.models.backtests import Trade
    from app.domain.model2.performance import (
        PERFORMANCE_POLICY,
        PricedOutcome,
        assess_performance,
        priced_statistics,
    )

    run = get_run(session, run_id)
    metrics = session.get(RunMetrics, run_id)
    summary = metrics.metrics.get("model2_summary") if metrics else None
    outcomes = tuple(
        PricedOutcome(
            trade.exit_timestamp,
            trade.gross_demo_units,
            trade.costs_demo_units,
            trade.net_demo_units,
        )
        for trade in session.scalars(
            select(Trade)
            .where(Trade.run_id == run_id)
            .order_by(Trade.exit_timestamp, Trade.sequence)
        )
    )
    statistics = priced_statistics(outcomes, run.start_date, run.end_date, run.starting_capital)
    reliability = summary.get("reliability") if summary else None
    scope = summary.get("scope", "INCOMPLETE") if summary else "INCOMPLETE"
    overall, criteria = assess_performance(
        statistics,
        scope=scope if run.status == "COMPLETED" else "INCOMPLETE",
        reliability=reliability or {},
        slippage=Decimal(run.snapshot["model2_request"]["parameters"]["slippage_percent"]),
        capital=run.starting_capital,
    )
    return {
        "run_id": str(run_id),
        "policy_version": PERFORMANCE_POLICY,
        "run_status": run.status,
        "scope": scope,
        "reliability": reliability,
        "overall_status": overall,
        "criteria": criteria,
        "summary": summary,
        "priced_statistics": statistics,
        "limitations": [
            "Priced cycles only; excluded risk assumptions are never financial returns.",
            "OHLC opening estimates, assumed margin and placeholder fees.",
            "Development and known-exposed later dates remain separate; no clean holdout is inferred.",
            "Control protocol, sufficient real history, paper and live approval remain separate.",
        ],
    }
