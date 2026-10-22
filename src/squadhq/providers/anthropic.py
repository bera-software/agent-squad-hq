"""Anthropic Messages API adapter.

Env: ``ANTHROPIC_API_KEY`` (required), ``ANTHROPIC_BASE_URL`` (default
``https://api.anthropic.com``), ``ANTHROPIC_VERSION`` (default ``2023-06-01``).

* ``web_search=True`` attaches the server-side ``web_search`` tool.
* Models with a ``thinking_off`` entry in ``prices.yml`` are sent that
  ``thinking`` value (adaptive thinking counts against ``max_tokens`` — #46).
  Models without one get no ``thinking`` field (a wrong value is a 400).
* ``json_mode`` has no request flag here; the prompt asks for JSON.
"""
from __future__ import annotations

from typing import Any

from squadhq.http import Transport
from squadhq.interfaces import LLMReply, Message
from squadhq.providers import prices
from squadhq.providers.base import HTTPProvider, env, usage_of

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}

_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "tool_use": "tool_use",
    "refusal": "refusal",
}


def thinking_off(model: str) -> dict[str, Any] | None:
    v = prices.setting("anthropic", model, "thinking_off")
    return dict(v) if isinstance(v, dict) else None


def build_request(model: str, system: str, messages: list[Message], max_tokens: int,
                  web_search: bool = False) -> dict[str, Any]:
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    if web_search:
        req["tools"] = [dict(WEB_SEARCH_TOOL)]
    t = thinking_off(model)
    if t is not None:
        req["thinking"] = t
    return req


def parse_reply(resp: dict[str, Any], model: str, provider: str = "anthropic") -> LLMReply:
    blocks = resp.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    raw_stop = resp.get("stop_reason") or ""
    u = resp.get("usage") or {}
    usage = usage_of(u.get("input_tokens"), u.get("output_tokens"),
                     u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"))
    return LLMReply(text=text, usage=usage, stop_reason=_STOP.get(raw_stop, "other"),
                    model=model, provider=provider, raw_stop_reason=raw_stop, raw=resp)


class AnthropicProvider(HTTPProvider):
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
        self.api_key = api_key
        self.base_url = (base_url or env("ANTHROPIC_BASE_URL", default="https://api.anthropic.com") or "").rstrip("/")

    def complete(self, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 web_search: bool = False, json_mode: bool = False, timeout: float = 120.0) -> LLMReply:
        key = self._require(self.api_key or env("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY")
        headers = {
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": env("ANTHROPIC_VERSION", default="2023-06-01") or "2023-06-01",
        }
        req = build_request(model, system, messages, max_tokens, web_search)
        resp = self._post(f"{self.base_url}/v1/messages", req, headers, timeout)
        return parse_reply(resp if isinstance(resp, dict) else {}, model, self.name)
