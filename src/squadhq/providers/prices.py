"""Model catalogue: prices, capabilities and per-model request tweaks.

Backed by ``prices.yml`` next to this module (override the path with
``SQUADHQ_PRICES_FILE``). Lookups never raise for an unknown model: prices
come back ``None`` and a single warning is logged per (provider, model).
"""
from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from squadhq.interfaces import Capability, Usage

log = logging.getLogger(__name__)

_MTOK = Decimal(1_000_000)
_warned: set[tuple[str, str]] = set()


@lru_cache(maxsize=4)
def _load(path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    else:
        text = resources.files("squadhq.providers").joinpath("prices.yml").read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("prices.yml: top level must be a mapping")
    return data


def catalogue() -> dict[str, Any]:
    """The parsed catalogue (cached; call ``reload()`` after editing the file)."""
    return _load(os.environ.get("SQUADHQ_PRICES_FILE") or None)


def reload() -> None:
    _load.cache_clear()
    _warned.clear()


def _provider_section(provider: str) -> dict[str, Any]:
    sec = catalogue().get("providers", {}).get(provider)
    return sec if isinstance(sec, dict) else {}


_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")
_BEDROCK_SUFFIX = re.compile(r"-v\d+(?::\d+)?$")


def _aliases(provider: str, model: str) -> list[tuple[str, str]]:
    """Other (provider, model) keys to try when ``model`` has no own entry."""
    out: list[tuple[str, str]] = []
    if provider == "bedrock" and "anthropic." in model:
        bare = _BEDROCK_SUFFIX.sub("", _BEDROCK_PREFIX.sub("", model))
        out.append(("anthropic", bare))
    if provider == "azure_openai":
        mapping = os.environ.get("AZURE_OPENAI_DEPLOYMENT_MODELS", "")
        for pair in mapping.split(","):
            dep, _, mdl = pair.partition("=")
            if dep.strip() == model and mdl.strip():
                out.append(("openai", mdl.strip()))
        out.append(("openai", model))
    if provider in ("openrouter", "together") and "/" in model:
        vendor, _, rest = model.partition("/")
        if vendor in ("anthropic", "openai"):
            out.append((vendor, rest))
    return out


def model_entry(provider: str, model: str) -> dict[str, Any] | None:
    """The catalogue entry for a model (aliases and ``"*"`` wildcard applied)."""
    for prov, mdl in [(provider, model), *_aliases(provider, model)]:
        models = _provider_section(prov).get("models") or {}
        entry = models.get(mdl)
        if isinstance(entry, dict):
            return entry
    wildcard = (_provider_section(provider).get("models") or {}).get("*")
    return wildcard if isinstance(wildcard, dict) else None


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def rates(provider: str, model: str, input_tokens: int = 0) -> tuple[float, float] | None:
    """``(input, output)`` $/MTok applicable at this prompt size, or ``None`` if unpriced."""
    e = model_entry(provider, model)
    if e is None or "input" not in e or "output" not in e:
        return None
    long = e.get("long_context")
    if isinstance(long, dict) and input_tokens > int(long.get("threshold", 0)):
        return float(long["input"]), float(long["output"])
    return float(e["input"]), float(e["output"])


def price(provider: str, model: str, usage: Usage) -> Decimal | None:
    """Cost of one call in USD, or ``None`` (with a one-time warning) for an unknown model."""
    e = model_entry(provider, model)
    if e is None or "input" not in e or "output" not in e:
        if (provider, model) not in _warned:
            _warned.add((provider, model))
            log.warning("no price for %s/%s in prices.yml; cost recorded as unknown", provider, model)
        return None
    inp, out = _dec(e["input"]), _dec(e["output"])
    long = e.get("long_context")
    if isinstance(long, dict) and usage.input_tokens > int(long.get("threshold", 0)):
        inp, out = _dec(long["input"]), _dec(long["output"])
    cr = _dec(e["cache_read"]) if "cache_read" in e else inp
    cw = _dec(e["cache_write"]) if "cache_write" in e else inp
    total = (
        Decimal(usage.input_tokens) * inp
        + Decimal(usage.output_tokens) * out
        + Decimal(usage.cache_read_tokens) * cr
        + Decimal(usage.cache_write_tokens) * cw
    ) / _MTOK
    return total


def capabilities(provider: str, model: str) -> set[Capability]:
    e = model_entry(provider, model) or {}
    names = e.get("capabilities")
    if names is None:
        names = (_provider_section(provider).get("defaults") or {}).get("capabilities", [])
    out: set[Capability] = set()
    for n in names or []:
        try:
            out.add(Capability(n))
        except ValueError:
            log.warning("prices.yml: unknown capability %r for %s/%s", n, provider, model)
    return out


def setting(provider: str, model: str, key: str) -> Any:
    """A per-model request tweak (``thinking_off``, ``reasoning_off`` ...) or ``None``."""
    e = model_entry(provider, model)
    return None if e is None else e.get(key)


def models(provider: str) -> dict[str, dict[str, Any]]:
    """Every explicitly listed model for a provider (no aliases, no wildcard)."""
    m = _provider_section(provider).get("models") or {}
    return {k: v for k, v in m.items() if k != "*" and isinstance(v, dict)}
