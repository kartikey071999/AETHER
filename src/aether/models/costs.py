"""Approximate USD prices per million tokens; not a provider billing guarantee."""

from decimal import Decimal
from types import MappingProxyType

# Groq model API prices, input/output respectively.
TOKEN_PRICES = MappingProxyType(
    {
        ("groq", "openai/gpt-oss-20b"): (Decimal("0.075"), Decimal("0.30")),
        ("groq", "openai/gpt-oss-120b"): (Decimal("0.15"), Decimal("0.60")),
        ("groq", "qwen/qwen3.8-27b"): (Decimal("0.80"), Decimal("4.00")),
    }
)


def estimate_cost(
    provider: str, model: str, input_tokens: int, output_tokens: int
) -> tuple[Decimal, bool]:
    if provider in {"ollama", "mock"}:
        return Decimal(0), False
    prices = TOKEN_PRICES.get((provider, model))
    if prices is None:
        return Decimal(0), True
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000, False
