from datetime import UTC, datetime
from decimal import Decimal

from app.domain.market_data_sources import ExecutableOptionQuote, executable_quote_coverage


def quote(symbol: str, timestamp: datetime, bid: str, ask: str) -> ExecutableOptionQuote:
    return ExecutableOptionQuote(
        contract_symbol=symbol,
        timestamp=timestamp,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_quantity=75,
        ask_quantity=75,
        provider="sample-provider",
    )


def test_executable_quote_coverage_accepts_synchronized_valid_quotes() -> None:
    timestamp = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
    required = {("NIFTY-CALL-1", timestamp), ("NIFTY-CALL-2", timestamp)}
    result = executable_quote_coverage(
        required,
        [
            quote("NIFTY-CALL-1", timestamp, "10", "11"),
            quote("NIFTY-CALL-2", timestamp, "5", "6"),
        ],
    )

    assert result["complete"] is True
    assert result["observed_count"] == 2
    assert result["missing_count"] == 0
    assert result["invalid_count"] == 0


def test_executable_quote_coverage_reports_missing_and_crossed_quotes() -> None:
    timestamp = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
    result = executable_quote_coverage(
        {("NIFTY-CALL-1", timestamp), ("NIFTY-CALL-2", timestamp)},
        [quote("NIFTY-CALL-1", timestamp, "12", "11")],
    )

    assert result["complete"] is False
    assert result["missing_count"] == 1
    assert result["invalid_count"] == 1
    assert result["missing_examples"] == [
        {"contract_symbol": "NIFTY-CALL-2", "timestamp": timestamp.isoformat()}
    ]
