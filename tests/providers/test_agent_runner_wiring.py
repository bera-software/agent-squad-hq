"""agent_runner.call_claude routed through the provider registry (non-Anthropic refs)."""
from __future__ import annotations

import io
import json
import os
import sys
from unittest import mock

os.environ.setdefault("HQ_REPO", "test-org/test-hq")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
_argv = sys.argv
sys.argv = ["agent_runner.py", "brief"]
try:
    from squadhq import agent_runner as ar
finally:
    sys.argv = _argv


def _resp(body: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(body).encode())


def _usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}


def test_openai_ref_end_to_end(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.delenv("OPENAI_API_STYLE", raising=False)
    body = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [],
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}
    seen = {}

    def fake(req):
        seen["url"] = req.full_url
        return _resp(body)

    usage = _usage()
    with mock.patch.object(ar, "MODEL", "openai/gpt-4.1"), mock.patch.object(ar, "FALLBACK_MODEL", None), \
         mock.patch("urllib.request.urlopen", side_effect=fake):
        reply = ar.call_claude("sys", "user", usage)
    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert reply.stop_reason == "max_tokens"  # what _empty_reply_escalation keys on
    assert reply.model == "openai/gpt-4.1"
    assert usage["est_cost_usd"] == 2.0 and usage["last_model"] == "openai/gpt-4.1"
    assert usage["last_model_fallback"] is False


def test_unpriced_model_uses_conservative_rate_and_error_label(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    body = {"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    usage = _usage()
    with mock.patch.object(ar, "MODEL", "together/some-model"), mock.patch.object(ar, "FALLBACK_MODEL", None), \
         mock.patch("urllib.request.urlopen", return_value=_resp(body)):
        monkeypatch.setenv("TOGETHER_API_KEY", "t")
        reply = ar.call_claude("sys", "user", usage)
    assert reply.text == "[]" and reply.stop_reason == "stop"
    assert usage["est_cost_usd"] == 10 / 1e6 * 3.0 + 1 / 1e6 * 15.0
    monkeypatch.delenv("TOGETHER_API_KEY")
    with mock.patch.object(ar, "MODEL", "together/some-model"), mock.patch.object(ar, "FALLBACK_MODEL", None):
        try:
            ar.call_claude("sys", "user", _usage())
        except ar.AnthropicAPIError as e:
            assert str(e).startswith("together API error")
        else:  # pragma: no cover
            raise AssertionError("expected AnthropicAPIError")


def test_compat_shims() -> None:
    assert ar.RATES["claude-sonnet-5-5"] == (2.0, 10.0)
    assert ar.RATES["openai/gpt-4.1"] == (2.0, 8.0)
    assert ar.LONG_PROMPT_RATES["claude-haiku-5-5"] == (100_000, (0.50, 2.50))
    assert ar.THINKING_OFF["claude-sonnet-5-5"] == {"type": "between_tools"}
    assert "claude-sonnet-4-6" not in ar.THINKING_OFF
    assert ar.rate_for("claude-unknown", 1) == (3.0, 15.0)
    assert ar.rate_for("gemini/gemini-2.5-pro", 300_000) == (2.5, 15.0)
