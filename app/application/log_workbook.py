"""Readable audit workbooks from persisted evidence only; never replay trading rules."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from decimal import Decimal
from heapq import merge
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import xlsxwriter

IST = ZoneInfo("Asia/Kolkata")
MAX_DATA_ROWS = 1_048_575
MISSING = object()


def flatten(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Keep every leaf, including nulls and empty collections, with its source path."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from flatten(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)) and value:
        for index, item in enumerate(value):
            yield from flatten(item, f"{path}[{index}]")
    else:
        yield path, value


def local_time(value: Any) -> Any:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # A missing timezone must not silently become machine-local time.
            return f"{value.isoformat()} (timezone unavailable)"
        return value.astimezone(IST).replace(tzinfo=None)
    return value


def build_log_workbook(
    destination: Path,
    *,
    summary: dict[str, Any],
    attempt_id: Any,
    signals: Iterable[dict[str, Any]],
    trades: Iterable[dict[str, Any]],
    journal: Iterable[dict[str, Any]],
    days: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    equity: Iterable[dict[str, Any]],
    analytics: Iterable[dict[str, Any]],
    signal_count: int | None = None,
    trade_count: int | None = None,
    journal_count: int | None = None,
    day_count: int | None = None,
    event_count: int | None = None,
    equity_count: int | None = None,
    analytics_count: int | None = None,
) -> dict[str, int]:
    """Use bounded worksheet memory and split long sheets instead of truncating."""
    if signal_count is None:
        if not isinstance(signals, list):
            raise ValueError("A streamed signal source needs its saved row count.")
        signal_count = len(signals)
    if trade_count is None:
        if not isinstance(trades, list):
            raise ValueError("A streamed trade source needs its saved row count.")
        trade_count = len(trades)
    if journal_count is None:
        if not isinstance(journal, list):
            raise ValueError("A streamed journal source needs its saved row count.")
        journal_count = len(journal)
    if day_count is None:
        if not isinstance(days, list):
            raise ValueError("A streamed day source needs its saved row count.")
        day_count = len(days)
    for label, value, count in (
        ("event", events, event_count),
        ("equity", equity, equity_count),
        ("analytics", analytics, analytics_count),
    ):
        if count is None and not isinstance(value, list):
            raise ValueError(f"A streamed {label} source needs its saved row count.")
    event_count = len(cast(list[dict[str, Any]], events)) if event_count is None else event_count
    equity_count = len(cast(list[dict[str, Any]], equity)) if equity_count is None else equity_count
    analytics_count = (
        len(cast(list[dict[str, Any]], analytics)) if analytics_count is None else analytics_count
    )
    counts: dict[str, int] = {}
    with xlsxwriter.Workbook(
        destination,
        {"constant_memory": True, "strings_to_formulas": False, "strings_to_urls": False},
    ) as book:
        header = book.add_format(
            {"bold": True, "bg_color": "#50308C", "font_color": "white", "text_wrap": True}
        )
        text_format = book.add_format({"valign": "top"})
        date_format = book.add_format({"num_format": "yyyy-mm-dd"})
        timestamp_format = book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        number_format = book.add_format({"num_format": "0.###############"})
        failed = book.add_format({"font_color": "#B42318", "bg_color": "#FEF3F2"})

        def write_sheet(name: str, columns: list[str], rows: Iterable[list[Any]]) -> None:
            part, total, row_index = 0, 0, 1
            sheet: Any = None

            def open_part() -> Any:
                title = name if part == 0 else f"{name} {part + 1}"
                ws = book.add_worksheet(title)
                ws.freeze_panes(1, min(2, len(columns)))
                ws.set_default_row(20)
                ws.set_row(0, 34)
                ws.set_column(0, len(columns) - 1, 24, text_format)
                for col, label in enumerate(columns):
                    if label in {
                        "Explanation",
                        "Field",
                        "Condition",
                        "Value",
                        "Reason",
                        "Exact value",
                        "Evidence availability",
                    }:
                        ws.set_column(col, col, 58)
                    ws.write_string(0, col, label, header)
                ws.repeat_rows(0)
                return ws

            sheet = open_part()
            for row in rows:
                if row_index > MAX_DATA_ROWS:
                    sheet.autofilter(0, 0, row_index - 1, len(columns) - 1)
                    part += 1
                    row_index = 1
                    sheet = open_part()
                for col, value in enumerate(row):
                    if value is MISSING:
                        continue
                    if value is None:
                        result = sheet.write_string(row_index, col, "Not recorded")
                    elif isinstance(value, datetime):
                        result = sheet.write_datetime(
                            row_index,
                            col,
                            local_time(value) if value.tzinfo else value,
                            timestamp_format,
                        )
                    elif isinstance(value, date):
                        result = sheet.write_datetime(row_index, col, value, date_format)
                    elif isinstance(value, bool):
                        result = sheet.write_boolean(row_index, col, value)
                    elif (
                        isinstance(value, (int, float, Decimal))
                        and len(str(value).replace(".", "").replace("-", "")) <= 15
                    ):
                        result = sheet.write_number(row_index, col, float(value), number_format)
                    else:
                        string = str(value)
                        # No truncation or formula interpretation of untrusted labels/evidence.
                        if len(string) > 32767:
                            raise ValueError(
                                "A log value exceeds Excel's cell limit; use the complete ZIP export."
                            )
                        result = sheet.write_string(row_index, col, string)
                    if result != 0:
                        raise ValueError(
                            f"Could not write {name} row {total + 1}, column {col + 1}."
                        )
                row_index += 1
                total += 1
            sheet.autofilter(0, 0, max(1, row_index - 1), len(columns) - 1)
            if "Result" in columns:
                col = columns.index("Result")
                sheet.conditional_format(
                    1,
                    col,
                    max(1, row_index - 1),
                    col,
                    {"type": "text", "criteria": "containing", "value": "failed", "format": failed},
                )
            counts[name] = total

        linked = {str(t["entry_signal_evaluation_id"]): t["sequence"] for t in trades}
        by_day: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"total": 0, "accepted": 0, "rejected": 0, "trades": 0, "reasons": Counter()}
        )
        for signal in signals:
            day = by_day[str(signal["session_date"])]
            day["total"] += 1
            day["accepted"] += signal["decision"] == "ACCEPTED"
            day["rejected"] += signal["decision"] == "REJECTED"
            day["trades"] += str(signal.get("id")) in linked
            if signal.get("rejection_reason"):
                day["reasons"][str(signal["rejection_reason"])] += 1
        source_days = {str(day["session_date"]): day for day in days}
        day_keys = sorted(set(by_day) | set(source_days))

        def daily_rows() -> Iterator[list[Any]]:
            for day in day_keys:
                entries = by_day.get(
                    day,
                    {"total": 0, "accepted": 0, "rejected": 0, "trades": 0, "reasons": Counter()},
                )
                saved = source_days.get(day, {})
                reasons = entries["reasons"]
                yield [
                    date.fromisoformat(day),
                    saved.get("included"),
                    saved.get("classification", "Classification not recorded"),
                    saved.get("primary_reason"),
                    entries["total"],
                    entries["accepted"],
                    entries["rejected"],
                    entries["trades"],
                    "; ".join(f"{key}: {value}" for key, value in reasons.most_common())
                    or "None recorded",
                ]

        write_sheet(
            "Days",
            [
                "Date",
                "Included",
                "Day outcome",
                "Reason",
                "Evaluations",
                "Accepted signals",
                "Rejected signals",
                "Trades opened",
                "Rejection counts",
            ],
            daily_rows(),
        )

        def decision_rows() -> Iterator[list[Any]]:
            for signal in signals:
                inputs = signal.get("inputs") or {}
                diagnostic = signal.get("diagnostic") or {}
                trade_sequence = linked.get(str(signal.get("id")))
                availability = (
                    "Individual checks recorded"
                    if diagnostic.get("evidence_version") == "bull-call-spread-checks-v2"
                    else "Grouped checks only; some comparisons were not recorded"
                    if diagnostic
                    else "Legacy: individual checks were not recorded"
                )
                yield [
                    signal["session_date"],
                    local_time(signal["timestamp"]),
                    signal["sequence"],
                    trade_sequence if trade_sequence is not None else "No linked trade",
                    signal["decision"],
                    signal.get("rejection_reason")
                    or (
                        "Trade opened"
                        if trade_sequence is not None
                        else "Signal accepted; no linked trade recorded"
                    ),
                    signal.get("window"),
                    MISSING if signal.get("score") is None else signal.get("score"),
                    inputs.get("close"),
                    inputs.get("previous_session_high"),
                    inputs.get("futures_vwap"),
                    inputs.get("rsi"),
                    inputs.get("india_vix"),
                    inputs.get("previous_india_vix"),
                    inputs.get("vix_decline_percent"),
                    availability,
                ]

        write_sheet(
            "Decisions",
            [
                "Date",
                "Time (IST)",
                "Decision #",
                "Trade #",
                "Decision",
                "Reason",
                "Entry window",
                "Score",
                "Spot close",
                "Previous session high",
                "Futures VWAP",
                "RSI",
                "India VIX",
                "Previous VIX close",
                "VIX decline %",
                "Evidence availability",
            ],
            decision_rows(),
        )

        def checks() -> Iterator[list[Any]]:
            for signal in signals:
                diagnostic = signal.get("diagnostic") or {}
                steps = diagnostic.get("evaluation_steps") or []
                if not steps:
                    yield [
                        signal["session_date"],
                        local_time(signal["timestamp"]),
                        signal["sequence"],
                        None,
                        "Individual checks not recorded",
                        "unavailable",
                        None,
                        None,
                        None,
                        None,
                        signal.get("rejection_reason"),
                    ]
                for step in steps:
                    yield [
                        signal["session_date"],
                        local_time(signal["timestamp"]),
                        signal["sequence"],
                        step.get("order"),
                        step.get("label"),
                        step.get("status"),
                        step.get("observed_value"),
                        step.get("comparator"),
                        step.get("threshold_value"),
                        step.get("score_contribution"),
                        step.get("reason_code"),
                    ]

        write_sheet(
            "Checks",
            [
                "Date",
                "Time (IST)",
                "Decision #",
                "Check order",
                "Condition",
                "Result",
                "Observed value",
                "Operator",
                "Threshold",
                "Points contributed",
                "Reason",
            ],
            checks(),
        )

        def spread_attempts() -> Iterator[list[Any]]:
            signal_sources = (
                (
                    s["session_date"],
                    s["timestamp"],
                    s["sequence"],
                    (s.get("inputs") or {}).get("execution_rejection", {}).get("attempts", []),
                )
                for s in signals
            )
            journal_sources = (
                (
                    e["session_date"],
                    e["timestamp"],
                    e.get("signal_sequence"),
                    e["payload"].get("spread_attempts", []),
                )
                for e in journal
                if e["event_type"] == "POSITION_OPENED"
            )
            sources = merge(signal_sources, journal_sources, key=lambda row: str(row[1]))
            for day, timestamp, sequence, attempts in sources:
                for index, attempt in enumerate(attempts, start=1):
                    cap_check: dict[str, Any] = next(
                        (
                            c
                            for c in attempt.get("checks", [])
                            if c.get("logic_step_id") == "debit_cap"
                        ),
                        {},
                    )
                    yield [
                        day,
                        local_time(timestamp),
                        sequence,
                        index,
                        attempt.get("buy_strike"),
                        attempt.get("sell_strike"),
                        attempt.get("width"),
                        attempt.get("buy_reference_close"),
                        attempt.get("sell_reference_close"),
                        attempt.get("buy_fill"),
                        attempt.get("sell_fill"),
                        attempt.get("net_debit_points"),
                        attempt.get("maximum_debit_points"),
                        cap_check.get("status", "Result not recorded; operands only"),
                    ]

        write_sheet(
            "Spread attempts",
            [
                "Date",
                "Time (IST)",
                "Decision #",
                "Attempt #",
                "Buy strike",
                "Sell strike",
                "Width",
                "Buy option close",
                "Sell option close",
                "Buy fill",
                "Sell fill",
                "Net debit",
                "Debit cap",
                "Result",
            ],
            spread_attempts(),
        )

        def monitoring() -> Iterator[list[Any]]:
            for entry in journal:
                if entry["event_type"] != "POSITION_CHECK":
                    continue
                payload = entry.get("payload") or {}
                saved_checks = payload.get("checks") or [
                    {
                        "label": "Individual exit results not recorded; see Journal evidence",
                        "status": "unavailable",
                    }
                ]
                for check in saved_checks:
                    yield [
                        entry["session_date"],
                        local_time(entry["timestamp"]),
                        entry.get("signal_sequence"),
                        entry["sequence"],
                        check.get("label"),
                        check.get("status"),
                        check.get("observed_value"),
                        check.get("comparator"),
                        check.get("threshold_value"),
                        payload.get("action", "Not recorded"),
                        payload.get("buy_reference_close"),
                        payload.get("sell_reference_close"),
                        payload.get("spread_value_points"),
                        payload.get("spot_close"),
                        payload.get("futures_vwap"),
                    ]

        write_sheet(
            "Monitoring",
            [
                "Date",
                "Time (IST)",
                "Entry decision #",
                "Journal #",
                "Condition",
                "Result",
                "Observed value",
                "Operator",
                "Threshold",
                "Action",
                "Long option close",
                "Short option close",
                "Spread value",
                "Spot close",
                "Futures VWAP",
            ],
            monitoring(),
        )
        write_sheet(
            "Trades",
            [
                "Trade #",
                "Entry time (IST)",
                "Exit time (IST)",
                "Exit reason",
                "Expiry",
                "Lot size",
                "Lot-size source",
                "Lots",
                "Total quantity",
                "Entry value",
                "Exit value",
                "Gross outcome",
                "Costs",
                "Net outcome",
                "Unit",
                "Entry decision ID",
            ],
            (
                [
                    t["sequence"],
                    local_time(t["entry_timestamp"]),
                    local_time(t["exit_timestamp"]),
                    t["exit_reason"],
                    (t.get("assumptions") or {}).get("expiry"),
                    (t.get("assumptions") or {}).get("lot_size"),
                    (t.get("assumptions") or {}).get("lot_size_source"),
                    (t.get("assumptions") or {}).get("lots_traded")
                    or (t.get("assumptions") or {}).get("lots"),
                    (t.get("assumptions") or {}).get("total_quantity") or t["quantity"],
                    t["entry_reference"],
                    t["exit_reference"],
                    t["gross_outcome"],
                    t["costs"],
                    t["net_outcome"],
                    t["unit"],
                    t["entry_signal_evaluation_id"],
                ]
                for t in trades
            ),
        )

        def evidence_rows(records: Iterable[dict[str, Any]], identity: str) -> Iterator[list[Any]]:
            for record in records:
                for path, value in flatten(record):
                    # Long values are losslessly split; IDs + field + part identify the original.
                    exact = value.isoformat() if isinstance(value, (date, datetime)) else str(value)
                    for offset in range(0, max(1, len(exact)), 30000):
                        yield [
                            record.get("session_date"),
                            record.get(identity),
                            path,
                            offset // 30000 + 1,
                            exact[offset : offset + 30000],
                        ]

        def source_table(name: str, records: Iterable[dict[str, Any]]) -> None:
            # One source record per row avoids millions of repeated field-name/ID rows.
            # Sparse columns retain their original dotted source paths; absent != null.
            columns = dict.fromkeys(["session_date", "sequence"])
            long_value = False
            for record in records:
                for field, value in flatten(record):
                    columns[field] = None
                    long_value = long_value or len(str(value)) > 32767
            if long_value or len(columns) > 16384:
                write_sheet(
                    name,
                    ["Date", "Record #", "Field", "Part", "Exact value"],
                    evidence_rows(records, "sequence"),
                )
                return

            def rows() -> Iterator[list[Any]]:
                for record in records:
                    fields = {
                        field: value.isoformat()
                        if isinstance(value, (date, datetime))
                        else str(value)
                        for field, value in flatten(record)
                    }
                    yield [fields.get(field, MISSING) for field in columns]

            write_sheet(name, list(columns), rows())

        source_table("Signal evidence", signals)
        source_table("Journal evidence", journal)
        write_sheet(
            "Trade evidence",
            ["Date", "Trade #", "Field", "Part", "Exact value"],
            evidence_rows(trades, "sequence"),
        )
        write_sheet(
            "Day evidence",
            ["Date", "Day", "Field", "Part", "Exact value"],
            evidence_rows(days, "session_date"),
        )
        write_sheet(
            "System events",
            ["Date", "Event #", "Field", "Part", "Exact value"],
            evidence_rows(events, "sequence"),
        )
        write_sheet(
            "Equity",
            ["Date", "Point #", "Field", "Part", "Exact value"],
            evidence_rows(equity, "sequence"),
        )
        write_sheet(
            "Analytics",
            ["Date", "Period", "Field", "Part", "Exact value"],
            evidence_rows(analytics, "period_start"),
        )
        write_sheet(
            "Run settings",
            ["Date", "Run ID", "Field", "Part", "Exact value"],
            evidence_rows([{**summary, "attempt_id": attempt_id}], "run_id"),
        )
        write_sheet(
            "Read me",
            ["Topic", "Explanation"],
            [
                [
                    "Purpose",
                    "Saved run evidence for partner review. Filter Date in Days, Decisions and Checks to follow a session. Decision # joins signal checks to entry and monitoring evidence.",
                ],
                [
                    "Full run",
                    "Includes all persisted rows for this run, independent of the page's filters or loaded pages. Journal and day evidence belong to the export's execution attempt.",
                ],
                [
                    "No replay",
                    "Export never recalculates historical rules. Missing details are marked Not recorded or unavailable. A new run is required for newly added instrumentation.",
                ],
                [
                    "Check results",
                    "passed / failed describe the stored condition. A failed mandatory gate blocks entry; a failed score contributor adds no points. not_evaluated means an earlier check prevented evaluation; informational is not a pass.",
                ],
                [
                    "Blank score",
                    "A blank score means an earlier mandatory gate rejected the candle before confirmation scoring began. It is different from a calculated score of zero.",
                ],
                [
                    "Signal vs trade",
                    "An accepted signal is not proof of a trade. Use the linked Trade #. Execution can reject an otherwise qualifying signal.",
                ],
                [
                    "Excluded days",
                    "Excluded sessions were not evaluated and are not no-signal days. Legacy runs without day classifications cannot prove full requested-session coverage.",
                ],
                [
                    "Monitoring",
                    "Exit checks use stored evaluation order. Earlier exits short-circuit later checks. Legacy checkpoints retain operands in Journal evidence, without invented pass/fail results.",
                ],
                [
                    "Precision",
                    "Readable sheets use Excel numbers up to 15 digits. Evidence sheets retain exact source text (including null as None); no Excel formula reruns trading decisions. Long values use numbered parts.",
                ],
                [
                    "Lot size",
                    "Expiry, lot size, lot-size source, lots and total quantity are stored per trade. P&L multiplication uses total quantity from the selected option-contract metadata.",
                ],
                [
                    "Time",
                    "Readable timestamps are Asia/Kolkata (IST). Raw evidence preserves the source offset. System event times describe processing time, not market time.",
                ],
                [
                    "Pricing",
                    "OPTION_SPREAD_SIMULATION is estimated historical OHLC plus configured slippage and costs, not executable bid/ask fills. PLATFORM_DEMO is synthetic DEMO_UNITS, never INR.",
                ],
                ["Schema", "stockbot.run-logs.v1"],
                ["Source", f"PostgreSQL persisted run {summary['run_id']}; attempt {attempt_id}"],
            ],
        )
        write_sheet(
            "Row counts",
            ["Collection", "Records"],
            [
                ["signals", signal_count],
                ["trades", trade_count],
                ["journal", journal_count],
                ["days", day_count],
                ["events", event_count],
                ["equity", equity_count],
                ["analytics", analytics_count],
                *[[key, count] for key, count in counts.items()],
            ],
        )
    return {
        "signals": signal_count,
        "trades": trade_count,
        "journal": journal_count,
        "days": day_count,
        "events": event_count,
        "equity": equity_count,
        "analytics_periods": analytics_count,
    }
