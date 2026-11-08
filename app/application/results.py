from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.application.backtests import BacktestServiceError
from app.db.models.backtests import BacktestRun
from app.db.repositories.backtests import BacktestRepository
from app.domain.comparison_series import NumericSeriesPoint, envelope_downsample
from app.domain.run_analytics import ClosedOutcome, calculate_trade_periods

DEMO_NOTICE = (
    "Synthetic platform fixture only. Results are in DEMO_UNITS, are not financial P&L, "
    "and do not represent strategy performance."
)
ESTIMATE_NOTICE = (
    "Historical INR estimate using completed option-candle closes and configured adverse "
    "slippage. Executable bid/ask quotes are unavailable."
)
EXPORT_SCHEMA_VERSION = "stockbot.backtest-export.v2"
EXPORT_ROW_LIMIT = 10_000
AnalyticsGranularity = Literal["TRADE", "DAY", "MONTH"]
AnalyticsSort = Literal["PERIOD_ASC", "PERIOD_DESC"]


def _snapshot_section(run: BacktestRun, name: str) -> dict[str, Any]:
    value = run.snapshot.get(name, {})
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _safe_metrics(run: BacktestRun) -> dict[str, Any]:
    value = dict(run.metrics.metrics) if run.metrics else {}
    if run.mode not in {"PLATFORM_DEMO", "OPTION_SPREAD_SIMULATION"}:
        raise BacktestServiceError(
            "RESULT_MODE_UNSUPPORTED",
            "This result mode does not expose comparable outcome data.",
            409,
        )
    is_estimate = run.mode == "OPTION_SPREAD_SIMULATION"
    expected = {
        "synthetic": not is_estimate,
        "unit": "INR" if is_estimate else "DEMO_UNITS",
        "financial_pnl_available": is_estimate,
    }
    if value and any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise BacktestServiceError(
            "UNSAFE_RESULT_METADATA",
            "The stored result does not contain the required pricing safeguards.",
            409,
        )
    return value | {"mode": run.mode, **expected}


def _flatten(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.update(_flatten(cast(dict[str, Any], item), path))
        else:
            result[path] = item
    return result


def _analytics_row(
    value: Any,
    *,
    granularity: AnalyticsGranularity,
    exit_reason: str | None = None,
) -> dict[str, Any]:
    """Serialize one stored or calculated row without formatting synthetic units."""

    def field(name: str, stored_name: str | None = None) -> Any:
        fallback = getattr(value, stored_name, None) if stored_name is not None else None
        return getattr(value, name, fallback)

    return {
        "granularity": granularity,
        "period_start": field("period_start").isoformat(),
        "period_end": field("period_end", "period_start").isoformat(),
        "partial": bool(field("partial")),
        "eligible_session_count": int(field("eligible_session_count")),
        "trade_count": int(field("trade_count")),
        "wins": int(field("wins")),
        "losses": int(field("losses")),
        "breakevens": int(field("breakevens")),
        "gross_outcome": str(field("gross", "gross_demo_units")),
        "costs": str(field("costs", "costs_demo_units")),
        "net_outcome": str(field("net", "net_demo_units")),
        "cumulative_net_outcome": str(field("cumulative_net", "cumulative_net_demo_units")),
        "equity_end": str(field("equity_end", "equity_demo_units")),
        "local_max_drawdown": str(field("local_max_drawdown", "local_max_drawdown_demo_units")),
        "run_drawdown_end": str(field("run_drawdown_end", "run_drawdown_demo_units")),
        **(
            {
                "sequence": field("sequence"),
                "entry_timestamp": field("entry_timestamp").isoformat(),
                "exit_timestamp": field("exit_timestamp").isoformat(),
                "exit_reason": exit_reason if exit_reason is not None else field("exit_reason"),
            }
            if granularity == "TRADE"
            else {}
        ),
    }


def _differences(runs: list[BacktestRun], section: str) -> dict[str, dict[str, Any]]:
    if section == "parameters":
        section_values = [_snapshot_section(run, "algorithm").get("parameters", {}) for run in runs]
        flattened = [
            _flatten(cast(dict[str, Any], value)) if isinstance(value, dict) else {}
            for value in section_values
        ]
    else:
        flattened = [_flatten(_snapshot_section(run, section)) for run in runs]
    keys = sorted({key for values in flattened for key in values})
    differences: dict[str, dict[str, Any]] = {}
    for key in keys:
        run_values = {str(run.id): flattened[index].get(key) for index, run in enumerate(runs)}
        if len({repr(value) for value in run_values.values()}) > 1:
            differences[key] = run_values
    return differences


class ResultService:
    def __init__(self, session: Session) -> None:
        self.runs = BacktestRepository(session)

    def comparison_preflight(self, run_ids: list[UUID]) -> dict[str, Any]:
        if len(set(run_ids)) != len(run_ids):
            raise BacktestServiceError(
                "DUPLICATE_COMPARISON_RUN",
                "Each comparison run must be selected only once.",
                422,
            )
        runs = self.runs.get_runs(run_ids)
        found = {run.id for run in runs}
        issues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        missing = [run_id for run_id in run_ids if run_id not in found]
        if missing:
            issues.append(
                {
                    "code": "COMPARISON_RUN_NOT_FOUND",
                    "message": "One or more selected runs do not exist.",
                    "run_ids": missing,
                }
            )
        unsupported = [
            run.id for run in runs if run.mode not in {"PLATFORM_DEMO", "OPTION_SPREAD_SIMULATION"}
        ]
        modes = {run.mode for run in runs}
        if unsupported or len(modes) > 1:
            issues.append(
                {
                    "code": "COMPARISON_MODE_INCOMPATIBLE",
                    "message": "Comparisons require runs with the same supported result mode.",
                    "run_ids": unsupported or [run.id for run in runs],
                }
            )
        incomplete = [run.id for run in runs if run.status != "COMPLETED"]
        if incomplete:
            issues.append(
                {
                    "code": "COMPARISON_RUN_NOT_COMPLETED",
                    "message": "Comparisons require completed runs.",
                    "run_ids": incomplete,
                }
            )
        same_dataset = len({run.dataset_version_id for run in runs}) <= 1
        if not same_dataset:
            warnings.append(
                {
                    "code": "COMPARISON_DATASET_INCOMPATIBLE",
                    "message": "Dataset versions differ; this is a diagnostic comparison only.",
                    "run_ids": [run.id for run in runs],
                }
            )
        same_range = len({(run.start_date, run.end_date) for run in runs}) <= 1
        if not same_range:
            warnings.append(
                {
                    "code": "COMPARISON_RANGE_INCOMPATIBLE",
                    "message": "Date ranges differ; this is a diagnostic comparison only.",
                    "run_ids": [run.id for run in runs],
                }
            )
        code_versions = {_snapshot_section(run, "algorithm").get("code_version") for run in runs}
        if len(code_versions) > 1:
            warnings.append(
                {
                    "code": "COMPARISON_CODE_VERSION_DIFFERS",
                    "message": "Algorithm code versions differ; interpret metric changes carefully.",
                    "run_ids": [run.id for run in runs],
                }
            )
        coverage_digests = {_snapshot_section(run, "coverage").get("digest") for run in runs}
        same_coverage = len(coverage_digests) <= 1 and None not in coverage_digests
        if not same_coverage:
            warnings.append(
                {
                    "code": "COMPARISON_COVERAGE_DIFFERS",
                    "message": "Included-session coverage differs; this is a diagnostic comparison only.",
                    "run_ids": [run.id for run in runs],
                }
            )
        engine_families = {
            str(run.snapshot.get("engine_version", "legacy")).split("-bull-call", 1)[-1]
            for run in runs
        }
        same_engine_family = len(engine_families) <= 1
        if not same_engine_family:
            warnings.append(
                {
                    "code": "COMPARISON_ENGINE_FAMILY_DIFFERS",
                    "message": "Engine families differ; this is a diagnostic comparison only.",
                    "run_ids": [run.id for run in runs],
                }
            )
        performance_comparable = bool(
            runs and same_dataset and same_range and same_coverage and same_engine_family
        )
        mode = next(iter(modes), None)
        is_estimate = mode == "OPTION_SPREAD_SIMULATION"
        return {
            "allowed": not issues,
            "run_ids": run_ids,
            "mode": mode if len(modes) == 1 else None,
            "synthetic": not is_estimate,
            "unit": "INR" if is_estimate else "DEMO_UNITS",
            "financial_pnl_available": is_estimate,
            "issues": issues,
            "warnings": warnings,
            "performance_comparable": performance_comparable,
            "comparison_scope": "PERFORMANCE" if performance_comparable else "DIAGNOSTIC_ONLY",
        }

    def compare(self, run_ids: list[UUID]) -> dict[str, Any]:
        preflight = self.comparison_preflight(run_ids)
        if not preflight["allowed"]:
            raise BacktestServiceError(
                "COMPARISON_PREFLIGHT_FAILED",
                "The selected runs cannot be compared safely.",
                409,
                {"preflight": preflight},
            )
        runs = self.runs.get_runs(run_ids)
        mode = runs[0].mode
        is_estimate = mode == "OPTION_SPREAD_SIMULATION"
        values = []
        for run in runs:
            algorithm = _snapshot_section(run, "algorithm")
            values.append(
                {
                    "run_id": run.id,
                    "dataset_version_id": run.dataset_version_id,
                    "algorithm_version_id": run.algorithm_version_id,
                    "start_date": run.start_date,
                    "end_date": run.end_date,
                    "status": run.status,
                    "code_version": str(algorithm.get("code_version", "unknown")),
                    "parameters": dict(algorithm.get("parameters", {})),
                    "assumptions": _snapshot_section(run, "assumptions"),
                    "metrics": _safe_metrics(run),
                    "synthetic": not is_estimate,
                    "unit": "INR" if is_estimate else "DEMO_UNITS",
                    "financial_pnl_available": is_estimate,
                }
            )
        return {
            "run_ids": run_ids,
            "mode": mode,
            "synthetic": not is_estimate,
            "unit": "INR" if is_estimate else "DEMO_UNITS",
            "financial_pnl_available": is_estimate,
            "notice": ESTIMATE_NOTICE if is_estimate else DEMO_NOTICE,
            "parameter_differences": _differences(runs, "parameters"),
            "assumption_differences": _differences(runs, "assumptions"),
            "runs": values,
            "performance_comparable": preflight["performance_comparable"],
            "comparison_scope": preflight["comparison_scope"],
        }

    @staticmethod
    def _comparison_hash(runs: list[BacktestRun]) -> str:
        """Bind a cached/browser comparison to the immutable records it shows."""

        identity = [
            {
                "id": str(run.id),
                "dataset_version_id": str(run.dataset_version_id),
                "algorithm_version_id": str(run.algorithm_version_id),
                "start_date": run.start_date.isoformat(),
                "end_date": run.end_date.isoformat(),
                "mode": run.mode,
            }
            for run in runs
        ]
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _bounded_points(
        points: list[NumericSeriesPoint], *, maximum: int, resolution: str
    ) -> tuple[list[NumericSeriesPoint], str]:
        if resolution == "raw":
            if len(points) > maximum:
                raise BacktestServiceError(
                    "COMPARISON_SERIES_POINT_LIMIT",
                    "Raw comparison data exceeds the requested per-run point limit.",
                    422,
                    {"original_point_count": len(points), "max_points_per_run": maximum},
                )
            return points, "raw"
        return envelope_downsample(points, maximum), (
            "raw" if len(points) <= maximum else "envelope"
        )

    def comparison_series(
        self,
        run_ids: list[UUID],
        *,
        requested_series: Sequence[str],
        start: datetime | None,
        end: datetime | None,
        resolution: str,
        max_points_per_run: int,
    ) -> dict[str, Any]:
        """Return only bounded persisted evidence after re-running safety preflight."""

        if start is not None and end is not None and start > end:
            raise BacktestServiceError(
                "INVALID_COMPARISON_SERIES_RANGE",
                "start must be on or before end.",
                422,
            )
        preflight = self.comparison_preflight(run_ids)
        if not preflight["allowed"]:
            raise BacktestServiceError(
                "COMPARISON_PREFLIGHT_FAILED",
                "The selected runs cannot be compared safely.",
                409,
                {"preflight": preflight},
            )
        runs = self.runs.get_runs(run_ids)
        mode = runs[0].mode
        is_estimate = mode == "OPTION_SPREAD_SIMULATION"
        selected = set(requested_series)
        values: list[dict[str, Any]] = []
        coverage: list[tuple[datetime, datetime]] = []
        response_resolution = "raw"
        reason_categories: set[str] = set()

        for run in runs:
            run_values: dict[str, Any] = {
                "run_id": run.id,
                "equity": [],
                "drawdown": [],
                "trade_outcomes": [],
                "signal_decisions": [],
                "rejection_reasons": [],
                "original_point_counts": {},
                "returned_point_counts": {},
            }
            timestamps: list[datetime] = []
            if {"equity", "drawdown"} & selected:
                equity = self.runs.list_comparison_equity(run.id, start=start, end=end)
                timestamps.extend(item.timestamp for item in equity)
                for name, attribute in (
                    ("equity", "equity_demo_units"),
                    ("drawdown", "drawdown_demo_units"),
                ):
                    if name not in selected:
                        continue
                    points = [
                        NumericSeriesPoint(item.timestamp, item.sequence, getattr(item, attribute))
                        for item in equity
                    ]
                    bounded, point_resolution = self._bounded_points(
                        points, maximum=max_points_per_run, resolution=resolution
                    )
                    response_resolution = (
                        "envelope" if point_resolution == "envelope" else response_resolution
                    )
                    run_values[name] = [
                        {
                            "timestamp": item.timestamp,
                            "sequence": item.sequence,
                            "value": item.value,
                        }
                        for item in bounded
                    ]
                    run_values["original_point_counts"][name] = len(points)
                    run_values["returned_point_counts"][name] = len(bounded)
            if "trade_outcomes" in selected:
                trades = self.runs.list_comparison_trades(run.id, start=start, end=end)
                timestamps.extend(item.exit_timestamp for item in trades)
                points = [
                    NumericSeriesPoint(item.exit_timestamp, item.sequence, item.net_demo_units)
                    for item in trades
                ]
                bounded, point_resolution = self._bounded_points(
                    points, maximum=max_points_per_run, resolution=resolution
                )
                response_resolution = (
                    "envelope" if point_resolution == "envelope" else response_resolution
                )
                run_values["trade_outcomes"] = [
                    {"timestamp": item.timestamp, "sequence": item.sequence, "value": item.value}
                    for item in bounded
                ]
                run_values["original_point_counts"]["trade_outcomes"] = len(points)
                run_values["returned_point_counts"]["trade_outcomes"] = len(bounded)
            if {"signal_decisions", "rejection_reasons"} & selected:
                signals = self.runs.list_comparison_signals(run.id, start=start, end=end)
                timestamps.extend(item.timestamp for item in signals)
                if "signal_decisions" in selected:
                    decisions = Counter(item.decision for item in signals)
                    run_values["signal_decisions"] = [
                        {"category": category, "count": decisions.get(category, 0)}
                        for category in ("ACCEPTED", "REJECTED")
                    ]
                if "rejection_reasons" in selected:
                    reasons = Counter(
                        item.rejection_reason
                        for item in signals
                        if item.rejection_reason is not None
                    )
                    reason_categories.update(reasons)
                    run_values["rejection_reasons"] = [
                        {"category": category, "count": count}
                        for category, count in sorted(reasons.items())
                    ]
            if timestamps:
                coverage.append((min(timestamps), max(timestamps)))
            values.append(run_values)

        warnings: list[dict[str, Any]] = []
        effective_start = max((item[0] for item in coverage), default=None)
        effective_end = min((item[1] for item in coverage), default=None)
        if coverage and (
            len({item[0] for item in coverage}) > 1 or len({item[1] for item in coverage}) > 1
        ):
            warnings.append(
                {
                    "code": "COMPARISON_PARTIAL_SERIES_OVERLAP",
                    "message": "Returned evidence does not cover the same timestamps for every run.",
                    "run_ids": run_ids,
                }
            )
        if (
            effective_start is not None
            and effective_end is not None
            and effective_start > effective_end
        ):
            warnings.append(
                {
                    "code": "COMPARISON_NO_SHARED_SERIES_OVERLAP",
                    "message": "No returned timestamps overlap across every selected run.",
                    "run_ids": run_ids,
                }
            )
        return {
            "comparison_hash": self._comparison_hash(runs),
            "run_ids": run_ids,
            "mode": mode,
            "synthetic": not is_estimate,
            "unit": "INR" if is_estimate else "DEMO_UNITS",
            "financial_pnl_available": is_estimate,
            "requested_series": list(requested_series),
            "effective_start": effective_start,
            "effective_end": effective_end,
            "resolution": response_resolution,
            "max_points_per_run": max_points_per_run,
            "warnings": warnings,
            "category_definitions": {
                "signal_decisions": ["ACCEPTED", "REJECTED"],
                "rejection_reasons": sorted(reason_categories),
            },
            "runs": values,
        }

    def _export_selected_analytics(
        self,
        run: BacktestRun,
        *,
        granularity: AnalyticsGranularity,
        start_date: date | None,
        end_date: date | None,
        include_zero_periods: bool,
        sort: AnalyticsSort,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        analytics = self.runs.get_analytics(run.id)
        if analytics is None:
            raise BacktestServiceError(
                "RUN_ANALYTICS_UNAVAILABLE",
                "Completed-run analytics are not available for this backtest yet.",
                409,
            )
        if start_date is not None and end_date is not None and start_date > end_date:
            raise BacktestServiceError(
                "INVALID_ANALYTICS_DATE_RANGE",
                "start_date must be on or before end_date.",
                422,
            )
        if granularity == "TRADE":
            records = self.runs.list_trades_chronological(run.id)
            periods = calculate_trade_periods(
                [
                    ClosedOutcome(
                        sequence=item.sequence,
                        entry_timestamp=item.entry_timestamp,
                        exit_timestamp=item.exit_timestamp,
                        gross=item.gross_demo_units,
                        costs=item.costs_demo_units,
                        net=item.net_demo_units,
                    )
                    for item in records
                ],
                timezone=analytics.timezone,
            )
            reasons = {item.sequence: item.exit_reason for item in records}
            selected = [
                item
                for item in periods
                if (start_date is None or item.period_start >= start_date)
                and (end_date is None or item.period_start <= end_date)
            ]
            self._check_export_limit(len(selected))
            rows = [
                _analytics_row(item, granularity="TRADE", exit_reason=reasons[item.sequence])
                for item in selected
            ]
        else:
            values, _, count = self.runs.list_analytics_periods(
                run.id,
                granularity,
                None,
                EXPORT_ROW_LIMIT,
                start_date=start_date,
                end_date=end_date,
                include_zero_periods=include_zero_periods,
                sort=sort,
            )
            self._check_export_limit(count)
            rows = [_analytics_row(item, granularity=granularity) for item in values]
        summary = analytics.summary
        reconciliation = "NOT_APPLICABLE"
        if start_date is None and end_date is None and include_zero_periods:
            expected_count = int(summary["total_trades"])
            expected_net = Decimal(str(summary["net_outcome"]))
            actual_count = sum(int(row["trade_count"]) for row in rows)
            actual_net = sum((Decimal(str(row["net_outcome"])) for row in rows), Decimal())
            if actual_count != expected_count or actual_net != expected_net:
                raise BacktestServiceError(
                    "ANALYTICS_EXPORT_RECONCILIATION_FAILED",
                    "Stored analytics did not reconcile to the immutable completed-run summary.",
                    409,
                    {
                        "granularity": granularity,
                        "expected_trade_count": expected_count,
                        "actual_trade_count": actual_count,
                        "expected_net_outcome": str(expected_net),
                        "actual_net_outcome": str(actual_net),
                    },
                )
            reconciliation = "PASSED"
        return rows, {
            "granularity": granularity,
            "start_date": start_date,
            "end_date": end_date,
            "include_zero_periods": include_zero_periods,
            "sort": sort,
            "schema_version": analytics.schema_version,
            "calculator_version": analytics.calculator_version,
            "source_fingerprint": analytics.source_fingerprint,
            "timezone": analytics.timezone,
            "attribution": "EXIT_TIMESTAMP",
            "reconciliation": reconciliation,
        }

    def export(
        self,
        run_id: UUID,
        kind: str,
        *,
        granularity: AnalyticsGranularity | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        include_zero_periods: bool = True,
        sort: AnalyticsSort = "PERIOD_ASC",
    ) -> dict[str, Any]:
        run = self.runs.get_run(run_id)
        if run is None:
            raise BacktestServiceError(
                "BACKTEST_NOT_FOUND", "The requested backtest does not exist.", 404
            )
        if run.status != "COMPLETED":
            raise BacktestServiceError(
                "EXPORT_RUN_NOT_COMPLETED", "Only completed runs can be exported.", 409
            )
        is_estimate = run.mode == "OPTION_SPREAD_SIMULATION"
        export_synthetic = not is_estimate
        export_unit = "INR" if is_estimate else "DEMO_UNITS"
        export_notice = ESTIMATE_NOTICE if is_estimate else DEMO_NOTICE
        metrics = _safe_metrics(run)
        rows: list[dict[str, Any]]
        analytics_summary: dict[str, Any] | None = None
        if kind == "summary":
            rows = [
                {
                    "run_id": str(run.id),
                    "dataset_version_id": str(run.dataset_version_id),
                    "algorithm_version_id": str(run.algorithm_version_id),
                    "start_date": run.start_date.isoformat(),
                    "end_date": run.end_date.isoformat(),
                    "status": run.status,
                    "metrics": metrics,
                }
            ]
        elif kind == "trades":
            count = self.runs.count_trades(run.id)
            self._check_export_limit(count)
            trades, _ = self.runs.list_trades(run.id, None, max(count, 1))
            rows = [
                {
                    "run_id": str(run.id),
                    "sequence": value.sequence,
                    "entry_timestamp": value.entry_timestamp.isoformat(),
                    "exit_timestamp": value.exit_timestamp.isoformat(),
                    "exit_reason": value.exit_reason,
                    "quantity": str(value.quantity),
                    "entry_reference": str(value.entry_reference),
                    "exit_reference": str(value.exit_reference),
                    "gross_demo_units": str(value.gross_demo_units),
                    "costs_demo_units": str(
                        getattr(
                            value, "costs_demo_units", value.gross_demo_units - value.net_demo_units
                        )
                    ),
                    "net_demo_units": str(value.net_demo_units),
                    "assumptions": value.assumptions,
                    "legs": [
                        {
                            "leg_number": leg.leg_number,
                            "name": leg.name,
                            "entry_reference": str(leg.entry_reference),
                            "exit_reference": str(leg.exit_reference),
                            "demo_units": str(leg.demo_units),
                            "synthetic": getattr(leg, "synthetic", export_synthetic),
                            "unit": getattr(leg, "unit", export_unit),
                            "financial_pnl_available": getattr(
                                leg, "financial_pnl_available", is_estimate
                            ),
                        }
                        for leg in value.legs
                    ],
                    "synthetic": getattr(value, "synthetic", export_synthetic),
                    "unit": getattr(value, "unit", export_unit),
                    "financial_pnl_available": getattr(
                        value, "financial_pnl_available", is_estimate
                    ),
                }
                for value in trades
            ]
        elif kind == "signals":
            count = self.runs.count_signals(run.id)
            self._check_export_limit(count)
            signals, _ = self.runs.list_signals(run.id, None, max(count, 1))
            rows = [
                {
                    "run_id": str(run.id),
                    "timestamp": value.timestamp.isoformat(),
                    "session_date": value.session_date.isoformat(),
                    "decision": value.decision,
                    "rejection_reason": value.rejection_reason,
                    "window": value.window,
                    "score": value.score,
                    "inputs": value.inputs,
                    "synthetic": export_synthetic,
                    "unit": export_unit,
                    "financial_pnl_available": is_estimate,
                }
                for value in signals
            ]
        elif kind == "analytics":
            if granularity is not None:
                rows, research_view = self._export_selected_analytics(
                    run,
                    granularity=granularity,
                    start_date=start_date,
                    end_date=end_date,
                    include_zero_periods=include_zero_periods,
                    sort=sort,
                )
                selected_analytics = self.runs.get_analytics(run.id)
                if selected_analytics is None:  # Defensive: the helper already checks this.
                    raise BacktestServiceError(
                        "RUN_ANALYTICS_UNAVAILABLE",
                        "Completed-run analytics are not available for this backtest yet.",
                        409,
                    )
                analytics_summary = selected_analytics.summary
                return {
                    "metadata": {
                        "schema_version": EXPORT_SCHEMA_VERSION,
                        "exported_at": datetime.now(UTC),
                        "kind": kind,
                        "run_id": run.id,
                        "mode": run.mode,
                        "synthetic": export_synthetic,
                        "unit": export_unit,
                        "financial_pnl_available": is_estimate,
                        "notice": export_notice,
                        "snapshot": run.snapshot,
                        "analytics": analytics_summary,
                        "research_view": research_view,
                    },
                    "rows": rows,
                }
            analytics = self.runs.get_analytics(run.id)
            if analytics is None:
                raise BacktestServiceError(
                    "RUN_ANALYTICS_UNAVAILABLE",
                    "Completed-run analytics are not available for this backtest yet.",
                    409,
                )
            analytics_summary = analytics.summary
            day_rows, _, _ = self.runs.list_analytics_periods(run.id, "DAY", None, EXPORT_ROW_LIMIT)
            month_rows, _, _ = self.runs.list_analytics_periods(
                run.id, "MONTH", None, EXPORT_ROW_LIMIT
            )
            rows = [
                {
                    "run_id": str(run.id),
                    "granularity": value.granularity,
                    "period_start": value.period_start.isoformat(),
                    "period_end": value.period_end.isoformat(),
                    "partial": value.partial,
                    "eligible_session_count": value.eligible_session_count,
                    "trade_count": value.trade_count,
                    "wins": value.wins,
                    "losses": value.losses,
                    "breakevens": value.breakevens,
                    "gross_outcome": str(value.gross_demo_units),
                    "costs": str(value.costs_demo_units),
                    "net_outcome": str(value.net_demo_units),
                    "cumulative_net_outcome": str(value.cumulative_net_demo_units),
                    "equity_end": str(value.equity_demo_units),
                    "local_max_drawdown": str(value.local_max_drawdown_demo_units),
                    "run_drawdown_end": str(value.run_drawdown_demo_units),
                }
                for value in [*day_rows, *month_rows]
            ]
        else:
            raise BacktestServiceError(
                "EXPORT_KIND_UNSUPPORTED", "The requested export kind is unsupported.", 422
            )
        return {
            "metadata": {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "exported_at": datetime.now(UTC),
                "kind": kind,
                "run_id": run.id,
                "mode": run.mode,
                "synthetic": export_synthetic,
                "unit": export_unit,
                "financial_pnl_available": is_estimate,
                "notice": export_notice,
                "snapshot": run.snapshot,
                "analytics": analytics_summary,
            },
            "rows": rows,
        }

    @staticmethod
    def _check_export_limit(count: int) -> None:
        if count > EXPORT_ROW_LIMIT:
            raise BacktestServiceError(
                "EXPORT_ROW_LIMIT_EXCEEDED",
                "This export is too large for the synchronous MVP export endpoint.",
                409,
                {"row_count": count, "maximum": EXPORT_ROW_LIMIT},
            )
