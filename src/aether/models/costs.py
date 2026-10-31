"""Approximate USD prices per million tokens; not a provider billing guarantee."""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)


# Groq model API prices, input/output respectively.
TOKEN_PRICES = MappingProxyType(
    {
        ("groq", "openai/gpt-oss-20b"): (Decimal("0.075"), Decimal("0.30")),
        ("groq", "openai/gpt-oss-120b"): (Decimal("0.15"), Decimal("0.60")),
        ("groq", "qwen/qwen3.8-27b"): (Decimal("0.80"), Decimal("4.00")),
    }
)


def estimate_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    prices: Mapping[str, ModelPrice] | None = None,
) -> tuple[Decimal, bool]:
    override = prices.get(f"{provider}/{model}") if prices is not None else None
    if override is not None:
        rates = override.input_per_mtok, override.output_per_mtok
    elif provider in {"ollama", "mock"}:
        return Decimal(0), False
    else:
        rates = TOKEN_PRICES.get((provider, model))
    if rates is None:
        return Decimal(0), True
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000, False
