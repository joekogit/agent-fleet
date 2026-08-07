"""Model pricing.

Transcripts record token counts but no dollar amounts, so every cost this
dashboard shows is an ESTIMATE derived from the table below.

Rates below were taken from https://platform.claude.com/docs/en/about-claude/pricing
on 2026-08-07, in US dollars per million tokens. **They go stale whenever
pricing changes.** Re-check them against that page before trusting any figure
the dashboard reports; nothing here can detect that they have drifted.

Cache writes have two tiers — a 5-minute TTL at 1.25x base input and a 1-hour
TTL at 2x — so they are priced separately rather than averaged. Claude Code
sessions normally use the 1-hour TTL, and the transcripts record which is
which, so the distinction is worth keeping: collapsing them mis-prices cache
creation by 60%.

An unrecognised model yields None rather than a plausible-looking wrong figure.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ModelPrice:
    input: float           # $ per 1M input tokens
    output: float          # $ per 1M output tokens
    cache_write_5m: float  # $ per 1M cache-creation tokens, 5-minute TTL
    cache_write_1h: float  # $ per 1M cache-creation tokens, 1-hour TTL
    cache_read: float      # $ per 1M cache-read tokens


def _tiers(base, output):
    """Cache tiers are fixed multiples of the base input price."""
    return ModelPrice(
        input=base,
        output=output,
        cache_write_5m=base * 1.25,
        cache_write_1h=base * 2.0,
        cache_read=base * 0.1,
    )


# NOTE: Claude Sonnet 5 is on introductory pricing ($2/$10) through
# 2026-08-31; from 2026-09-01 it becomes $3/$15 like Sonnet 4.6. Update the
# line below on that date — this table has no notion of time.
PRICES = {
    "claude-fable-5": _tiers(10.0, 50.0),
    "claude-mythos-5": _tiers(10.0, 50.0),

    "claude-opus-5": _tiers(5.0, 25.0),
    "claude-opus-4-8": _tiers(5.0, 25.0),
    "claude-opus-4-7": _tiers(5.0, 25.0),
    "claude-opus-4-6": _tiers(5.0, 25.0),
    "claude-opus-4-5": _tiers(5.0, 25.0),
    "claude-opus-4-1": _tiers(15.0, 75.0),      # retired
    "claude-opus-4": _tiers(15.0, 75.0),        # retired

    "claude-sonnet-5": _tiers(2.0, 10.0),       # introductory, see note above
    "claude-sonnet-4-6": _tiers(3.0, 15.0),
    "claude-sonnet-4-5": _tiers(3.0, 15.0),
    "claude-sonnet-4": _tiers(3.0, 15.0),       # retired

    "claude-haiku-4-5": _tiers(1.0, 5.0),
    "claude-haiku-3-5": _tiers(0.8, 4.0),       # retired
}

_PER_MILLION = 1_000_000.0
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model(model):
    """Strip a dated snapshot suffix: claude-haiku-4-5-20251001 -> claude-haiku-4-5.

    Transcripts carry both bare and dated model ids for the same model, and a
    dated id that is missing from the table would silently render no cost.
    """
    if not model:
        return None
    return _DATE_SUFFIX.sub("", model.strip())


def estimate_cost(model, tokens):
    """Estimated dollars for one session, or None if the model is unpriced.

    `tokens` uses the wire-format keys built in `model.build_card`. Cache
    creation is split: `cache_creation` is the total and `cache_creation_1h`
    is the portion written with the 1-hour TTL, so the 5-minute portion is
    whatever is left over.
    """
    price = PRICES.get(normalize_model(model))
    if price is None:
        return None

    total_write = tokens.get("cache_creation", 0)
    write_1h = min(tokens.get("cache_creation_1h", 0), total_write)
    write_5m = max(0, total_write - write_1h)

    return (
        tokens.get("input", 0) * price.input
        + tokens.get("output", 0) * price.output
        + write_5m * price.cache_write_5m
        + write_1h * price.cache_write_1h
        + tokens.get("cache_read", 0) * price.cache_read
    ) / _PER_MILLION
