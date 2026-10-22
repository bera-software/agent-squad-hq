"""Registry, model refs, price catalogue and the fallback chain."""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from squadhq import providers
from squadhq.interfaces import (
    Capability,
    CapacityError,
    FatalError,
    LLMReply,
    Message,
    TransientError,
    Usage,
)
from squadhq.providers import prices
from squadhq.providers.fallback import complete_with_fallback

MSGS = [Message("user", "hi")]


# ---------------------------------------------------------------- resolve

def test_resolve_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SQUADHQ_DEFAULT_PROVIDER", raising=False)
    assert providers.resolve("claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert providers.resolve("openai/gpt-4.1") == ("openai", "gpt-4.1")
    assert providers.resolve("ollama/llama3.1:8b") == ("ollama", "llama3.1:8b")
    assert providers.resolve("openrouter/anthropic/claude-x") == ("openrouter", "anthropic/claude-x")
    assert providers.resolve("Azure/prod-dep") == ("azure_openai", "prod-dep")
    # Unknown prefix: the whole thing is a bare model id.
    assert providers.resolve("meta-llama/Llama-3") == ("anthropic", "meta-llama/Llama-3")
    monkeypatch.setenv("SQUADHQ_DEFAULT_PROVIDER", "google")
    assert providers.resolve("gemini-2.5-pro") == ("gemini", "gemini-2.5-pro")
    with pytest.raises(FatalError):
        providers.resolve("  ")


def test_get_provider_every_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://localhost:9/v1")
    for name in providers.available_providers():
        p = providers.get_provider(name)
        assert p.name == name
    with pytest.raises(FatalError):
        providers.get_provider("nope")


def test_register_custom_and_complete() -> None:
    class Echo:
        name = "echo"

        def complete(self, **kw: Any) -> LLMReply:
            return LLMReply(text=kw["messages"][0].content, usage=Usage(1, 1), stop_reason="end",
                            model=kw["model"], provider="echo")

        def capabilities(self, model: str) -> set[Capability]:
            return set()

        def price(self, model: str, usage: Usage) -> Decimal | None:
            return None

    providers.register("echo", lambda **kw: Echo())  # type: ignore[arg-type,return-value]
    r = providers.complete("echo/m1", system="", messages=MSGS, max_tokens=1)
    assert r.text == "hi" and r.model == "m1"


def test_price_for_and_capabilities() -> None:
    assert providers.price_for("claude-sonnet-5-5", Usage(1_000_000, 1_000_000)) == Decimal("12.0")
    assert providers.price_for("openai/gpt-4o-mini", Usage(1_000_000, 0, 1_000_000)) == Decimal("0.225")
    assert providers.price_for("", Usage()) is None
    assert Capability.THINKING in providers.capabilities_for("openai/gpt-5")
    assert providers.capabilities_for("openai/gpt-4.1") == {Capability.TOOLS, Capability.WEB_SEARCH, Capability.JSON_MODE}


def test_unknown_price_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    prices.reload()
    with caplog.at_level(logging.WARNING):
        assert providers.price_for("openai/gpt-unknown", Usage(5, 5)) is None
        assert providers.price_for("openai/gpt-unknown", Usage(5, 5)) is None
    assert sum("gpt-unknown" in r.message for r in caplog.records) == 1


def test_custom_prices_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "p.yml"
    f.write_text("providers:\n  anthropic:\n    models:\n      m: {input: 1, output: 2, capabilities: [tools, bogus]}\n")
    monkeypatch.setenv("SQUADHQ_PRICES_FILE", str(f))
    prices.reload()
    try:
        assert prices.rates("anthropic", "m") == (1.0, 2.0)
        assert prices.rates("anthropic", "claude-sonnet-5-5") is None
        assert prices.capabilities("anthropic", "m") == {Capability.TOOLS}
        assert prices.setting("anthropic", "zzz", "thinking_off") is None
    finally:
        monkeypatch.delenv("SQUADHQ_PRICES_FILE")
        prices.reload()
    bad = tmp_path / "bad.yml"
    bad.write_text("- 1\n")
    with pytest.raises(ValueError):
        prices._load(str(bad))


def test_rates_long_context() -> None:
    assert prices.rates("anthropic", "claude-haiku-5-5", 100_000) == (0.10, 0.50)
    assert prices.rates("anthropic", "claude-haiku-5-5", 100_001) == (0.50, 2.50)
    assert prices.rates("nobody", "x") is None


# --------------------------------------------------------------- fallback

class Scripted:
    """Provider whose replies/errors are scripted per model."""

    def __init__(self, name: str, script: dict[str, list[Any]]) -> None:
        self.name = name
        self.script = script
        self.calls: list[str] = []

    def complete(self, *, model: str, **kw: Any) -> LLMReply:
        self.calls.append(model)
        step = self.script[model].pop(0) if len(self.script[model]) > 1 else self.script[model][0]
        if isinstance(step, Exception):
            raise step
        return LLMReply(text=step, usage=Usage(), stop_reason="end", model=model, provider=self.name)

    def capabilities(self, model: str) -> set[Capability]:
        return set()

    def price(self, model: str, usage: Usage) -> Decimal | None:
        return None


def run(primary: str, fallback: str | None, provs: dict[str, Scripted], **kw: Any) -> Any:
    with mock.patch("squadhq.providers.fallback.time.sleep") as sleep:
        try:
            res = complete_with_fallback(primary, fallback, system="", messages=MSGS, max_tokens=1,
                                         provider_factory=lambda n: provs[n], **kw)  # type: ignore[arg-type,return-value]
        except Exception as e:  # noqa: BLE001
            return e, sleep
    return res, sleep


def test_success_first_try() -> None:
    a = Scripted("anthropic", {"p": ["ok"]})
    res, sleep = run("p", "f", {"anthropic": a})
    assert res.reply.text == "ok" and not res.used_fallback and res.attempts == 1
    sleep.assert_not_called()


def test_transient_retries_same_then_final_attempt_on_fallback() -> None:
    a = Scripted("anthropic", {"p": [TransientError("503")], "f": ["from fallback"]})
    res, sleep = run("p", "f", {"anthropic": a})
    assert a.calls == ["p", "p", "f"]
    assert res.used_fallback and res.used_ref == "f" and res.attempts == 3 and len(res.errors) == 2
    assert [c.args[0] for c in sleep.call_args_list] == [1.0, 2.0]


def test_retry_after_honoured() -> None:
    a = Scripted("anthropic", {"p": [TransientError("429", retry_after=7), "ok"]})
    res, sleep = run("p", None, {"anthropic": a})
    assert res.reply.text == "ok"
    sleep.assert_called_once_with(7)


def test_model_specific_switches_immediately() -> None:
    a = Scripted("anthropic", {"p": [TransientError("529 overloaded", model_specific=True)], "f": ["ok"]})
    res, sleep = run("p", "f", {"anthropic": a})
    assert a.calls == ["p", "f"] and res.used_fallback
    sleep.assert_not_called()
    a2 = Scripted("anthropic", {"p": [FatalError("model not found", model_specific=True)], "f": ["ok"]})
    res2, _ = run("p", "f", {"anthropic": a2})
    assert res2.used_ref == "f"


def test_provider_wide_switches_only_cross_provider() -> None:
    a = Scripted("anthropic", {"p": [FatalError("401", provider_wide=True)], "f": ["never"]})
    err, sleep = run("p", "f", {"anthropic": a})
    assert isinstance(err, FatalError) and a.calls == ["p"]
    sleep.assert_not_called()
    a = Scripted("anthropic", {"p": [CapacityError("quota", provider_wide=True)]})
    o = Scripted("openai", {"gpt-4.1": ["cross"]})
    res, _ = run("p", "openai/gpt-4.1", {"anthropic": a, "openai": o})
    assert res.reply.text == "cross" and res.provider == "openai" and res.used_fallback


def test_capacity_same_provider_raises_without_retry() -> None:
    a = Scripted("anthropic", {"p": [CapacityError("quota")]})
    err, sleep = run("p", "f", {"anthropic": a})
    assert isinstance(err, CapacityError) and a.calls == ["p"]


def test_exhausted_raises_last_error_and_no_fallback_loop() -> None:
    a = Scripted("anthropic", {"p": [TransientError("x")], "f": [TransientError("y")]})
    err, _ = run("p", "f", {"anthropic": a})
    assert isinstance(err, TransientError) and str(err) == "y"
    a = Scripted("anthropic", {"p": [TransientError("x")]})
    err, sleep = run("p", "p", {"anthropic": a}, max_attempts=2)
    assert a.calls == ["p", "p"] and sleep.call_count == 1
    a = Scripted("anthropic", {"p": [FatalError("bad request")]})
    err, _ = run("p", "f", {"anthropic": a})
    assert isinstance(err, FatalError) and a.calls == ["p"]


def test_on_event_messages() -> None:
    events: list[str] = []
    a = Scripted("anthropic", {"p": [TransientError("x")], "f": ["ok"]})
    with mock.patch("squadhq.providers.fallback.time.sleep"):
        complete_with_fallback("p", "f", system="", messages=MSGS, max_tokens=1,
                               provider_factory=lambda n: a, on_event=events.append)  # type: ignore[arg-type,return-value]
    assert any("switching to fallback" in e for e in events)
    assert any("retrying" in e for e in events)
