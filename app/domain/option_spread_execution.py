"""Compatibility imports for existing option-spread callers.

New code should import models, pricing, or engine directly by responsibility.
"""

from app.domain.option_spread_engine import simulate_option_spread_backtest
from app.domain.option_spread_models import (
    FINANCIAL_DECIMAL_QUANTUM,
    INR_RESULT_UNIT,
    PRICING_QUALITY,
    SPREAD_EXECUTION_ADAPTER_VERSION,
    OpenPositionDataGapError,
    OptionExecutionCancelled,
    OptionExitReason,
    OptionSpreadAssumptions,
    OptionSpreadEquityPointResult,
    OptionSpreadExecutionRejection,
    OptionSpreadExecutionResult,
    OptionSpreadLegResult,
    OptionSpreadTradeResult,
    StatutoryCharges,
    StatutoryCostCalculator,
)

__all__ = [
    "FINANCIAL_DECIMAL_QUANTUM",
    "INR_RESULT_UNIT",
    "PRICING_QUALITY",
    "SPREAD_EXECUTION_ADAPTER_VERSION",
    "OpenPositionDataGapError",
    "OptionExecutionCancelled",
    "OptionExitReason",
    "OptionSpreadAssumptions",
    "OptionSpreadEquityPointResult",
    "OptionSpreadExecutionRejection",
    "OptionSpreadExecutionResult",
    "OptionSpreadLegResult",
    "OptionSpreadTradeResult",
    "StatutoryCharges",
    "StatutoryCostCalculator",
    "simulate_option_spread_backtest",
]
