"""Tests for squadhq.failure_handler — classification, retry/fallback paths,
override cleanup, escalation dedup. No network: gh is a scripted fake."""
import json
import subprocess

import pytest

from squadhq import failure_handler as fh
from squadhq import github


class FakeGH:
    """Runner for GitHubClient: ``handler(args) -> (rc, stdout, stderr)``."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda a: (0, "", ""))

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        rc, out, err = self.handler(args)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def has(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def gh():
    fake = FakeGH()
    github.set_client(github.GitHubClient(runner=fake, sleep=lambda s: None))
    yield fake
    github.set_client(None)


@pytest.mark.parametrize("text,expected", [
    ("Your credit balance is too low to access the API", "INFRA"),
    ("error: insufficient_quota", "INFRA"),
    ("You exceeded your current quota, please check your plan and billing details", "INFRA"),
    ("HTTP 402 Payment Required", "INFRA"),
    ("HTTP 401: Bad credentials", "INFRA"),
    ("authentication_error: invalid x-api-key", "INFRA"),
    ("HTTP 403 Forbidden", "INFRA"),
    ("gh: 404 Not Found", "INFRA"),
    ("could not find label 'x'", "INFRA"),
    ("HTTP 429 Too Many Requests", "TRANSIENT"),
    ("rate limit exceeded", "TRANSIENT"),
    ('{"status": 529, "type": "overloaded_error"}', "TRANSIENT"),
    ("HTTP 503 Service Unavailable", "TRANSIENT"),
    ("HTTPS error 500", "TRANSIENT"),
    ("request timed out", "TRANSIENT"),
    ("connection reset by peer", "TRANSIENT"),
    ("ECONNREFUSED 127.0.0.1", "TRANSIENT"),
    ("HTTP 408 request timeout", "TRANSIENT"),
    # generic 4xx is no longer transient
    ("HTTP 400 Bad Request: invalid parameter", "UNKNOWN"),
    ('{"status": 422}', "UNKNOWN"),
    ("", "UNKNOWN"),
])
def test_classify_table(text, expected):
    assert fh.classify(text, "failure")[0] == expected


def test_classify_agent_when_step_succeeded():
    assert fh.classify("nothing recognisable", "success") == ("AGENT", "agent step succeeded but job still failed")


def test_suggested_fix_is_not_optimistic():
    assert "likely resolved" not in fh.SUGGESTED_FIX["TRANSIENT-EXHAUSTED"]
    assert "Claude" not in fh.SUGGESTED_FIX["TRANSIENT-EXHAUSTED"]


def test_model_specific_and_var_name():
    assert fh.is_model_specific("the model is overloaded")
    assert fh.is_model_specific("model_not_found")
    assert not fh.is_model_specific("HTTP 503")
    assert fh.var_name("review-fix") == "MODEL_OVERRIDE_REVIEW_FIX"


def test_resolve_fallback_model(tmp_path):
    cfg = tmp_path / "models.json"
    cfg.write_text(json.dumps({"tiers": {"heavy": {"primary": "a", "fallback": "b"}},
                               "hub_loops": {"light": {"primary": "c"}}}))
    assert fh.resolve_fallback_model("a", str(cfg)) == "b"
    assert fh.resolve_fallback_model("b", str(cfg)) is None
    assert fh.resolve_fallback_model("c", str(cfg)) is None
    assert fh.resolve_fallback_model("zzz", str(cfg)) is None
    assert fh.resolve_fallback_model("", str(cfg)) is None
    assert fh.resolve_fallback_model("a", str(tmp_path / "missing.json")) is None


def test_default_config_path_is_hub_relative(monkeypatch, tmp_path):
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path))
    assert fh.default_config_path() == str(tmp_path / "config" / "models.json")


def test_extract_text_from_execution_file_variants(tmp_path):
    assert fh.extract_text_from_execution_file("") == ""
    assert fh.extract_text_from_execution_file(str(tmp_path / "nope")) == ""
    p = tmp_path / "x.json"
    p.write_text(json.dumps([{"type": "result", "result": "boom 529"}, {"is_error": True, "x": 1}, "junk"]))
    out = fh.extract_text_from_execution_file(str(p))
    assert "boom 529" in out and '"is_error": true' in out
    p.write_text(json.dumps([{"type": "assistant"}]))
    assert "assistant" in fh.extract_text_from_execution_file(str(p))
    p.write_text(json.dumps({"error": "overloaded"}))
    assert "overloaded" in fh.extract_text_from_execution_file(str(p))
    p.write_text(json.dumps(42))
    assert fh.extract_text_from_execution_file(str(p)) == "42"
    p.write_text("not json at all: rate limit")
    assert "rate limit" in fh.extract_text_from_execution_file(str(p))


def jobs_handler(jobs, log="tail: HTTP 503", log_rc=0):
    def h(args):
        if args[:2] == ["api", "--paginate"]:
            return 0, "\n".join(json.dumps(j) for j in jobs), ""
        if args[0] == "api" and args[1].endswith("/logs"):
            return log_rc, log, ""
        return 1, "", "unexpected"
    return h


def test_extract_text_from_log_paginates_line_delimited(gh):
    gh.handler = jobs_handler([{"id": 1, "name": "review", "conclusion": "success"},
                               {"id": 2, "name": "review (x)", "conclusion": "failure"}])
    assert fh.extract_text_from_log("o/r", "9", "review") == "tail: HTTP 503"
    assert gh.calls[0] == ["api", "--paginate", "repos/o/r/actions/runs/9/jobs", "--jq", ".jobs[] | tojson"]
    assert gh.calls[1] == ["api", "repos/o/r/actions/jobs/2/logs"]


def test_extract_text_from_log_single_job_and_failures(gh):
    gh.handler = jobs_handler([{"id": 7, "name": "run"}])
    assert fh.extract_text_from_log("o/r", "9", "growth") == "tail: HTTP 503"
    gh.handler = jobs_handler([{"id": 7, "name": "a"}, {"id": 8, "name": "b"}])
    assert fh.extract_text_from_log("o/r", "9", "growth") == ""
    gh.handler = jobs_handler([{"id": 7, "name": "run"}], log_rc=1)
    assert fh.extract_text_from_log("o/r", "9", "run") == ""
    gh.handler = lambda a: (1, "", "boom")
    assert fh.extract_text_from_log("o/r", "9", "run") == ""
    assert fh.extract_text_from_log("", "9", "run") == ""


def test_escalate_creates_then_dedups(gh):
    existing = {"n": None}

    def h(args):
        if args[:2] == ["issue", "list"]:
            n = existing["n"]
            return 0, json.dumps([{"number": n, "title": args[args.index("--search") + 1].split('"')[1],
                                   "author": {"login": "app/squadhq-ops"}}]
                                 if n else []), ""
        if args[:2] == ["issue", "create"]:
            existing["n"] = 5
            return 0, "https://github.com/o/hub/issues/5\n", ""
        return 0, "", ""
    gh.handler = h
    ok, title = fh.escalate("o/hub", "INFRA", "http://run/1", 1, "review", "o/p", "pull_request", "3", "sig")
    assert ok and title == "ESCALATION [INFRA]: review failed (o/p)"
    assert len(gh.has("issue", "create")) == 1
    ok, _ = fh.escalate("o/hub", "INFRA", "http://run/2", 2, "review", "o/p", "", "", "")
    assert ok
    assert len(gh.has("issue", "create")) == 1  # no duplicate
    comments = gh.has("issue", "comment")
    assert comments and "Recurred: run http://run/2 (attempt 2)" in comments[0][-1]


def base_env(monkeypatch, tmp_path, **extra):
    summary = tmp_path / "summary.md"
    env = {"HUB_REPO": "o/hub", "RUN_REPO": "o/p", "REPO": "o/p", "JOB": "review", "EVENT": "push",
           "RUN_ID": "9", "RUN_ATTEMPT": "1", "RUN_URL": "http://run", "GITHUB_STEP_SUMMARY": str(summary)}
    env.update(extra)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return summary


def write_exec(tmp_path, text):
    p = tmp_path / "exec.json"
    p.write_text(json.dumps([{"type": "result", "result": text}]))
    return str(p)


def scripted(rerun_rc=0, var_rc=0):
    def h(args):
        if args[:2] == ["api", "--method"] or (args[0] == "api" and " ".join(args).endswith("/rerun")):
            return rerun_rc, "", "" if rerun_rc == 0 else "HTTP 403 nope"
        if args[:2] == ["variable", "set"] or args[:2] == ["variable", "delete"]:
            return var_rc, "", ""
        if args[:2] == ["issue", "list"]:
            return 0, "[]", ""
        if args[:2] == ["issue", "create"]:
            return 0, "https://github.com/o/hub/issues/11", ""
        return 0, "", ""
    return h


def test_main_transient_reruns(gh, monkeypatch, tmp_path):
    summary = base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "HTTP 503"))
    gh.handler = scripted()
    fh.main(["ci"])
    assert any(" ".join(c).endswith("/rerun") for c in gh.calls)
    assert not gh.has("issue", "create")
    assert "auto-retrying" in summary.read_text()


def test_main_model_specific_sets_override_then_reruns(gh, monkeypatch, tmp_path):
    cfg = tmp_path / "models.json"
    cfg.write_text(json.dumps({"tiers": {"t": {"primary": "m1", "fallback": "m2", "jobs": ["review"]}}}))
    summary = base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "the model is overloaded"),
                       CURRENT_MODEL="m1", CONFIG_PATH=str(cfg))
    gh.handler = scripted()
    fh.main(["ci"])
    assert gh.has("variable", "set", "MODEL_OVERRIDE_REVIEW")
    assert "fallback m2" in summary.read_text()
    assert not gh.has("variable", "delete")


def test_main_override_cleared_when_rerun_fails(gh, monkeypatch, tmp_path):
    cfg = tmp_path / "models.json"
    cfg.write_text(json.dumps({"tiers": {"t": {"primary": "m1", "fallback": "m2"}}}))
    summary = base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "model overloaded"),
                       CURRENT_MODEL="m1", CONFIG_PATH=str(cfg))
    gh.handler = scripted(rerun_rc=1)
    fh.main(["ci"])
    assert gh.has("variable", "delete", "MODEL_OVERRIDE_REVIEW")
    assert gh.has("issue", "create")
    text = summary.read_text()
    assert "override cleared" in text and "TRANSIENT-EXHAUSTED" in text


def test_main_rerun_api_failure_escalates(gh, monkeypatch, tmp_path):
    summary = base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "HTTP 503"))
    gh.handler = scripted(rerun_rc=1)
    fh.main(["ci"])
    assert gh.has("issue", "create")
    assert "retry API call failed" in summary.read_text()


def test_main_attempt_three_escalates_exhausted(gh, monkeypatch, tmp_path):
    base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "HTTP 503"), RUN_ATTEMPT="3")
    gh.handler = scripted()
    fh.main(["ci"])
    create = gh.has("issue", "create")[0]
    assert "ESCALATION [TRANSIENT-EXHAUSTED]" in create[create.index("-t") + 1]


def test_main_billing_never_retries(gh, monkeypatch, tmp_path):
    base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "credit balance is too low"),
             RUN_ATTEMPT="bogus")
    gh.handler = scripted()
    fh.main(["ci"])
    assert not any(" ".join(c).endswith("/rerun") for c in gh.calls)
    create = gh.has("issue", "create")[0]
    assert "ESCALATION [INFRA]" in create[create.index("-t") + 1]


def test_main_cron_mode_uses_log_and_never_retries(gh, monkeypatch, tmp_path):
    summary = base_env(monkeypatch, tmp_path)
    log_jobs = jobs_handler([{"id": 1, "name": "review", "conclusion": "failure"}], log="HTTP 503")

    def h(args):
        if args[:2] == ["issue", "list"]:
            return 0, "[]", ""
        if args[:2] == ["issue", "create"]:
            return 1, "", "boom"
        return log_jobs(args)
    gh.handler = h
    fh.main(["cron"])
    assert not any(" ".join(c).endswith("/rerun") for c in gh.calls)
    assert "escalation issue creation FAILED" in summary.read_text()


def test_main_ci_falls_back_to_log(gh, monkeypatch, tmp_path):
    base_env(monkeypatch, tmp_path, EXECUTION_FILE="")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    gh.handler = lambda a: (0, "[]", "") if a[:2] == ["issue", "list"] else (1, "", "x")
    fh.main(["ci"])  # no text, UNKNOWN, escalation attempted
    assert gh.has("issue", "create")


def test_run_swallows_crash(monkeypatch, tmp_path, capsys):
    base_env(monkeypatch, tmp_path)
    monkeypatch.setattr(fh, "main", lambda argv=None: (_ for _ in ()).throw(ValueError("x")))
    assert fh.run([]) == 0
    assert "swallowed error" in capsys.readouterr().out
    assert "crashed" in (tmp_path / "summary.md").read_text()


def test_sh_seam_strips_gh(gh):
    fh.sh(["gh", "api", "x"])
    assert gh.calls[-1] == ["api", "x"]


def write_result(tmp_path, **kw):
    data = {"schema_version": "1", "executor": "x", "role": "engineer", "model": "m1", "outcome": "failure"}
    data.update(kw)
    p = tmp_path / "execution-result.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_result_error_class_preferred_over_execution_file(gh, monkeypatch, tmp_path):
    base_env(monkeypatch, tmp_path, EXECUTION_FILE=write_exec(tmp_path, "HTTP 503"),
             EXECUTION_RESULT=write_result(tmp_path, error_class="policy", error_message="tool not allowed"))
    gh.handler = scripted()
    fh.main(["ci"])
    assert not any(" ".join(c).endswith("/rerun") for c in gh.calls)
    create = gh.has("issue", "create")[0]
    assert "ESCALATION [POLICY]" in create[create.index("-t") + 1]
    assert "policy guard" in create[create.index("-b") + 1]


def test_result_transient_model_specific_uses_result_model(gh, monkeypatch, tmp_path):
    cfg = tmp_path / "models.json"
    cfg.write_text(json.dumps({"tiers": {"t": {"primary": "m1", "fallback": "m2"}}}))
    summary = base_env(monkeypatch, tmp_path, CONFIG_PATH=str(cfg), EXECUTION_RESULT=write_result(
        tmp_path, error_class="transient", error_message="boom", extra={"model_specific": True}))
    monkeypatch.delenv("CURRENT_MODEL", raising=False)
    gh.handler = scripted()
    fh.main(["ci"])
    assert gh.has("variable", "set", "MODEL_OVERRIDE_REVIEW")
    assert "fallback m2" in summary.read_text()


def test_result_unknown_class_falls_back_to_text(gh, monkeypatch, tmp_path):
    base_env(monkeypatch, tmp_path, EXECUTION_RESULT=write_result(
        tmp_path, error_class="unknown", error_message="HTTP 503 Service Unavailable"))
    gh.handler = scripted()
    fh.main(["ci"])
    assert any(" ".join(c).endswith("/rerun") for c in gh.calls)


def test_result_capacity_class_is_infra_and_bad_file_ignored(gh, monkeypatch, tmp_path, capsys):
    base_env(monkeypatch, tmp_path, EXECUTION_RESULT=write_result(tmp_path, error_class="capacity"))
    gh.handler = scripted()
    fh.main(["ci"])
    create = gh.has("issue", "create")[0]
    assert "ESCALATION [INFRA]" in create[create.index("-t") + 1]
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    assert fh.load_execution_result(str(bad)) is None
    assert "unreadable" in capsys.readouterr().out
    assert fh.load_execution_result(str(tmp_path / "missing.json")) is None
