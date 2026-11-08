"""Compatibility imports for the derivatives data reader.

New domain code should use pure values in ``app.domain.derivatives``; storage code
belongs in ``app.infrastructure.market_data.parquet_derivatives``.
"""

from app.domain.derivatives import OptionCandle, OptionContract
from app.infrastructure.market_data.parquet_derivatives import (
    DerivativesSessionData,
    ParquetDerivativesProvider,
)

__all__ = [
    "DerivativesSessionData",
    "OptionCandle",
    "OptionContract",
    "ParquetDerivativesProvider",
]
