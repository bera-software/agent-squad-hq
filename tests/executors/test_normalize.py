import json
import os
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

from squadhq.executors import normalize as nz
from squadhq.executors.result import read_result
from squadhq.interfaces import ErrorClass

FIX = Path(__file__).parent / "fixtures"


def _text(name):
    return (FIX / name).read_text()


# ---------------------------------------------------------------- claude-code

def test_claude_success():
    r = nz.normalize("claude-code", FIX / "claude_success.json", role="implement")
    assert r.outcome == "success"
    assert r.error_class is ErrorClass.NONE
    assert r.model == "claude-sonnet-5-5"
    assert r.cost_usd == pytest.approx(1.234568)
    assert (r.input_tokens, r.output_tokens, r.turns) == (152340, 8123, 23)
    assert r.duration_s == pytest.approx(312.45)
    assert r.extra["cache_read_tokens"] == 90000
    assert r.extra["session_id"] == "sess-1"
    assert r.transcript_path.endswith("claude_success.json")
    assert not r.capacity_tripped


def test_claude_model_flag_overrides_init():
    r = nz.normalize("claude", FIX / "claude_success.json", model="x-model", duration_s=1.0)
    assert r.model == "x-model"
    assert r.duration_s == 1.0


def test_claude_error_is_transient():
    r = nz.normalize_claude_code(_text("claude_error.json"))
    assert r.outcome == "failure"
    assert r.error_class is ErrorClass.TRANSIENT
    assert "Overloaded" in r.error_message
    assert r.extra["permission_denials"] == 1
    assert r.model == "claude-sonnet-5"


def test_claude_max_turns_is_agent():
    r = nz.normalize_claude_code(json.loads(_text("claude_max_turns.json")))
    assert r.outcome == "failure"
    assert r.error_class is ErrorClass.AGENT
    assert r.turns == 60


def test_claude_capacity_trip():
    r = nz.normalize_claude_code(str(FIX / "claude_capacity.json"))
    assert r.capacity_tripped
    assert r.error_class is ErrorClass.CAPACITY
    assert r.outcome == "failure"


def test_claude_success_mentioning_signature_does_not_trip():
    data = json.loads(_text("claude_success.json"))
    data[1]["message"]["content"][0]["text"] = "capacity_gate looks for 'hit your session limit'"
    r = nz.normalize_claude_code(data)
    assert r.outcome == "success" and not r.capacity_tripped


def test_claude_single_result_dict_and_no_result():
    r = nz.normalize_claude_code({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1})
    assert r.outcome == "success" and r.turns == 1
    r = nz.normalize_claude_code([{"type": "system", "subtype": "init", "model": "m"}])
    assert r.outcome == "failure" and r.error_class is ErrorClass.UNKNOWN
    r = nz.normalize_claude_code([{"type": "assistant", "is_error": True, "text": "Bad credentials"}])
    assert r.error_class is ErrorClass.INFRA


def test_claude_unparseable_and_wrong_type():
    r = nz.normalize_claude_code("this is not json")
    assert r.error_class is ErrorClass.INFRA and r.outcome == "failure"
    r = nz.normalize_claude_code("You've hit your weekly limit")
    assert r.capacity_tripped and r.error_class is ErrorClass.CAPACITY
    r = nz.normalize_claude_code(42)
    assert r.error_class is ErrorClass.INFRA


# ---------------------------------------------------------------------- codex

def test_codex_success(monkeypatch):
    r = nz.normalize("codex", FIX / "codex_success.jsonl", model="gpt-5-codex")
    assert r.outcome == "success"
    assert (r.input_tokens, r.output_tokens, r.turns) == (24763, 122, 1)
    assert r.extra["commands"] == 1
    assert r.extra["cache_read_tokens"] == 24448
    assert r.extra["final_message"].startswith("Fixed")
    assert r.extra["thread_id"].startswith("0199")


def test_codex_priced_via_providers(monkeypatch):
    seen = {}

    def price_for(ref, usage):
        seen["ref"] = ref
        seen["usage"] = usage
        return Decimal("0.0123")

    fake = types.ModuleType("squadhq.providers")
    fake.price_for = price_for
    monkeypatch.setitem(sys.modules, "squadhq.providers", fake)
    r = nz.normalize_codex(_text("codex_success.jsonl"), model="gpt-5-codex")
    assert r.cost_usd == pytest.approx(0.0123)
    assert seen["ref"] == "openai/gpt-5-codex"
    assert seen["usage"].cache_read_tokens == 24448


def test_codex_price_errors_are_swallowed(monkeypatch):
    fake = types.ModuleType("squadhq.providers")

    def boom(ref, usage):
        raise RuntimeError("no prices")

    fake.price_for = boom
    monkeypatch.setitem(sys.modules, "squadhq.providers", fake)
    assert nz.normalize_codex(_text("codex_success.jsonl"), model="openai/gpt-5").cost_usd is None


def test_codex_failure_transient():
    r = nz.normalize_codex(_text("codex_failure.jsonl"))
    assert r.outcome == "failure"
    assert r.error_class is ErrorClass.TRANSIENT
    assert "429" in r.error_message
    assert r.input_tokens is None and r.cost_usd is None


def test_codex_legacy_envelope():
    r = nz.normalize_codex(_text("codex_legacy.jsonl"))
    assert r.outcome == "success"
    assert r.model == "gpt-5-codex"
    assert (r.input_tokens, r.output_tokens, r.turns) == (1000, 50, 1)
    assert r.extra["unparsed_lines"] == 1
    assert r.extra["final_message"] == "All tests pass."
    assert r.extra["commands"] == 1


def test_codex_legacy_error_and_flat_token_count():
    lines = [
        {"msg": {"type": "token_count", "input_tokens": 5, "output_tokens": 6}},
        {"msg": {"type": "error", "message": "You've hit your usage limit. Upgrade to Pro"}},
    ]
    r = nz.normalize_codex("\n".join(json.dumps(x) for x in lines))
    assert r.capacity_tripped and r.error_class is ErrorClass.CAPACITY
    assert r.input_tokens == 5


def test_codex_no_events_and_no_turns():
    r = nz.normalize_codex("garbage\n\n")
    assert r.error_class is ErrorClass.INFRA
    r = nz.normalize_codex("hit your usage limit")
    assert r.capacity_tripped
    r = nz.normalize_codex('{"type":"thread.started","thread_id":"x"}')
    assert r.outcome == "failure" and r.error_class is ErrorClass.UNKNOWN


# ----------------------------------------------------------------- gemini-cli

def test_gemini_success():
    r = nz.normalize("gemini", FIX / "gemini_success.json")
    assert r.outcome == "success"
    assert r.model == "gemini-2.5-pro"
    assert (r.input_tokens, r.output_tokens, r.turns) == (18234, 1200, 4)
    assert r.duration_s == pytest.approx(15.234)
    assert r.extra["tool_calls"] == 6
    assert r.extra["cache_read_tokens"] == 1200


def test_gemini_error_capacity():
    r = nz.normalize_gemini_cli(_text("gemini_error.json"))
    assert r.outcome == "failure"
    assert r.capacity_tripped and r.error_class is ErrorClass.CAPACITY
    assert r.error_message.startswith("ApiError:")


def test_gemini_other_shapes():
    assert nz.normalize_gemini_cli("not json").error_class is ErrorClass.INFRA
    assert nz.normalize_gemini_cli([1, 2]).error_class is ErrorClass.INFRA
    r = nz.normalize_gemini_cli({"stats": {}})
    assert r.outcome == "failure" and r.error_class is ErrorClass.UNKNOWN
    r = nz.normalize_gemini_cli({"error": "permission denied on repo"})
    assert r.error_class is ErrorClass.INFRA
    r = nz.normalize_gemini_cli({"response": "ok", "stats": {"models": {"m": "bad"}}})
    assert r.outcome == "success" and r.input_tokens is None


# ---------------------------------------------------------------------- aider

def test_aider_success_last_match_wins():
    r = nz.normalize("aider", FIX / "aider.log")
    assert r.outcome == "success"
    assert r.model == "anthropic/claude-sonnet-5-5"
    assert (r.input_tokens, r.output_tokens) == (12000, 1200)
    assert r.cost_usd == pytest.approx(0.07)
    assert r.turns == 2
    assert r.extra["commits"] == ["1a2b3c4", "5d6e7f8"]


def test_aider_error_and_empty():
    r = nz.normalize_aider(_text("aider_error.log"))
    assert r.outcome == "failure"
    assert r.error_class is ErrorClass.TRANSIENT
    assert r.input_tokens == 2000 and r.cost_usd is None
    assert nz.normalize_aider("   ").error_class is ErrorClass.INFRA
    assert nz.normalize_aider("insufficient_quota").capacity_tripped


# ------------------------------------------------------------- entry + CLI

def test_normalize_missing_empty_and_unknown(tmp_path):
    r = nz.normalize("codex", tmp_path / "nope.jsonl", role="review")
    assert r.outcome == "failure" and r.error_class is ErrorClass.INFRA and r.role == "review"
    empty = tmp_path / "e.json"
    empty.write_text("  ")
    assert nz.normalize("claude-code", empty).error_class is ErrorClass.INFRA
    with pytest.raises(ValueError):
        nz.normalize("cursor", empty)


def test_normalize_unreadable_and_normaliser_bug(tmp_path, monkeypatch):
    d = tmp_path / "dir"
    d.mkdir()
    r = nz.normalize("aider", d)
    assert r.error_class is ErrorClass.INFRA and "cannot read" in r.error_message

    f = tmp_path / "x.log"
    f.write_text("hello")

    def bug(*a, **k):
        raise KeyError("boom")

    monkeypatch.setitem(nz.NORMALIZERS, "aider", bug)
    r = nz.normalize("aider", f)
    assert "normaliser error" in r.error_message


def test_cli_writes_result(tmp_path, capsys):
    out = tmp_path / "execution-result.json"
    rc = nz.main(["--executor", "claude-code", "--input", str(FIX / "claude_success.json"),
                  "--output", str(out), "--role", "implement"])
    assert rc == 0
    line = capsys.readouterr().out
    assert "outcome=success" in line and "cost=$1.2346" in line
    data = json.loads(out.read_text())
    assert data["schema_version"] == "1" and data["error_class"] == "none"
    assert read_result(out).turns == 23


def test_cli_missing_input_still_writes(tmp_path, capsys):
    out = tmp_path / "r.json"
    rc = nz.main(["--executor", "codex", "--input", str(tmp_path / "missing"), "--output", str(out)])
    assert rc == 0
    assert json.loads(out.read_text())["error_class"] == "infra"
    assert "cost=unpriced" in capsys.readouterr().out


def test_cli_unknown_executor(tmp_path):
    assert nz.main(["--executor", "cursor", "--input", "x", "--output", os.fspath(tmp_path / "r")]) == 2


def test_package_registry():
    import squadhq.executors as ex

    assert ex.get_normalizer("claude") is nz.normalize_claude_code
    assert ex.list_executors() == ["aider", "claude-code", "codex", "gemini-cli"]
    assert ex.normalize_output("aider", FIX / "aider.log").cost_usd == pytest.approx(0.07)
    assert ex.to_dict is not None
    with pytest.raises(AttributeError):
        ex.nonexistent  # noqa: B018
