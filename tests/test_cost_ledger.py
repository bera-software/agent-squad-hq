"""Tests for squadhq.cost_ledger. No network: gh is a scripted fake."""
import datetime
import json
import subprocess

import pytest

from squadhq import cost_ledger as cl
from squadhq import github


class FakeGH:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.jobs = []
        self.log = ""
        self.fail = set()

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        key = args[0] + " " + args[1]
        if key in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 404")
        out = ""
        if args[:2] == ["issue", "list"]:
            out = json.dumps(self.issues)
        elif args[:2] == ["issue", "create"]:
            out = "https://github.com/o/hub/issues/31\n"
        elif args[:2] == ["api", "--paginate"]:
            out = "\n".join(json.dumps(j) for j in self.jobs)
        elif args[0] == "api" and args[1].endswith("/logs"):
            out = self.log
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def find(self, *p):
        return [c for c in self.calls if c[:len(p)] == list(p)]


@pytest.fixture
def gh():
    fake = FakeGH()
    github.set_client(github.GitHubClient(runner=fake, sleep=lambda s: None))
    yield fake
    github.set_client(None)


def test_extract_metrics_variants(tmp_path):
    empty = {"cost_usd": None, "duration_ms": None, "num_turns": None}
    assert cl.extract_metrics("") == empty
    assert cl.extract_metrics(str(tmp_path / "missing")) == empty
    p = tmp_path / "x.json"
    p.write_text("not json")
    assert cl.extract_metrics(str(p)) == empty
    p.write_text(json.dumps([{"type": "assistant"},
                             {"type": "result", "total_cost_usd": 0.1234567, "duration_ms": 10, "num_turns": 3}]))
    assert cl.extract_metrics(str(p)) == {"cost_usd": 0.123457, "duration_ms": 10, "num_turns": 3}
    p.write_text(json.dumps([{"total_cost_usd": 1}]))
    assert cl.extract_metrics(str(p))["cost_usd"] == 1
    p.write_text(json.dumps([]))
    assert cl.extract_metrics(str(p)) == empty
    p.write_text(json.dumps(["x"]))
    assert cl.extract_metrics(str(p)) == empty
    p.write_text(json.dumps({"total_cost_usd": "free", "num_turns": 2}))
    assert cl.extract_metrics(str(p)) == {"cost_usd": None, "duration_ms": None, "num_turns": 2}


def test_extract_metrics_from_log(gh):
    empty = {"cost_usd": None, "duration_ms": None, "num_turns": None}
    assert cl.extract_metrics_from_log("", "1", "review") == empty
    gh.jobs = [{"id": 5, "name": "x / review"}]
    gh.log = 'x "total_cost_usd": 0.5 y "duration_ms": 1200.0 "num_turns": 4 "total_cost_usd": 0.75'
    assert cl.extract_metrics_from_log("o/hub", "1", "review") == {"cost_usd": 0.75, "duration_ms": 1200,
                                                                  "num_turns": 4}
    assert gh.find("api", "--paginate", "repos/o/hub/actions/runs/1/jobs")
    gh.log = "nothing here"
    assert cl.extract_metrics_from_log("o/hub", "1", "review") == empty
    gh.jobs = [{"id": 5, "name": "other"}]
    assert cl.extract_metrics_from_log("o/hub", "1", "review") == empty
    gh.jobs = [{"id": 5, "name": "review"}]
    gh.fail = {"api repos/o/hub/actions/jobs/5/logs"}
    assert cl.extract_metrics_from_log("o/hub", "1", "review") == empty
    gh.fail = {"api --paginate"}
    assert cl.extract_metrics_from_log("o/hub", "1", "review") == empty


def test_extract_metrics_from_log_swallows(monkeypatch):
    monkeypatch.setattr(github, "client", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert cl.extract_metrics_from_log("o/hub", "1", "r")["cost_usd"] is None


def test_find_or_create_ledger_issue(gh):
    gh.issues = [{"number": 7, "title": "Cost ledger — 2026-01", "author": {"login": "mallory"}},
                 {"number": 9, "title": "Cost ledger — 2026-01", "author": {"login": "app/squadhq-ops"}}]
    assert cl.find_or_create_ledger_issue("o/hub", "Cost ledger — 2026-01") == 9
    lst = gh.find("issue", "list")[0]
    assert lst[lst.index("--state") + 1] == "all"
    gh.issues = []
    assert cl.find_or_create_ledger_issue("o/hub", "Cost ledger — 2026-02") == 31
    create = gh.find("issue", "create")[0]
    assert create[create.index("-l") + 1] == "dept:finance"


def set_env(monkeypatch, **kw):
    base = {"HUB_REPO": "o/hub", "REPO": "o/p", "WORKFLOW": "router", "JOB": "review", "EVENT": "pull_request",
            "MODEL": "m", "RUN_ID": "12", "RUN_ATTEMPT": "x", "SESSION_RESUMED": "true", "MODEL_FALLBACK": "false",
            "AUTO_MERGED": "maybe"}
    base.update(kw)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def posted_line(gh):
    body = gh.find("issue", "comment")[0][-1]
    return json.loads(body.strip("`").removeprefix("json\n"))


def test_main_records_metrics_line_in_utc_month(gh, monkeypatch, tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"type": "result", "total_cost_usd": 0.2, "duration_ms": 5, "num_turns": 1}))
    set_env(monkeypatch, EXECUTION_FILE=str(p), AUTH_MODE="api")
    cl.main()
    month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    search = gh.find("issue", "list")[0]
    assert f"Cost ledger — {month}" in search[search.index("--search") + 1]
    line = posted_line(gh)
    assert line["cost_usd"] == 0.2 and line["billing"] == "api"
    assert line["run_id"] == 12 and line["run_attempt"] is None
    assert line["session_resumed"] is True and line["model_fallback"] is False and line["auto_merged"] is None
    assert line["date"] == datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def test_main_skip_line(gh, monkeypatch):
    set_env(monkeypatch, AUTH_MODE="skip")
    cl.main()
    line = posted_line(gh)
    assert line["skipped_capacity"] is True and line["cost_usd"] == 0


def test_main_log_fallback_and_comment_failure(gh, monkeypatch, capsys):
    set_env(monkeypatch, AUTH_MODE="", GITHUB_RUN_ID="7")
    gh.jobs = [{"id": 1, "name": "review"}]
    gh.log = '"total_cost_usd": 1.5'
    gh.fail = {"issue comment"}
    cl.main()
    assert "NOT recorded" in capsys.readouterr().out


def test_main_no_ledger_issue(gh, monkeypatch, capsys):
    set_env(monkeypatch, AUTH_MODE="skip")
    gh.fail = {"issue create"}
    cl.main()
    assert "could not find or create" in capsys.readouterr().out


def test_run_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(cl, "main", lambda: (_ for _ in ()).throw(ValueError("x")))
    assert cl.run() == 0
    assert "swallowed" in capsys.readouterr().out


def test_sh_seam(gh):
    cl.sh(["gh", "api", "x"])
    assert gh.calls[-1] == ["api", "x"]


def test_main_prefers_execution_result(gh, monkeypatch, tmp_path):
    r = tmp_path / "execution-result.json"
    r.write_text(json.dumps({"executor": "codex", "role": "engineer", "model": "openai/gpt-x",
                             "outcome": "success", "cost_usd": 0.1234567, "turns": 4, "duration_s": 2.5}))
    set_env(monkeypatch, EXECUTION_RESULT=str(r), AUTH_MODE="api", MODEL="")
    cl.main()
    line = posted_line(gh)
    assert line["cost_usd"] == 0.123457 and line["num_turns"] == 4 and line["duration_ms"] == 2500
    assert line["model"] == "openai/gpt-x"


def test_main_unpriced_result_falls_back_to_execution_file(gh, monkeypatch, tmp_path):
    r = tmp_path / "execution-result.json"
    r.write_text(json.dumps({"executor": "x", "role": "e", "model": "m2", "outcome": "success"}))
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"type": "result", "total_cost_usd": 0.3, "duration_ms": 9, "num_turns": 2}))
    set_env(monkeypatch, EXECUTION_RESULT=str(r), EXECUTION_FILE=str(p), AUTH_MODE="api")
    cl.main()
    line = posted_line(gh)
    assert line["cost_usd"] == 0.3 and line["model"] == "m"
    assert cl.metrics_from_result(None) is None
    from squadhq.executors.result import from_dict
    assert cl.metrics_from_result(from_dict({"cost_usd": 1, "outcome": "success"}))["duration_ms"] is None
    assert cl.load_execution_result(str(tmp_path / "missing.json")) is None
