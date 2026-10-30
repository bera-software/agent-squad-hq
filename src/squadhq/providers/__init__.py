"""LLM provider registry for hub loops (single prompt -> single reply).

Model refs are ``"provider/model"`` (``"openai/gpt-4.1"``,
``"ollama/llama3.1:8b"``, ``"openrouter/anthropic/claude-sonnet-4"``) or a
bare model id, which uses ``SQUADHQ_DEFAULT_PROVIDER`` (default
``anthropic``) — so every existing ``config/models.json`` entry
(``"claude-sonnet-5-5"``) keeps working.

Public API::

    resolve(ref) -> (provider_name, model)
    get_provider(name, **kwargs) -> LLMProvider
    available_providers() -> list[str]
    price_for(ref, usage) -> Decimal | None
    capabilities_for(ref) -> set[Capability]
    complete(ref, *, system, messages, max_tokens, ...) -> LLMReply
    stream_reply(provider, *, model, system, messages, ...) -> Iterator[StreamEvent]
    complete_with_fallback(primary_ref, fallback_ref, *, system, messages, max_tokens, ...) -> FallbackResult
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

from squadhq.interfaces import Capability, FatalError, LLMProvider, LLMReply, Message, StreamEvent, Usage
from squadhq.providers import prices
from squadhq.providers.fallback import FallbackResult, complete_with_fallback

__all__ = [
    "resolve",
    "get_provider",
    "register",
    "available_providers",
    "price_for",
    "capabilities_for",
    "complete",
    "stream_reply",
    "complete_with_fallback",
    "FallbackResult",
    "default_provider",
    "configured_kwargs",
]


def _anthropic(**kw: Any) -> LLMProvider:
    from squadhq.providers.anthropic import AnthropicProvider

    return AnthropicProvider(**kw)


def _openai(**kw: Any) -> LLMProvider:
    from squadhq.providers.openai import OpenAIProvider

    return OpenAIProvider(**kw)


def _azure(**kw: Any) -> LLMProvider:
    from squadhq.providers.azure_openai import AzureOpenAIProvider

    return AzureOpenAIProvider(**kw)


def _gemini(**kw: Any) -> LLMProvider:
    from squadhq.providers.gemini import GeminiProvider

    return GeminiProvider(**kw)


def _bedrock(**kw: Any) -> LLMProvider:
    from squadhq.providers.bedrock import BedrockProvider

    return BedrockProvider(**kw)


def _compat(cls_name: str) -> Callable[..., LLMProvider]:
    def make(**kw: Any) -> LLMProvider:
        from squadhq.providers import openai_compatible

        cls: Callable[..., LLMProvider] = getattr(openai_compatible, cls_name)
        return cls(**kw)

    return make


_REGISTRY: dict[str, Callable[..., LLMProvider]] = {
    "anthropic": _anthropic,
    "openai": _openai,
    "azure_openai": _azure,
    "gemini": _gemini,
    "bedrock": _bedrock,
    "openai_compatible": _compat("OpenAICompatibleProvider"),
    "ollama": _compat("OllamaProvider"),
    "vllm": _compat("VLLMProvider"),
    "lmstudio": _compat("LMStudioProvider"),
    "openrouter": _compat("OpenRouterProvider"),
    "together": _compat("TogetherProvider"),
}
_ALIASES = {"azure": "azure_openai", "google": "gemini", "aws": "bedrock", "claude": "anthropic",
            "lm_studio": "lmstudio", "openai-compatible": "openai_compatible"}


def register(name: str, factory: Callable[..., LLMProvider]) -> None:
    """Register a custom provider factory (plugins, tests)."""
    _REGISTRY[name] = factory


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def _canonical(name: str) -> str:
    n = name.strip().lower()
    return _ALIASES.get(n, n)


def default_provider() -> str:
    return _canonical(os.environ.get("SQUADHQ_DEFAULT_PROVIDER") or "anthropic")


def resolve(model_ref: str) -> tuple[str, str]:
    """Split ``"provider/model"`` into ``(provider, model)``; a bare id uses the default provider.

    The prefix only counts as a provider when it names a registered one, so
    ids that contain ``/`` themselves still work bare.
    """
    ref = model_ref.strip()
    if not ref:
        raise FatalError("empty model reference")
    head, sep, rest = ref.partition("/")
    if sep and rest and _canonical(head) in _REGISTRY:
        return _canonical(head), rest
    return default_provider(), ref


# config/auth.json ``providers.<name>`` (rendered from squadhq.yml) -> adapter
# constructor keyword. ``secret`` names an environment variable; its value is
# passed only when that variable is set (otherwise the adapter's own env lookup applies).
_BASE_URL_KWARG = {"azure_openai": "endpoint", "bedrock": "endpoint_url"}
_SECRET_KWARG = {"bedrock": "bearer_token"}


def configured_kwargs(name: str, auth_path: str | None = None) -> dict[str, Any]:
    """Constructor kwargs for ``name`` from the hub's ``config/auth.json``
    (``base_url``, ``region``, ``secret``). Missing or unreadable file -> ``{}``."""
    import json

    from squadhq.paths import hub_path

    key = _canonical(name)
    try:
        with open(auth_path or hub_path("config", "auth.json"), encoding="utf-8") as f:
            entry = ((json.load(f) or {}).get("providers") or {}).get(key) or {}
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(entry, dict):
        return {}
    out: dict[str, Any] = {}
    if entry.get("base_url"):
        out[_BASE_URL_KWARG.get(key, "base_url")] = str(entry["base_url"])
    if entry.get("region") and key == "bedrock":
        out["region"] = str(entry["region"])
    secret = entry.get("secret")
    if secret and os.environ.get(str(secret)):
        out[_SECRET_KWARG.get(key, "api_key")] = os.environ[str(secret)]
    return out


def get_provider(name: str, **kwargs: Any) -> LLMProvider:
    """Instantiate a provider adapter by registry name. Settings from
    ``config/auth.json`` (``configured_kwargs``) apply first; explicit
    ``kwargs`` win."""
    key = _canonical(name)
    factory = _REGISTRY.get(key)
    if factory is None:
        raise FatalError(f"unknown LLM provider {name!r}; known: {', '.join(available_providers())}",
                         provider_wide=True)
    return factory(**{**configured_kwargs(key), **kwargs})


def price_for(model_ref: str, usage: Usage) -> Decimal | None:
    """Price one call by model ref from ``prices.yml`` (``None`` if unknown, never raises)."""
    try:
        prov, model = resolve(model_ref)
    except FatalError:
        return None
    return prices.price(prov, model, usage)


def capabilities_for(model_ref: str) -> set[Capability]:
    prov, model = resolve(model_ref)
    return prices.capabilities(prov, model)


def complete(model_ref: str, *, system: str, messages: list[Message], max_tokens: int,
             web_search: bool = False, json_mode: bool = False, timeout: float = 120.0,
             **provider_kwargs: Any) -> LLMReply:
    """One call, no retry/fallback."""
    prov, model = resolve(model_ref)
    return get_provider(prov, **provider_kwargs).complete(
        model=model, system=system, messages=messages, max_tokens=max_tokens,
        web_search=web_search, json_mode=json_mode, timeout=timeout)


def stream_reply(provider: LLMProvider, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 json_mode: bool = False, timeout: float = 120.0) -> Iterator[StreamEvent]:
    """Text deltas as they arrive, then one event carrying the whole reply. Providers without a
    native stream answer in one delta, so every provider can be streamed."""
    native = getattr(provider, "stream", None)
    if callable(native):
        yield from native(model=model, system=system, messages=messages, max_tokens=max_tokens,
                          json_mode=json_mode, timeout=timeout)
        return
    reply = provider.complete(model=model, system=system, messages=messages, max_tokens=max_tokens,
                              json_mode=json_mode, timeout=timeout)
    if reply.text:
        yield StreamEvent(text=reply.text)
    yield StreamEvent(reply=reply)
