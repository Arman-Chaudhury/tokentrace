"""Versioned price table.

Prices are USD per million tokens, Anthropic first-party API list prices.
``PRICING_VERSION`` is stored alongside every computed cost so old reports can
be recomputed when the table changes.  Cache-write and cache-read rates that
Anthropic has not published per model use the standard multipliers
(write = 1.25x input, read = 0.10x input); those rows are marked ``assumed``.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_VERSION = "2026-10-06"


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float
    assumed_cache: bool = False


def _std(inp: float, out: float, cache_read: float | None = None) -> Price:
    return Price(
        input=inp,
        output=out,
        cache_read=cache_read if cache_read is not None else round(inp * 0.10, 4),
        cache_write=round(inp * 1.25, 4),
        assumed_cache=cache_read is None,
    )


# Keys are model-id prefixes.  Lookup strips "[1m]" and trailing date suffixes
# and then tries progressively shorter dash-separated prefixes.
PRICES: dict[str, Price] = {
    "claude-fable-5-1": _std(10.00, 50.00, cache_read=0.25),
    "claude-mythos-5-1": _std(10.00, 50.00, cache_read=0.25),
    "claude-fable-5": _std(10.00, 50.00),
    "claude-opus-5-5": _std(4.00, 20.00, cache_read=0.20),
    "claude-opus-5": _std(5.00, 25.00),
    "claude-opus-4-8": _std(5.00, 25.00),
    "claude-opus-4-7": _std(5.00, 25.00),
    "claude-opus-4-6": _std(5.00, 25.00),
    "claude-opus-4-5": _std(5.00, 25.00),
    "claude-sonnet-5-5": _std(2.00, 10.00, cache_read=0.20),
    "claude-sonnet-5": _std(2.00, 10.00),
    "claude-sonnet-4-6": _std(3.00, 15.00),
    "claude-sonnet-4-5": _std(3.00, 15.00),
    "claude-haiku-5-5": _std(0.10, 0.50),
    "claude-haiku-4-5": _std(1.00, 5.00),
}

FREE_MODELS = {"<synthetic>", "", None}


def normalize_model(model: str | None) -> str:
    if not model:
        return ""
    m = model.strip()
    if m.endswith("[1m]"):
        m = m[:-4]
    parts = m.split("-")
    # drop a trailing 8-digit date snapshot like 20251001
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts = parts[:-1]
    return "-".join(parts)


def lookup(model: str | None) -> Price | None:
    if model in FREE_MODELS:
        return Price(0, 0, 0, 0)
    key = normalize_model(model)
    parts = key.split("-")
    while parts:
        cand = "-".join(parts)
        if cand in PRICES:
            return PRICES[cand]
        parts.pop()
    return None


def cost_usd(model: str | None, input_tokens: int, cache_read: int, cache_write: int, output: int) -> float | None:
    """Return the USD cost, or None when the model is not in the table."""
    p = lookup(model)
    if p is None:
        return None
    return (
        input_tokens * p.input
        + cache_read * p.cache_read
        + cache_write * p.cache_write
        + output * p.output
    ) / 1_000_000
