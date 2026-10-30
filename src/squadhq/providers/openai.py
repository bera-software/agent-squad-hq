"""OpenAI adapter: Responses API (default) or Chat Completions.

Env: ``OPENAI_API_KEY`` (required), ``OPENAI_BASE_URL`` (default
``https://api.openai.com/v1``), ``OPENAI_ORG_ID`` / ``OPENAI_PROJECT_ID``
(optional headers), ``OPENAI_API_STYLE`` = ``responses`` | ``chat``.

* Responses API: ``web_search=True`` adds the ``web_search`` tool;
  ``json_mode`` sets ``text.format = json_object``; reasoning models with a
  ``reasoning_off`` catalogue entry get ``reasoning.effort`` at that level so
  hidden reasoning does not eat ``max_output_tokens``.
* Chat Completions (also used by every OpenAI-compatible server): no web
  search (ignored with a warning); ``json_mode`` sets ``response_format``.

Usage is normalised so ``input_tokens`` excludes cached tokens
(``cache_read_tokens`` carries them).
"""
from __future__ import annotations

from typing import Any, Literal

from squadhq.http import Transport
from squadhq.interfaces import LLMReply, Message
from squadhq.providers import prices
from squadhq.providers.base import HTTPProvider, env, message_content, usage_of, warn_unsupported

ApiStyle = Literal["responses", "chat"]

_CHAT_STOP = {
    "stop": "end",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def parse_responses(resp: dict[str, Any], model: str, provider: str) -> LLMReply:
    texts: list[str] = []
    refused = False
    tool = False
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "output_text":
                    texts.append(str(c.get("text", "")))
                elif c.get("type") == "refusal":
                    refused = True
        elif itype in ("function_call", "custom_tool_call"):
            tool = True
    status = str(resp.get("status") or "")
    reason = str((resp.get("incomplete_details") or {}).get("reason") or "")
    if status == "incomplete" and reason in ("max_output_tokens", "max_tokens"):
        stop = "max_tokens"
    elif refused or reason == "content_filter":
        stop = "refusal"
    elif tool:
        stop = "tool_use"
    elif status == "completed":
        stop = "end"
    else:
        stop = "other"
    u = resp.get("usage") or {}
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
    usage = usage_of((u.get("input_tokens") or 0) - cached, u.get("output_tokens"), cached)
    return LLMReply(text="".join(texts), usage=usage, stop_reason=stop, model=model, provider=provider,
                    raw_stop_reason=reason or status, raw=resp)


def parse_chat(resp: dict[str, Any], model: str, provider: str) -> LLMReply:
    choices = resp.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):  # some compatible servers return parts
        text = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    else:
        text = str(content or "")
    raw_stop = str(first.get("finish_reason") or "")
    stop = _CHAT_STOP.get(raw_stop, "other")
    if msg.get("refusal"):
        stop = "refusal"
    u = resp.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = usage_of((u.get("prompt_tokens") or 0) - cached, u.get("completion_tokens"), cached)
    return LLMReply(text=text, usage=usage, stop_reason=stop, model=model, provider=provider,
                    raw_stop_reason=raw_stop, raw=resp)


class OpenAIProvider(HTTPProvider):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    key_envs: tuple[str, ...] = ("OPENAI_API_KEY",)
    base_url_envs: tuple[str, ...] = ("OPENAI_BASE_URL",)
    key_required = True
    default_style: ApiStyle = "responses"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 api_style: ApiStyle | None = None, extra_headers: dict[str, str] | None = None,
                 transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
        self.api_key = api_key
        self.base_url = (base_url or env(*self.base_url_envs, default=self.default_base_url) or "").rstrip("/")
        style = api_style or (env("OPENAI_API_STYLE") if self.name == "openai" else None)
        self.api_style: ApiStyle = "chat" if (style or self.default_style) == "chat" else "responses"
        self.extra_headers = dict(extra_headers or {})

    def _headers(self) -> dict[str, str]:
        key = self.api_key or env(*self.key_envs)
        if self.key_required:
            key = self._require(key, " / ".join(self.key_envs))
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if self.name == "openai":
            if org := env("OPENAI_ORG_ID"):
                h["OpenAI-Organization"] = org
            if proj := env("OPENAI_PROJECT_ID"):
                h["OpenAI-Project"] = proj
        h.update(self.extra_headers)
        return h

    def _reasoning_effort(self, model: str) -> str | None:
        v = prices.setting(self._cat, model, "reasoning_off")
        return str(v) if v else None

    def complete(self, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 web_search: bool = False, json_mode: bool = False, timeout: float = 120.0) -> LLMReply:
        if self.api_style == "chat":
            return self._chat(model, system, messages, max_tokens, web_search, json_mode, timeout)
        body: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": [{"role": m.role, "content": message_content(m, "openai_responses")} for m in messages],
            "max_output_tokens": max_tokens,
        }
        if web_search:
            body["tools"] = [{"type": "web_search"}]
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        if effort := self._reasoning_effort(model):
            body["reasoning"] = {"effort": effort}
        resp = self._post(f"{self.base_url}/responses", body, self._headers(), timeout)
        return parse_responses(resp if isinstance(resp, dict) else {}, model, self.name)

    def _chat_url(self, model: str) -> str:
        return f"{self.base_url}/chat/completions"

    def _chat_body(self, model: str, system: str, messages: list[Message], max_tokens: int,
                   json_mode: bool) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend({"role": m.role, "content": message_content(m, "openai_chat")} for m in messages)
        body: dict[str, Any] = {"model": model, "messages": msgs}
        if self.name in ("openai", "azure_openai"):
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.name in ("openai", "azure_openai") and (effort := self._reasoning_effort(model)):
            body["reasoning_effort"] = effort
        return body

    def _chat(self, model: str, system: str, messages: list[Message], max_tokens: int,
              web_search: bool, json_mode: bool, timeout: float) -> LLMReply:
        if web_search:
            warn_unsupported(self.name, "web_search (chat completions)", model)
        body = self._chat_body(model, system, messages, max_tokens, json_mode)
        resp = self._post(self._chat_url(model), body, self._headers(), timeout)
        return parse_chat(resp if isinstance(resp, dict) else {}, model, self.name)
