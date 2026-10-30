"""Google Gemini adapter (Generative Language API ``generateContent``).

Env: ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``), ``GEMINI_BASE_URL`` (default
``https://generativelanguage.googleapis.com/v1beta``). For Vertex AI point
``GEMINI_BASE_URL`` at the publisher endpoint and set ``GEMINI_BEARER_TOKEN``
(an OAuth access token, e.g. from ``gcloud auth print-access-token``).

* ``web_search=True`` enables ``google_search`` grounding.
* ``json_mode`` sets ``responseMimeType: application/json``.
* Models with ``thinking_off`` in the catalogue get that ``thinkingConfig``.
* Thought parts are dropped from the text; thought tokens are billed as output.
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from squadhq.http import Transport
from squadhq.interfaces import LLMReply, Message
from squadhq.providers import prices
from squadhq.providers.base import HTTPProvider, env, message_content, usage_of

_STOP = {
    "STOP": "end",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
    "IMAGE_SAFETY": "refusal",
    "MALFORMED_FUNCTION_CALL": "other",
}


def parse_reply(resp: dict[str, Any], model: str) -> LLMReply:
    cands = resp.get("candidates") or []
    first = cands[0] if cands and isinstance(cands[0], dict) else {}
    parts = (first.get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and not p.get("thought"))
    tool = any(isinstance(p, dict) and "functionCall" in p for p in parts)
    raw_stop = str(first.get("finishReason") or "")
    if not cands and (resp.get("promptFeedback") or {}).get("blockReason"):
        raw_stop = str(resp["promptFeedback"]["blockReason"])
        stop = "refusal"
    else:
        stop = "tool_use" if tool else _STOP.get(raw_stop, "other")
    u = resp.get("usageMetadata") or {}
    cached = u.get("cachedContentTokenCount") or 0
    out = (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0)
    usage = usage_of((u.get("promptTokenCount") or 0) - cached, out, cached)
    return LLMReply(text=text, usage=usage, stop_reason=stop, model=model, provider="gemini",
                    raw_stop_reason=raw_stop, raw=resp)


class GeminiProvider(HTTPProvider):
    name = "gemini"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 bearer_token: str | None = None, transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.base_url = (base_url or env("GEMINI_BASE_URL",
                                         default="https://generativelanguage.googleapis.com/v1beta") or "").rstrip("/")

    def complete(self, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 web_search: bool = False, json_mode: bool = False, timeout: float = 120.0) -> LLMReply:
        headers = {"Content-Type": "application/json"}
        token = self.bearer_token or env("GEMINI_BEARER_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["x-goog-api-key"] = self._require(self.api_key or env("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                                                      "GEMINI_API_KEY")
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        t = prices.setting("gemini", model, "thinking_off")
        if isinstance(t, dict):
            gen["thinkingConfig"] = dict(t)
        body: dict[str, Any] = {
            "contents": [{"role": "model" if m.role == "assistant" else "user",
                          "parts": message_content(m, "gemini")} for m in messages],
            "generationConfig": gen,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if web_search:
            body["tools"] = [{"google_search": {}}]
        url = f"{self.base_url}/models/{urllib.parse.quote(model, safe='')}:generateContent"
        resp = self._post(url, body, headers, timeout)
        return parse_reply(resp if isinstance(resp, dict) else {}, model)
