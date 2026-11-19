"""Only the settings authorized for the first Model 2 research implementation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Model2Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    wing_width_points: Literal[150, 200] = Field(
        default=200, description="200-point primary or 150-point alternative wings."
    )
    slippage_percent: Decimal = Field(
        default=Decimal("2.00"),
        allow_inf_nan=False,
        description="Adverse per-leg fill slippage: exactly 0%, 1% or 2%. Assessment uses 2%.",
        json_schema_extra={"enum": ["0.00", "1.00", "2.00"], "unit": "percent"},
    )
    vix_state_filter_enabled: bool = Field(
        default=False,
        strict=True,
        description="Experimental VIX-rise filter; disabled in the primary specification.",
    )

    @field_validator("wing_width_points", mode="before")
    @classmethod
    def require_integer_width(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Wing width must be the integer 150 or 200.")
        return value

    @field_validator("slippage_percent", mode="before")
    @classmethod
    def reject_boolean_slippage(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("Slippage must be a percentage, not a boolean.")
        return value

    @field_validator("slippage_percent")
    @classmethod
    def approved_slippage_only(cls, value: Decimal) -> Decimal:
        if value not in (Decimal("0"), Decimal("1"), Decimal("2")):
            raise ValueError("Only 0%, 1% and 2% slippage are approved.")
        return value.quantize(Decimal("0.01"))

    @property
    def experimental(self) -> bool:
        return self.vix_state_filter_enabled


@dataclass(frozen=True)
class Model2Preset:
    key: str
    name: str
    parameters: Model2Parameters


def model2_presets() -> tuple[Model2Preset, Model2Preset]:
    """Return fresh immutable parameter values, without touching the catalog."""
    return (
        Model2Preset("primary_200", "Primary 200-point wings", Model2Parameters()),
        Model2Preset(
            "alternative_150",
            "Alternative 150-point wings",
            Model2Parameters(wing_width_points=150),
        ),
    )
