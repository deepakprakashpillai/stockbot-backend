"""Truthful display labels for saved estimated historical research evidence."""

from __future__ import annotations

from collections.abc import Mapping

CURRENT_RESEARCH_LIMITATIONS = (
    "Results are unfiltered for market event days.",
    "The continuous front-month futures bundle does not verify contract rollover.",
    "Stops and targets are evaluated at five-minute closes; intrabar breaches are not detected.",
    "No account affordability constraint is applied to the configured lot count.",
)


def display_research_limitations(assumptions: Mapping[str, object]) -> list[str]:
    saved = assumptions.get("limitations")
    limitations = [str(item) for item in saved] if isinstance(saved, list) else []
    if assumptions.get("pricing_quality") == "estimated_historical_ohlc":
        for item in CURRENT_RESEARCH_LIMITATIONS:
            if item not in limitations:
                limitations.append(item)
    return limitations
