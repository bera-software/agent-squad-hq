"""AWS Bedrock Runtime adapter (Converse API), signed with stdlib SigV4.

Env: ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` (default ``us-east-1``),
``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY`` (+ ``AWS_SESSION_TOKEN``),
or ``AWS_BEARER_TOKEN_BEDROCK`` (Bedrock API key; sent as a bearer token,
no signing). ``BEDROCK_ENDPOINT_URL`` overrides the runtime endpoint (VPC
endpoints). No boto3 needed; instance-profile / SSO credential discovery is
not implemented — export the variables (e.g. ``aws configure export-credentials``).

``model`` is a Bedrock model id or inference-profile id
(``us.anthropic.claude-sonnet-4-6-v1:0``). Web search is not available on
Converse and is ignored with a warning.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

from squadhq import http
from squadhq.http import Transport
from squadhq.interfaces import AdapterError, LLMReply, Message
from squadhq.providers import sigv4
from squadhq.providers.base import HTTPProvider, env, usage_of, warn_unsupported

_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "tool_use": "tool_use",
    "guardrail_intervened": "refusal",
    "content_filtered": "refusal",
}


def parse_reply(resp: dict[str, Any], model: str) -> LLMReply:
    content = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    text = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and "text" in c)
    raw_stop = str(resp.get("stopReason") or "")
    u = resp.get("usage") or {}
    usage = usage_of(u.get("inputTokens"), u.get("outputTokens"),
                     u.get("cacheReadInputTokens"), u.get("cacheWriteInputTokens"))
    return LLMReply(text=text, usage=usage, stop_reason=_STOP.get(raw_stop, "other"), model=model,
                    provider="bedrock", raw_stop_reason=raw_stop, raw=resp)


class BedrockProvider(HTTPProvider):
    name = "bedrock"

    def __init__(self, *, region: str | None = None, access_key: str | None = None,
                 secret_key: str | None = None, session_token: str | None = None,
                 bearer_token: str | None = None, endpoint_url: str | None = None,
                 transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
        self.region = region or env("AWS_REGION", "AWS_DEFAULT_REGION", default="us-east-1") or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.bearer_token = bearer_token
        self.endpoint_url = (endpoint_url or env("BEDROCK_ENDPOINT_URL")
                             or f"https://bedrock-runtime.{self.region}.amazonaws.com").rstrip("/")

    def complete(self, *, model: str, system: str, messages: list[Message], max_tokens: int,
                 web_search: bool = False, json_mode: bool = False, timeout: float = 120.0) -> LLMReply:
        if web_search:
            warn_unsupported(self.name, "web_search", model)
        body: dict[str, Any] = {
            "messages": [{"role": m.role, "content": [{"text": m.content}]} for m in messages],
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            body["system"] = [{"text": system}]
        url = f"{self.endpoint_url}/model/{urllib.parse.quote(model, safe='')}/converse"
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        bearer = self.bearer_token or env("AWS_BEARER_TOKEN_BEDROCK")
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        else:
            ak = self._require(self.access_key or env("AWS_ACCESS_KEY_ID"), "AWS_ACCESS_KEY_ID")
            sk = self._require(self.secret_key or env("AWS_SECRET_ACCESS_KEY"), "AWS_SECRET_ACCESS_KEY")
            headers.update(sigv4.sign("POST", url, headers, data, access_key=ak, secret_key=sk,
                                      region=self.region, service="bedrock",
                                      session_token=self.session_token or env("AWS_SESSION_TOKEN")))
        try:
            resp = http.request("POST", url, headers=headers, data=data, timeout=timeout, retries=0,
                                transport=self.transport).json() or {}
        except AdapterError as e:
            if "ResourceNotFound" in e.body or "ModelNotReady" in e.body or "model identifier" in e.body.lower():
                e.model_specific = True
            e.args = (f"bedrock: {e.args[0] if e.args else ''}",)
            raise
        return parse_reply(resp if isinstance(resp, dict) else {}, model)
