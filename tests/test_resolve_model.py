"""Tests for squadhq.resolve_model. No network: gh is a scripted fake."""
import json
import subprocess

import pytest

from squadhq import github
from squadhq import resolve_model as rm


class FakeGH:
    def __init__(self):
        self.calls = []
        self.vars = []
        self.list_rc = 0
        self.delete_rc = 0

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        if args[:2] == ["variable", "list"]:
            out = self.vars if isinstance(self.vars, str) else json.dumps(self.vars)
            return subprocess.CompletedProcess(cmd, self.list_rc, out, "")
        if args[:2] == ["variable", "delete"]:
            return subprocess.CompletedProcess(cmd, self.delete_rc, "", "HTTP 403" if self.delete_rc else "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")


@pytest.fixture
def gh():
    fake = FakeGH()
    github.set_client(github.GitHubClient(runner=fake, sleep=lambda s: None))
    yield fake
    github.set_client(None)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"tiers": {"heavy": {"primary": "big", "fallback": "small", "jobs": ["review"]}}}))
    out = tmp_path / "out"
    monkeypatch.setenv("CONFIG_PATH", str(p))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("JOB", "review")
    monkeypatch.setenv("RUN_REPO", "o/p")
    return out


def outputs(out):
    return dict(ln.split("=", 1) for ln in out.read_text().splitlines())


def test_primary_when_no_override(gh, cfg):
    rm.main()
    assert outputs(cfg) == {"resolved_model": "big", "used_fallback": "false"}


def test_override_is_consumed_once(gh, cfg):
    gh.vars = [{"name": "MODEL_OVERRIDE_REVIEW", "value": "small"}, {"name": "X", "value": "y"}]
    rm.main()
    assert outputs(cfg) == {"resolved_model": "small", "used_fallback": "true"}
    assert ["variable", "delete", "MODEL_OVERRIDE_REVIEW", "-R", "o/p"] in gh.calls


def test_override_delete_failure_warns(gh, cfg, capsys):
    gh.vars = [{"name": "MODEL_OVERRIDE_REVIEW", "value": "small"}]
    gh.delete_rc = 1
    rm.main()
    assert "WARNING could not delete" in capsys.readouterr().out


def test_unreadable_variable_list_is_no_override(gh, cfg):
    gh.vars = "not json"
    rm.main()
    assert outputs(cfg)["resolved_model"] == "big"
    gh.list_rc = 1
    assert rm.read_override("o/p", "X") is None
    assert rm.read_override("", "X") is None
    gh.list_rc, gh.vars = 0, ""
    assert rm.read_override("o/p", "X") is None


def test_missing_config_uses_last_resort(gh, cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nope.json"))
    rm.main()
    assert outputs(cfg) == {"resolved_model": rm._LAST_RESORT, "used_fallback": "false"}


def test_missing_tier_uses_last_resort(gh, cfg, monkeypatch):
    monkeypatch.setenv("JOB", "unknown-job")
    rm.main()
    assert outputs(cfg)["resolved_model"] == rm._LAST_RESORT


def test_default_config_path_and_var_name(monkeypatch, tmp_path):
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path))
    assert rm.default_config_path() == str(tmp_path / "config" / "models.json")
    assert rm.var_name("a.b-c") == "MODEL_OVERRIDE_A_B_C"
    assert rm.find_tier({"tiers": {"t": {"jobs": ["x"]}}}, "x") == {"jobs": ["x"]}
    assert rm.find_tier({}, "x") is None


def test_gh_output_prints_without_github_output(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    rm.gh_output("k", "v")
    assert capsys.readouterr().out == "k=v\n"


def test_run_never_raises(cfg, monkeypatch):
    monkeypatch.setattr(rm, "main", lambda: (_ for _ in ()).throw(ValueError("x")))
    assert rm.run() == 0
    assert outputs(cfg)["resolved_model"] == rm._LAST_RESORT


def test_sh_seam(gh):
    rm.sh(["gh", "variable", "list"])
    assert gh.calls[-1] == ["variable", "list"]
