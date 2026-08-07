"""Model pricing.

The transcripts record token counts but no dollar amounts, so every cost shown
by this dashboard is an ESTIMATE derived from the table below.

>>> EDIT THESE RATES. <<<
They are starting values, in US dollars per million tokens, and they go stale
whenever pricing changes. Verify them against current published pricing before
trusting any number the dashboard reports.

An unrecognised model yields None rather than a plausible-looking wrong figure.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input: float          # $ per 1M input tokens
    output: float         # $ per 1M output tokens
    cache_write: float    # $ per 1M cache-creation tokens
    cache_read: float     # $ per 1M cache-read tokens


PRICES = {
    "claude-opus-5": ModelPrice(
        input=15.0, output=75.0, cache_write=18.75, cache_read=1.50
    ),
    "claude-sonnet-5": ModelPrice(
        input=3.0, output=15.0, cache_write=3.75, cache_read=0.30
    ),
    "claude-fable-5": ModelPrice(
        input=3.0, output=15.0, cache_write=3.75, cache_read=0.30
    ),
    "claude-haiku-4-5-20251001": ModelPrice(
        input=1.0, output=5.0, cache_write=1.25, cache_read=0.10
    ),
}

_PER_MILLION = 1_000_000.0


def estimate_cost(model, tokens):
    """Estimated dollars for one session, or None if the model is unpriced."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        tokens.get("input", 0) * price.input
        + tokens.get("output", 0) * price.output
        + tokens.get("cache_creation", 0) * price.cache_write
        + tokens.get("cache_read", 0) * price.cache_read
    ) / _PER_MILLION
