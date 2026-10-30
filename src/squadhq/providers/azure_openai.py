"""Azure OpenAI adapter (Chat Completions on a deployment; optional v1 Responses API).

``model`` is the **deployment name**. Env:

* ``AZURE_OPENAI_ENDPOINT`` (required), e.g. ``https://myres.openai.azure.com``
* ``AZURE_OPENAI_API_KEY`` (``api-key`` header) or ``AZURE_OPENAI_AD_TOKEN`` (Entra ID bearer)
* ``AZURE_OPENAI_API_VERSION`` (default ``2024-10-21``)
* ``AZURE_OPENAI_API_STYLE`` = ``chat`` (default) | ``responses`` (``/openai/v1/responses``)
* ``AZURE_OPENAI_DEPLOYMENT_MODELS`` = ``"deploy-a=gpt-4.1,deploy-b=gpt-4o-mini"`` for price lookup
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from squadhq.http import Transport
from squadhq.interfaces import LLMReply, Message
from squadhq.providers.base import env, message_content
from squadhq.providers.openai import ApiStyle, OpenAIProvider, parse_responses

DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIProvider(OpenAIProvider):
    name = "azure_openai"
    key_envs = ("AZURE_OPENAI_API_KEY",)
    base_url_envs = ("AZURE_OPENAI_ENDPOINT",)
    default_base_url = ""
    default_style = "chat"

    def __init__(self, *, endpoint: str | None = None, api_key: str | None = None,
                 ad_token: str | None = None, api_version: str | None = None,
                 api_style: ApiStyle | None = None, transport: Transport | None = None) -> None:
        style = api_style or ("responses" if env("AZURE_OPENAI_API_STYLE") == "responses" else "chat")
        super().__init__(api_key=api_key, base_url=endpoint, api_style=style, transport=transport)
        self.ad_token = ad_token
        self.api_version = api_version or env("AZURE_OPENAI_API_VERSION", default=DEFAULT_API_VERSION) or DEFAULT_API_VERSION

    def _headers(self) -> dict[str, str]:
        self._require(self.base_url, "AZURE_OPENAI_ENDPOINT")
        h = {"Content-Type": "application/json"}
        token = self.ad_token or env("AZURE_OPENAI_AD_TOKEN")
        key = self.api_key or env("AZURE_OPENAI_API_KEY")
        if key:
            h["api-key"] = key
        elif token:
            h["Authorization"] = f"Bearer {token}"
        else:
            self._require(None, "AZURE_OPENAI_API_KEY or AZURE_OPENAI_AD_TOKEN")
        return h

    def _chat_url(self, model: str) -> str:
        dep = urllib.parse.quote(model, safe="")
        return f"{self.base_url}/openai/deployments/{dep}/chat/completions?api-version={self.api_version}"

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
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        if web_search:
            body["tools"] = [{"type": "web_search_preview"}]
        resp = self._post(f"{self.base_url}/openai/v1/responses?api-version=preview", body, self._headers(), timeout)
        return parse_responses(resp if isinstance(resp, dict) else {}, model, self.name)
