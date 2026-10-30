"""Provider adapters against recorded fixtures (no network)."""
from __future__ import annotations

import datetime
import json
from decimal import Decimal
from pathlib import Path

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import Capability, CapacityError, FatalError, LLMProvider, Message, TransientError, Usage
from squadhq.providers import get_provider, sigv4
from squadhq.providers.anthropic import AnthropicProvider
from squadhq.providers.azure_openai import AzureOpenAIProvider
from squadhq.providers.bedrock import BedrockProvider
from squadhq.providers.gemini import GeminiProvider
from squadhq.providers.gemini import parse_reply as gemini_parse
from squadhq.providers.openai import OpenAIProvider, parse_chat, parse_responses
from squadhq.providers.openai_compatible import OllamaProvider, OpenAICompatibleProvider, OpenRouterProvider

FX = Path(__file__).parent / "fixtures"
MSGS = [Message(role="user", content="hello")]


def fx(name: str) -> dict:
    return json.loads((FX / name).read_text())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_API_STYLE", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
              "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_STYLE",
              "OPENAI_COMPATIBLE_BASE_URL", "OPENROUTER_API_KEY", "GEMINI_BEARER_TOKEN", "OPENAI_ORG_ID",
              "OPENAI_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------- anthropic

def test_anthropic_request_and_reply() -> None:
    t = FakeTransport().add("POST /v1/messages", (200, fx("anthropic_messages.json")))
    p = AnthropicProvider(api_key="sk-test", transport=t)
    assert isinstance(p, LLMProvider)
    r = p.complete(model="claude-sonnet-5-5", system="sys", messages=MSGS, max_tokens=100, web_search=True)
    body = t.calls[0]["body"]
    assert body["thinking"] == {"type": "between_tools"}
    assert body["tools"][0]["name"] == "web_search"
    assert t.calls[0]["headers"]["X-api-key"] == "sk-test"
    assert r.text == "Hello world"
    assert r.stop_reason == "end" and r.raw_stop_reason == "end_turn"
    assert r.usage == Usage(1200, 300, 100, 0)
    assert r.provider == "anthropic"


def _sse(*events: dict) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def test_anthropic_streams_text_then_the_whole_reply() -> None:
    from squadhq.providers import stream_reply

    body = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 50, "output_tokens": 1}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "x"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
                {"type": "message_stop"})
    t = FakeTransport().add("POST /v1/messages", (200, body))
    events = list(stream_reply(AnthropicProvider(api_key="sk-test", transport=t), model="claude-sonnet-5-5",
                               system="s", messages=MSGS, max_tokens=50))
    assert [e.text for e in events if e.text] == ["Hel", "lo"] and t.calls[0]["body"]["stream"] is True
    reply = events[-1].reply
    assert reply is not None and reply.text == "Hello" and reply.stop_reason == "end"
    assert reply.usage.input_tokens == 50 and reply.usage.output_tokens == 7
    overloaded = FakeTransport().add("POST /v1/messages", (200, _sse(
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})))
    with pytest.raises(TransientError):
        list(AnthropicProvider(api_key="k", transport=overloaded).stream(
            model="claude-sonnet-5-5", system="s", messages=MSGS, max_tokens=5))
    refused = FakeTransport().add("POST /v1/messages", (401, {"error": {"type": "authentication_error"}}))
    with pytest.raises(FatalError):
        list(AnthropicProvider(api_key="k", transport=refused).stream(
            model="claude-sonnet-5-5", system="s", messages=MSGS, max_tokens=5))


def test_providers_without_a_stream_answer_in_one_piece() -> None:
    from squadhq.providers import stream_reply

    t = FakeTransport().add("POST /chat/completions", (200, fx("openai_chat.json")))
    events = list(stream_reply(OpenAICompatibleProvider(base_url="https://llm.test/v1", api_key="k", transport=t),
                               model="m", system="s", messages=MSGS, max_tokens=5))
    assert len(events) == 2 and events[0].text and events[-1].reply is not None
    assert events[-1].reply.text == events[0].text


def test_anthropic_no_thinking_for_legacy_model_and_missing_key() -> None:
    t = FakeTransport().add("/v1/messages", (200, {"content": [], "stop_reason": "max_tokens"}))
    p = AnthropicProvider(api_key="k", transport=t)
    r = p.complete(model="claude-sonnet-4-6", system="s", messages=MSGS, max_tokens=5)
    assert "thinking" not in t.calls[0]["body"] and "tools" not in t.calls[0]["body"]
    assert r.stop_reason == "max_tokens" and r.text == ""
    with pytest.raises(FatalError) as ei:
        AnthropicProvider(transport=t).complete(model="m", system="s", messages=MSGS, max_tokens=5)
    assert ei.value.provider_wide


@pytest.mark.parametrize(
    "status,body,cls,model_specific,provider_wide",
    [
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, TransientError, True, False),
        (503, "upstream", TransientError, False, False),
        (429, {"error": {"type": "rate_limit_error"}}, TransientError, False, False),
        (401, {"error": {"type": "authentication_error"}}, FatalError, False, True),
        (400, {"error": {"message": "Your credit balance is too low"}}, FatalError, False, True),
        (404, {"error": {"type": "not_found_error", "message": "model: claude-x"}}, FatalError, True, False),
        (400, {"error": {"message": "max_tokens: must be positive"}}, FatalError, False, False),
    ],
)
def test_anthropic_error_mapping(status: int, body: object, cls: type, model_specific: bool,
                                 provider_wide: bool) -> None:
    t = FakeTransport().add("/v1/messages", (status, body))
    with pytest.raises(cls) as ei:
        AnthropicProvider(api_key="k", transport=t).complete(model="m", system="s", messages=MSGS, max_tokens=5)
    assert ei.value.status == status
    assert ei.value.model_specific is model_specific
    assert ei.value.provider_wide is provider_wide
    assert str(ei.value).startswith("anthropic:")


def test_anthropic_price_and_caps() -> None:
    p = AnthropicProvider(api_key="k")
    assert p.price("claude-haiku-5-5", Usage(100_000, 1_000_000)) == Decimal("0.51")
    assert p.price("claude-haiku-5-5", Usage(1_000_000, 0)) == Decimal("0.50")  # long-prompt tier
    assert Capability.WEB_SEARCH in p.capabilities("claude-haiku-5-5")
    assert p.price("claude-unknown", Usage(1, 1)) is None


# ------------------------------------------------------------------ openai

def test_openai_responses_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-1")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-1")
    t = FakeTransport().add("POST /v1/responses", (200, fx("openai_responses.json")))
    p = get_provider("openai", transport=t)
    r = p.complete(model="gpt-5", system="sys", messages=MSGS, max_tokens=50, web_search=True, json_mode=True)
    call = t.calls[0]
    assert call["url"] == "https://api.openai.com/v1/responses"
    assert call["headers"]["Authorization"] == "Bearer sk-o"
    assert call["headers"]["Openai-organization"] == "org-1"
    b = call["body"]
    assert b["instructions"] == "sys" and b["max_output_tokens"] == 50
    assert b["tools"] == [{"type": "web_search"}]
    assert b["text"] == {"format": {"type": "json_object"}}
    assert b["reasoning"] == {"effort": "minimal"}
    assert r.text == '[{"type":"comment"}]' and r.stop_reason == "end"
    assert r.usage.input_tokens == 800 and r.usage.cache_read_tokens == 200


def test_openai_responses_stop_reasons() -> None:
    r = parse_responses(fx("openai_responses_incomplete.json"), "gpt-5", "openai")
    assert r.stop_reason == "max_tokens" and r.text == ""
    refusal = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    assert parse_responses(refusal, "m", "openai").stop_reason == "refusal"
    tool = {"status": "completed", "output": [{"type": "function_call", "name": "f"}, "junk"]}
    assert parse_responses(tool, "m", "openai").stop_reason == "tool_use"
    assert parse_responses({"status": "failed"}, "m", "openai").stop_reason == "other"


def test_openai_chat_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_STYLE", "chat")
    t = FakeTransport().add("/chat/completions", (200, fx("openai_chat.json")))
    p = OpenAIProvider(api_key="k", transport=t)
    r = p.complete(model="o4-mini", system="sys", messages=MSGS, max_tokens=9, json_mode=True, web_search=True)
    b = t.calls[0]["body"]
    assert b["messages"][0] == {"role": "system", "content": "sys"}
    assert b["max_completion_tokens"] == 9 and b["reasoning_effort"] == "low"
    assert b["response_format"] == {"type": "json_object"}
    assert r.text == "hi there" and r.stop_reason == "end"


def test_parse_chat_variants() -> None:
    assert parse_chat({"choices": [{"message": {"content": None}, "finish_reason": "length"}]}, "m", "x").stop_reason == "max_tokens"
    parts = {"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}, "finish_reason": "tool_calls"}]}
    r = parse_chat(parts, "m", "x")
    assert r.text == "ab" and r.stop_reason == "tool_use"
    assert parse_chat({"choices": [{"message": {"refusal": "no"}, "finish_reason": "stop"}]}, "m", "x").stop_reason == "refusal"
    assert parse_chat({}, "m", "x").text == ""


def test_openai_quota_is_capacity() -> None:
    t = FakeTransport().add("/responses", (429, {"error": {"code": "insufficient_quota", "message": "exceeded your current quota"}}))
    with pytest.raises(CapacityError):
        OpenAIProvider(api_key="k", transport=t).complete(model="gpt-4.1", system="", messages=MSGS, max_tokens=1)


# -------------------------------------------------------- openai-compatible

def test_ollama_defaults_and_free_price() -> None:
    t = FakeTransport().add("localhost:11434/v1/chat/completions", (200, fx("openai_chat.json")))
    p = OllamaProvider(transport=t)
    r = p.complete(model="llama3.1:8b", system="", messages=MSGS, max_tokens=7)
    b = t.calls[0]["body"]
    assert b["max_tokens"] == 7 and b["messages"] == [{"role": "user", "content": "hello"}]
    assert "Authorization" not in t.calls[0]["headers"]
    assert r.provider == "ollama"
    assert p.price("anything", Usage(10_000, 10_000)) == Decimal(0)


def test_openai_compatible_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FatalError):
        OpenAICompatibleProvider()
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://vllm.internal:8000/v1/")
    p = OpenAICompatibleProvider(api_key="abc", extra_headers={"X-Test": "1"}, transport=FakeTransport()
                                 .add("/chat/completions", (200, fx("openai_chat.json"))))
    p.complete(model="m", system="s", messages=MSGS, max_tokens=1)
    assert p.base_url == "http://vllm.internal:8000/v1"


def test_openrouter_needs_key_and_aliases_price() -> None:
    t = FakeTransport().add("openrouter.ai/api/v1/chat/completions", (200, fx("openai_chat.json")))
    with pytest.raises(FatalError):
        OpenRouterProvider(transport=t).complete(model="x", system="", messages=MSGS, max_tokens=1)
    p = OpenRouterProvider(api_key="or-key", transport=t)
    p.complete(model="anthropic/claude-sonnet-5-5", system="", messages=MSGS, max_tokens=1)
    assert t.calls[-1]["body"]["model"] == "anthropic/claude-sonnet-5-5"
    assert p.price("anthropic/claude-sonnet-5-5", Usage(1_000_000, 0)) == Decimal("2.0")


# ------------------------------------------------------------------- azure

def test_azure_chat_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_MODELS", "prod-41=gpt-4.1")
    t = FakeTransport().add("/openai/deployments/prod-41/chat/completions", (200, fx("openai_chat.json")))
    p = get_provider("azure", transport=t)
    r = p.complete(model="prod-41", system="s", messages=MSGS, max_tokens=3)
    assert t.calls[0]["url"].endswith("chat/completions?api-version=2024-10-21")
    assert t.calls[0]["headers"]["Api-key"] == "az-key"
    assert r.text == "hi there"
    assert p.price("prod-41", Usage(1_000_000, 0)) == Decimal("2.0")


def test_azure_responses_and_ad_token() -> None:
    t = FakeTransport().add("/openai/v1/responses", (200, fx("openai_responses.json")))
    p = AzureOpenAIProvider(endpoint="https://r.openai.azure.com", ad_token="tok", api_style="responses", transport=t)
    r = p.complete(model="dep", system="s", messages=MSGS, max_tokens=3, json_mode=True, web_search=True)
    assert t.calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert t.calls[0]["body"]["tools"] == [{"type": "web_search_preview"}]
    assert r.provider == "azure_openai"
    with pytest.raises(FatalError):
        AzureOpenAIProvider(endpoint="https://r", transport=t).complete(model="d", system="", messages=MSGS, max_tokens=1)
    with pytest.raises(FatalError):
        AzureOpenAIProvider(api_key="k", transport=t).complete(model="d", system="", messages=MSGS, max_tokens=1)


# ------------------------------------------------------------------ gemini

def test_gemini_request_and_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    t = FakeTransport().add("models/gemini-2.5-flash:generateContent", (200, fx("gemini_generate.json")))
    p = GeminiProvider(transport=t)
    msgs = [Message("user", "q"), Message("assistant", "a"), Message("user", "q2")]
    r = p.complete(model="gemini-2.5-flash", system="sys", messages=msgs, max_tokens=64, web_search=True, json_mode=True)
    b = t.calls[0]["body"]
    assert t.calls[0]["headers"]["X-goog-api-key"] == "g-key"
    assert [c["role"] for c in b["contents"]] == ["user", "model", "user"]
    assert b["generationConfig"] == {"maxOutputTokens": 64, "responseMimeType": "application/json",
                                     "thinkingConfig": {"thinkingBudget": 0}}
    assert b["tools"] == [{"google_search": {}}]
    assert b["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert r.text == "Answer" and r.stop_reason == "end"
    assert r.usage == Usage(400, 25, 100, 0)


def test_gemini_variants() -> None:
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    r = gemini_parse(blocked, "m")
    assert r.stop_reason == "refusal" and r.raw_stop_reason == "SAFETY"
    assert gemini_parse({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "m").stop_reason == "max_tokens"
    fc = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "f"}}]}, "finishReason": "STOP"}]}
    assert gemini_parse(fc, "m").stop_reason == "tool_use"
    t = FakeTransport().add(":generateContent", (200, {}))
    GeminiProvider(bearer_token="ya29", transport=t).complete(model="gemini-2.5-pro", system="", messages=MSGS, max_tokens=1)
    assert t.calls[0]["headers"]["Authorization"] == "Bearer ya29"
    assert t.calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 128}
    with pytest.raises(FatalError):
        GeminiProvider(transport=t).complete(model="m", system="", messages=MSGS, max_tokens=1)


def test_gemini_long_context_price() -> None:
    p = GeminiProvider(api_key="k")
    assert p.price("gemini-2.5-pro", Usage(100_000, 0)) == Decimal("0.125")
    assert p.price("gemini-2.5-pro", Usage(300_000, 0)) == Decimal("0.75")


# ----------------------------------------------------------------- bedrock

def test_sigv4_aws_test_suite_vectors() -> None:
    when = datetime.datetime(2015, 8, 30, 12, 36, tzinfo=datetime.timezone.utc)
    creds = dict(access_key="AKIDEXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                 region="us-east-1", service="service", now=when)
    vanilla = sigv4.sign("GET", "https://example.amazonaws.com/", {}, b"", **creds)  # type: ignore[arg-type]
    assert vanilla["Authorization"].endswith(
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")
    assert vanilla["X-Amz-Date"] == "20150830T123600Z"
    query = sigv4.sign("GET", "https://example.amazonaws.com/?Param2=value2&Param1=value1", {}, b"", **creds)  # type: ignore[arg-type]
    assert query["Authorization"].endswith(
        "Signature=b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500")


def test_sigv4_helpers() -> None:
    assert sigv4.canonical_uri("") == "/"
    assert sigv4.canonical_uri("/model/a%3A1/converse") == "/model/a%253A1/converse"
    assert sigv4.canonical_uri("/a%3A1", double_encode=False) == "/a%3A1"
    h = sigv4.sign("POST", "https://h/x", {"Content-Type": "application/json"}, b"{}", access_key="a",
                   secret_key="s", region="r", service="bedrock", session_token="tok")
    assert h["X-Amz-Security-Token"] == "tok"
    assert "content-type;host;x-amz-date;x-amz-security-token" in h["Authorization"]


def test_bedrock_signed_converse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session")
    t = FakeTransport().add("bedrock-runtime.eu-west-1.amazonaws.com/model/", (200, fx("bedrock_converse.json")))
    p = BedrockProvider(region="eu-west-1", transport=t)
    model = "eu.anthropic.claude-sonnet-5-5-v1:0"
    r = p.complete(model=model, system="sys", messages=MSGS, max_tokens=10, web_search=True)
    call = t.calls[0]
    assert call["url"].endswith("/model/eu.anthropic.claude-sonnet-5-5-v1%3A0/converse")
    assert call["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert call["headers"]["X-amz-security-token"] == "session"
    assert call["body"]["system"] == [{"text": "sys"}]
    assert call["body"]["inferenceConfig"] == {"maxTokens": 10}
    assert r.text == "Bedrock says hi" and r.stop_reason == "end" and r.usage.input_tokens == 30
    # Anthropic ids on Bedrock are priced from the anthropic table.
    assert p.price(model, Usage(1_000_000, 0)) == Decimal("2.0")
    assert p.price("amazon.nova-lite-v1:0", Usage(1_000_000, 0)) == Decimal("0.06")


def test_bedrock_bearer_and_errors() -> None:
    t = FakeTransport().add("/converse", (200, {"stopReason": "guardrail_intervened"}))
    p = BedrockProvider(bearer_token="bk", region="us-east-1", transport=t)
    r = p.complete(model="amazon.nova-pro-v1:0", system="", messages=MSGS, max_tokens=1)
    assert t.calls[0]["headers"]["Authorization"] == "Bearer bk"
    assert r.stop_reason == "refusal"
    t2 = FakeTransport().add("/converse", (404, {"message": "ResourceNotFoundException: model"}))
    with pytest.raises(FatalError) as ei:
        BedrockProvider(bearer_token="bk", transport=t2).complete(model="x", system="", messages=MSGS, max_tokens=1)
    assert ei.value.model_specific and str(ei.value).startswith("bedrock:")
    t3 = FakeTransport().add("/converse", (429, {"message": "ThrottlingException"}))
    with pytest.raises(TransientError):
        BedrockProvider(bearer_token="bk", transport=t3).complete(model="x", system="", messages=MSGS, max_tokens=1)
    with pytest.raises(FatalError):
        BedrockProvider(transport=t3).complete(model="x", system="", messages=MSGS, max_tokens=1)


# ------------------------------------------------------------------ images
def test_images_take_each_apis_shape() -> None:
    from squadhq.interfaces import Image
    from squadhq.providers.base import message_content

    png = Image("image/png", b"\x89PNG")
    m = Message(role="user", content="What is this?", images=(png,))
    assert message_content(Message("user", "hi"), "anthropic") == "hi"
    assert message_content(m, "anthropic") == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw=="}},
        {"type": "text", "text": "What is this?"}]
    assert message_content(m, "openai_chat")[1] == {"type": "image_url",
                                                    "image_url": {"url": "data:image/png;base64,iVBORw=="}}
    assert message_content(m, "openai_responses")[1] == {"type": "input_image",
                                                         "image_url": "data:image/png;base64,iVBORw=="}
    assert message_content(m, "gemini")[1] == {"inline_data": {"mime_type": "image/png", "data": "iVBORw=="}}
    jpg = Message("user", "x", images=(Image("image/jpg", b"j"),))
    assert message_content(jpg, "bedrock")[1]["image"]["format"] == "jpeg"
    # Images only ever go with the person's turns.
    assert message_content(Message("assistant", "ok", images=(png,)), "anthropic") == "ok"
    with pytest.raises(ValueError):
        message_content(m, "smoke-signals")
    t = FakeTransport().add("POST /v1/messages", (200, fx("anthropic_messages.json")))
    AnthropicProvider(api_key="k", transport=t).complete(model="claude-haiku-5-5", system="", messages=[m],
                                                         max_tokens=50)
    assert t.calls[0]["body"]["messages"][0]["content"][0]["type"] == "image"
    assert Capability.VISION in AnthropicProvider(api_key="k").capabilities("claude-haiku-5-5")
