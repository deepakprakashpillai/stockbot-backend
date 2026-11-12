"""Compare the isolated Q10/L04 correction with V1.2 without writing business records."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.backtest_coverage import analyze_derivatives_coverage
from app.domain.algorithms import (
    BULL_CALL_SPREAD,
    FUTURES_BULL_CALL_SPREAD,
    AlgorithmDefinition,
    BullCallSpreadSignalParameters,
)
from app.domain.option_spread_models import OptionSpreadAssumptions
from app.domain.research_pipeline import run_chronological_option_research
from app.domain.signal_models import SignalDecision
from app.infrastructure.market_data.csv_reader import CsvMarketDataProvider
from app.infrastructure.market_data.parquet_derivatives import ParquetDerivativesProvider


def compare(bundle: Path, start: date, end: date) -> dict[str, object]:
    spot = bundle / "spot/nifty_spot_5min.csv"
    definitions = (BULL_CALL_SPREAD, FUTURES_BULL_CALL_SPREAD)
    coverage = {}
    for definition in definitions:
        parameters = definition.parameter_model()
        assert isinstance(parameters, BullCallSpreadSignalParameters)
        coverage[definition.code_version] = analyze_derivatives_coverage(
            bundle_dir=bundle,
            spot_path=spot,
            start_date=start,
            end_date=end,
            parameters=parameters,
            algorithm=definition,
        )
    common = set.intersection(*(set(value["included_sessions"]) for value in coverage.values()))

    def replay(definition: AlgorithmDefinition, sessions: set[str]) -> dict[str, object]:
        provider = ParquetDerivativesProvider(bundle)
        parameters = definition.parameter_model()
        assert isinstance(parameters, BullCallSpreadSignalParameters)
        configured = definition.configure(parameters.model_dump(mode="python"))
        signals: list[SignalDecision] = []
        result = run_chronological_option_research(
            provider.iter_enriched_spot_candles(
                CsvMarketDataProvider(spot).stream(start - timedelta(days=7), end)
            ),
            configured,
            provider,
            OptionSpreadAssumptions.from_parameters(parameters, starting_capital=Decimal("100000")),
            requested_start=start,
            requested_end=end,
            included_sessions={date.fromisoformat(day) for day in sessions},
            on_signal=signals.append,
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {"signals": [asdict(signal) for signal in signals], "result": asdict(result)},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return {
            "included_sessions": len(sessions),
            "evaluations": len(signals),
            "accepted_signals": sum(signal.decision == "ACCEPTED" for signal in signals),
            "accepted_signal_days": len(
                {signal.session_date for signal in signals if signal.decision == "ACCEPTED"}
            ),
            "signal_reasons": dict(
                Counter(signal.rejection_reason or "ACCEPTED" for signal in signals)
            ),
            "trades": len(result.trades),
            "trade_dates": [trade.entry_timestamp.date().isoformat() for trade in result.trades],
            "execution_rejections": dict(Counter(item.reason for item in result.rejections)),
            "exit_reasons": dict(Counter(trade.exit_reason for trade in result.trades)),
            "fingerprint": fingerprint,
        }

    return {
        "range": [start.isoformat(), end.isoformat()],
        "bundle": str(bundle),
        "scope": "Only futures-versus-futures entry and trend reversal; unchanged V1.2 thresholds and pricing.",
        "executable_quotes": False,
        "common_sessions": sorted(common),
        "versions": {
            definition.code_version: {
                "coverage_digest": coverage[definition.code_version]["coverage_digest"],
                "excluded_sessions": coverage[definition.code_version]["excluded_sessions"],
                "own_coverage": replay(
                    definition, set(coverage[definition.code_version]["included_sessions"])
                ),
                "common_coverage": replay(definition, common),
            }
            for definition in definitions
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle", type=Path, default=Path("data/datasets/upstox_nifty_derivatives_v6")
    )
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 7, 21))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 7, 17))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.bundle, args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {version: data["own_coverage"] for version, data in report["versions"].items()},
            indent=2,
        )
    )
