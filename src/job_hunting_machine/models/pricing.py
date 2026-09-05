"""Decimal price arithmetic. Cached input is a subset, reasoning is part of output."""

from dataclasses import dataclass
from decimal import Decimal

from job_hunting_machine.models.router import ModelDefinition


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        if any(
            type(v) is not int or v < 0
            for v in (
                self.input_tokens,
                self.cached_input_tokens,
                self.output_tokens,
                self.cache_write_tokens,
            )
        ):
            raise ValueError("Usage tokens must be nonnegative integers")
        if self.cached_input_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("Cached tokens cannot exceed total input")


def estimate_cost(model: ModelDefinition, usage: TokenUsage) -> Decimal:
    return (
        (usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens)
        * model.input_per_million_usd
        + usage.cached_input_tokens * model.cached_input_per_million_usd
        + usage.cache_write_tokens * model.cache_write_price
        + usage.output_tokens * model.output_per_million_usd
    ) / Decimal(1_000_000)
