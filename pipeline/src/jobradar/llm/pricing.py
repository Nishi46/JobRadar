"""Per-model prices and the cost of one call.

Prices are USD per million tokens, copied from the providers' price pages for the
models our prompts name (the model lives in prompt frontmatter, so a new model means a
new entry here). An unknown model costs `None`, not 0: a silent zero would make a
missing entry look like a free run in `make cost`.

The two providers count tokens differently (see `llm.base`), so each entry says whether
cached tokens are already inside `input_tokens`.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_PER = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_read: float
    # None when the provider has no separate write price: written tokens are then billed
    # as ordinary input (OpenAI caches automatically and charges nothing extra to write).
    cache_write: float | None
    # True (OpenAI): input_tokens already includes cached tokens, so the cached part must
    # be moved from the full price to the cache price. False (Anthropic): input_tokens
    # is the uncached remainder and cache tokens are billed on top.
    cache_in_input: bool


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        input=0.10, output=0.50, cache_read=0.01, cache_write=0.125, cache_in_input=False
    ),
    "gpt-6.2-sol": ModelPrice(
        input=2.00, output=10.00, cache_read=0.10, cache_write=None, cache_in_input=True
    ),
}


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float | None:
    price = PRICES.get(model)
    if price is None:
        log.warning("no price for model %r; cost recorded as NULL", model)
        return None

    if price.cache_in_input:
        # Cached reads (and, with no write price, cache writes) are inside input_tokens.
        uncached = input_tokens - cache_read_tokens
        if uncached < 0:
            log.warning(
                "cached tokens (%d) exceeds input tokens (%d) for %r; clamping",
                cache_read_tokens,
                input_tokens,
                model,
            )
            uncached = 0
        dollars = uncached * price.input + cache_read_tokens * price.cache_read
    else:
        write_price = price.input if price.cache_write is None else price.cache_write
        dollars = (
            input_tokens * price.input
            + cache_read_tokens * price.cache_read
            + cache_write_tokens * write_price
        )
    return (dollars + output_tokens * price.output) / _PER
