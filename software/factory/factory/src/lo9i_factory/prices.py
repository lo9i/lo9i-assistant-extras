"""What the direct APIs charge, to count what a task spends (SPEC §3). USD per
million tokens; Copilot is counted in requests instead, so it has none.

The table holds what was known when this version came out. A model it
doesn't have, or one whose price changed, takes `prices.<provider:model>` in
Settings. A model with neither can't be used: its calls couldn't be counted
against the task's limit.
"""

from dataclasses import dataclass

from .errors import ValidationError
from .settings import Settings

# Anthropic writes the prompt cache at 1.25 times the input price (its
# 5-minute cache) and reads it at the price given.
_CACHE_WRITE = 1.25


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


def _anthropic(input: float, output: float, cache_read: float) -> Price:
    return Price(input, output, cache_read, input * _CACHE_WRITE)


def _openai(input: float, output: float, cached: float) -> Price:
    # OpenAI caches on its own and doesn't charge for writing it.
    return Price(input, output, cached, input)


PRICES = {
    "anthropic:claude-fable-5-1": _anthropic(10, 50, 0.25),
    "anthropic:claude-fable-5": _anthropic(10, 50, 1),
    "anthropic:claude-opus-5-5": _anthropic(4, 20, 0.2),
    "anthropic:claude-opus-5": _anthropic(5, 25, 0.5),
    "anthropic:claude-opus-4-8": _anthropic(5, 25, 0.5),
    "anthropic:claude-opus-4-7": _anthropic(5, 25, 0.5),
    "anthropic:claude-opus-4-6": _anthropic(5, 25, 0.5),
    "anthropic:claude-sonnet-5-5": _anthropic(2, 10, 0.2),
    "anthropic:claude-sonnet-5": _anthropic(2, 10, 0.2),
    "anthropic:claude-sonnet-4-6": _anthropic(3, 15, 0.3),
    "anthropic:claude-haiku-4-5": _anthropic(1, 5, 0.1),
    "openai:gpt-5": _openai(1.25, 10, 0.125),
    "openai:gpt-5-mini": _openai(0.25, 2, 0.025),
    "openai:gpt-5-nano": _openai(0.05, 0.4, 0.005),
}


def find(model: str, settings: Settings) -> Price | None:
    """A provider:model's price: the user's, else the table's."""
    if model in settings.prices:
        return Price(**settings.prices[model])
    return PRICES.get(model)


def price(model: str, settings: Settings) -> Price:
    """Raises ValidationError for a model of a direct API with no price."""
    found = find(model, settings)
    if found is None:
        raise ValidationError(
            f"no price for {model}, so its calls can't be counted against the task's limit;"
            f" set prices.{model} in Settings (USD per million tokens: input, output)"
        )
    return found


def usd(p: Price, input_tokens: int, output_tokens: int, cache_read_tokens: int, cache_write_tokens: int) -> float:
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1_000_000
