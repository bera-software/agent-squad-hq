"""Token-broker mode (ADR-0011): the broker client (.github/actions/squadhq-token/broker.py),
its verbatim copies in the reusable entry workflows, and the wiring that keeps App-key
mode unchanged and gives id-token: write to minting jobs only."""
import glob
import importlib.util
import json
import os
import re

import pytest
import yaml

from squadhq import settings
from squadhq.paths import _SOURCE_ROOT as ROOT

GH = os.path.join(ROOT, ".github")
BROKER = os.path.join(GH, "actions", "squadhq-token", "broker.py")
APP_TOKEN = "actions/create-github-app-token@"
JWT = "eyJhbGciOiJSUzI1NiJ9.payload.signature"
TOKEN = "ghs_" + "a" * 36


def _load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _broker():
    spec = importlib.util.spec_from_file_location("squadhq_broker", BROKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lint():
    spec = importlib.util.spec_from_file_location("isolation_lint", os.path.join(GH, "lint", "isolation.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- the copies

@pytest.mark.parametrize("name", ["router.yml", "squadhq-agent.yml"])
def test_inline_broker_client_is_a_verbatim_copy(name):
    with open(BROKER, encoding="utf-8") as f:
        source = f.read().rstrip("\n")
    runs = [s.get("run", "") for job in _load(os.path.join(GH, "workflows", name))["jobs"].values()
            for s in job.get("steps") or [] if "squadhq-token broker client" in str(s.get("run", ""))]
    assert runs, f"{name}: no inline broker client"
    for run in runs:
        head, _, rest = run.partition("\n")
        assert head == "python3 -I - token <<'PY'"
        body, _, tail = rest.rstrip("\n").rpartition("\nPY")
        assert tail == "" and body == source


def test_one_pin_for_create_github_app_token():
    pins = set()
    for path in glob.glob(os.path.join(GH, "**", "*.yml"), recursive=True):
        pins |= set(re.findall(r"actions/create-github-app-token@([0-9a-f]{40})", open(path, encoding="utf-8").read()))
    assert len(pins) == 1


def _steps(path):
    data = _load(path)
    if "runs" in data:
        yield "action", data["runs"]["steps"]
    for name, job in (data.get("jobs") or {}).items():
        yield name, job.get("steps") or []


def test_every_app_key_mint_has_a_broker_path():
    """Each create-github-app-token step is skipped in broker mode and has a broker
    sibling (workflows) or a broker branch (composites)."""
    checked = 0
    for path in glob.glob(os.path.join(GH, "**", "*.yml"), recursive=True):
        for job, steps in _steps(path):
            ids = {s.get("id") for s in steps}
            for s in steps:
                if APP_TOKEN not in str(s.get("uses", "")):
                    continue
                checked += 1
                cond = str(s.get("if", ""))
                if path.endswith(os.path.join("actions", "squadhq-token", "action.yml")):
                    assert cond == "steps.mode.outputs.broker == ''" and "broker" in ids
                elif path.endswith(os.path.join("actions", "setup-hub", "action.yml")):
                    assert "steps.cfg.outputs.broker == ''" in cond and "mint-broker" in ids
                else:
                    assert "env.SQUADHQ_TOKEN_BROKER == ''" in cond, (path, job)
                    assert f"{s['id']}-broker" in ids, (path, job)
    assert checked >= 11


def _perm_set(text):
    return {k: v for k, _, v in (e.partition("=") for e in re.split(r"[,\s]+", text)) if k and v}


def test_broker_steps_request_the_app_key_permission_set():
    """Broker mode is never broader than App-key mode: each inline broker step asks
    for exactly the permissions its App-key sibling asks for."""
    checked = 0
    for name in ("router.yml", "squadhq-agent.yml"):
        for job in _load(os.path.join(GH, "workflows", name))["jobs"].values():
            steps = {s.get("id"): s for s in job.get("steps") or []}
            for sid, s in steps.items():
                if sid and sid.endswith("-broker"):
                    app = steps[sid[: -len("-broker")]]
                    want = {k[len("permission-"):]: str(v) for k, v in app["with"].items() if k.startswith("permission-")}
                    assert _perm_set(s["env"]["TOKEN_PERMISSIONS"]) == want, (name, sid)
                    checked += 1
    assert checked >= 20
    for action in ("squadhq-token", "setup-hub"):
        data = _load(os.path.join(GH, "actions", action, "action.yml"))
        perms = {k[len("permission-"):] for k in data["inputs"] if k.startswith("permission-")}
        broker = next(s for s in data["runs"]["steps"] if s.get("id") in ("broker", "mint-broker"))
        assert set(_perm_set(broker["env"]["TOKEN_PERMISSIONS"].replace("${{ inputs.", "x").replace(" }}", ""))) \
            == perms, action


def test_every_minting_job_revokes_its_broker_tokens():
    lint = _lint()
    checked = 0
    for path in glob.glob(os.path.join(GH, "workflows", "*.yml")):
        for name, job in (_load(path).get("jobs") or {}).items():
            if "steps" in job and lint.mints_token(job):
                last = job["steps"][-1]
                assert last.get("if") == "always() && env.SQUADHQ_TOKEN_BROKER != ''", (path, name)
                assert "DELETE" in last["run"] and "squadhq-broker-tokens" in last["run"], (path, name)
                checked += 1
    assert checked >= 40


def test_agent_prep_is_always_at_the_nested_path():
    """agent-prep nests ./.squadhq/.github/actions/squadhq-token: every caller must
    have the hub there."""
    for path in glob.glob(os.path.join(GH, "workflows", "*.yml")):
        text = open(path, encoding="utf-8").read()
        for u in re.findall(r"uses: (\S*actions/agent-prep)", text):
            assert u == "./.squadhq/.github/actions/agent-prep", (path, u)


def _calls(data):
    for job in (data.get("jobs") or {}).values():
        if isinstance(job, dict) and str(job.get("uses", "")).startswith("./.github/workflows/"):
            yield job, os.path.basename(job["uses"])


def test_minting_jobs_and_their_callers_have_id_token_write():
    lint = _lint()
    wfs = {os.path.basename(p): _load(p) for p in glob.glob(os.path.join(GH, "workflows", "*.yml"))}
    minting_wf = {n for n, d in wfs.items() if any(lint.mints_token(j) for j in (d.get("jobs") or {}).values()
                                                    if isinstance(j, dict) and "steps" in j)}
    # Transitive: a workflow whose jobs call a minting workflow also mints.
    changed = True
    while changed:
        changed = False
        for n, d in wfs.items():
            if n not in minting_wf and any(callee in minting_wf and callee != "agent-exec.yml" for _, callee in _calls(d)):
                minting_wf.add(n)
                changed = True
    missing = []
    for n, d in wfs.items():
        for name, job in (d.get("jobs") or {}).items():
            needs = ("steps" in job and lint.mints_token(job)) or (
                "uses" in job and os.path.basename(str(job["uses"])) in minting_wf
                and not str(job["uses"]).endswith("agent-exec.yml"))
            if needs and lint._perm(job.get("permissions"), "id-token") != "write":
                missing.append(f"{n}:{name}")
    assert not missing, missing
    for stub in ("agents.yml.stub", os.path.join("optional", "squadhq-agent.yml.stub")):
        text = open(os.path.join(ROOT, "onboarding", stub), encoding="utf-8").read()
        assert re.search(r"^      id-token: write$", text, re.M), stub


def test_agent_jobs_never_get_id_token():
    for path in glob.glob(os.path.join(GH, "workflows", "*.yml")):
        for name, job in (_load(path).get("jobs") or {}).items():
            agentish = any(str(s.get("uses", "")).rstrip("/").endswith("actions/run-agent") for s in job.get("steps") or [])
            agentish = agentish or str(job.get("uses", "")).endswith("agent-exec.yml")
            if agentish:
                assert "id-token" not in (job.get("permissions") or {}), (path, name)


def test_every_workflow_env_carries_the_broker_variables():
    for path in glob.glob(os.path.join(GH, "workflows", "*.yml")):
        data = _load(path)
        if any(isinstance(j, dict) and "steps" in j and _lint().mints_token(j) for j in data["jobs"].values()) or \
                any(True for _ in _calls(data)):
            env = data.get("env") or {}
            assert env.get("SQUADHQ_TOKEN_BROKER") == "${{ vars.SQUADHQ_TOKEN_BROKER }}", path
            assert env.get("SQUADHQ_TOKEN_BROKER_AUDIENCE") == "${{ vars.SQUADHQ_TOKEN_BROKER_AUDIENCE }}", path


# ---------------------------------------------------------------- the client

@pytest.fixture
def env(monkeypatch, tmp_path):
    out = tmp_path / "out"
    out.write_text("")
    for k, v in {"SQUADHQ_TOKEN_BROKER": "https://api.example.test/", "GITHUB_OUTPUT": str(out),
                 "GITHUB_REPOSITORY_OWNER": "acme", "GITHUB_JOB": "review-prep", "GITHUB_RUN_ID": "42",
                 "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example.test/token?api-version=2.0",
                 "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req-secret", "RUNNER_TEMP": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    for k in ("SQUADHQ_TOKEN_BROKER_AUDIENCE", "TOKEN_IDENTITY", "TOKEN_REPOSITORIES", "TOKEN_PURPOSE",
              "TOKEN_PERMISSIONS", "TOKEN_SKIP_REVOKE",
              "USAGE_JOB", "USAGE_OUTCOME", "USAGE_RESULT"):
        monkeypatch.delenv(k, raising=False)
    return out


def _fake(mod, broker_status=200, broker_body=None, oidc_status=200):
    seen = []

    def call(req, tries=3):
        seen.append(req)
        if req.full_url.startswith("https://oidc.example.test/"):
            return oidc_status, ({"value": JWT} if oidc_status == 200 else {})
        body = broker_body if broker_body is not None else {
            "token": TOKEN, "expires_at": "2026-10-26T12:00:00Z", "identity": "ops",
            "login": "agent-squad-hq[bot]", "repositories": ["web"], "permissions": {"contents": "write"}}
        return broker_status, body
    mod.call = call
    return seen


def test_token_exchange(env, monkeypatch, capsys):
    mod = _broker()
    seen = _fake(mod)
    monkeypatch.setenv("TOKEN_REPOSITORIES", "acme/web, acme/agent-squad-hq\nacme/web")
    mod.token()
    out = capsys.readouterr().out
    oidc_req, broker_req = seen
    assert oidc_req.full_url.endswith("api-version=2.0&audience=https%3A%2F%2Fapi.example.test")
    assert oidc_req.get_header("Authorization") == "bearer req-secret"
    assert broker_req.full_url == "https://api.example.test/v1/broker/token"
    assert broker_req.get_header("Authorization") == f"Bearer {JWT}"
    assert json.loads(broker_req.data) == {"identity": "ops", "purpose": "review-prep",
                                           "repositories": ["web", "agent-squad-hq"]}
    # Masks come first; neither secret is printed in clear.
    assert out.index(f"::add-mask::{JWT}") < out.index(f"::add-mask::{TOKEN}")
    assert out.replace(f"::add-mask::{JWT}", "").replace(f"::add-mask::{TOKEN}", "").count(TOKEN) == 0
    assert JWT not in out.replace(f"::add-mask::{JWT}", "")
    lines = dict(ln.split("=", 1) for ln in env.read_text().splitlines())
    assert lines == {"token": TOKEN, "login": "agent-squad-hq[bot]", "app-slug": "agent-squad-hq",
                     "expires-at": "2026-10-26T12:00:00Z"}
    # Listed for the job's final revoke step, readable by this user only.
    listed = env.parent / "squadhq-broker-tokens"
    assert listed.read_text() == TOKEN + "\n" and listed.stat().st_mode & 0o777 == 0o600


# GitHub's stateless installation token format (rolled out 2026): ghs_<app id>_<JWT>, about 520 characters.
STATELESS = "ghs_123456_" + "eyJhbGciOiJFUzI1NiJ9." + "eyJzdWIiOiJhcHAifQ-_" * 22 + ".c2lnbmF0dXJl"


def test_stateless_tokens_are_accepted(env):
    assert len(STATELESS) > 255
    mod = _broker()
    _fake(mod, broker_body={"token": STATELESS, "expires_at": "2026-10-26T12:00:00Z", "identity": "ops",
                            "login": "agent-squad-hq[bot]"})
    mod.token()
    assert dict(ln.split("=", 1) for ln in env.read_text().splitlines())["token"] == STATELESS


@pytest.mark.parametrize("bad", ["ghs_short", "ghs_" + "a" * 40 + "\nnext=1", "ghs_" + "a" * 40 + " x",
                                 "ghs_" + "a" * 2100])
def test_unusable_tokens_are_refused(env, bad):
    mod = _broker()
    _fake(mod, broker_body={"token": bad})
    with pytest.raises(SystemExit):
        mod.token()
    assert "token=" not in env.read_text()


def test_permissions_narrow_like_the_app_key_path(env, monkeypatch):
    mod = _broker()
    seen = _fake(mod)
    monkeypatch.setenv("TOKEN_PERMISSIONS", "contents=read,issues=, pull-requests=write\nactions-variables=write")
    mod.token()
    assert json.loads(seen[1].data)["permissions"] == {"contents": "read", "pull_requests": "write",
                                                       "actions_variables": "write"}


def test_skip_revoke_leaves_the_token_to_the_post_job(env, monkeypatch):
    mod = _broker()
    _fake(mod)
    monkeypatch.setenv("TOKEN_SKIP_REVOKE", "true")
    mod.token()
    assert not (env.parent / "squadhq-broker-tokens").exists()


def test_agent_token_for_many_projects(env, monkeypatch, capsys):
    mod = _broker()
    seen = _fake(mod)
    monkeypatch.setenv("TOKEN_IDENTITY", "agent")
    monkeypatch.setenv("TOKEN_REPOSITORIES", ",".join(f"acme/p{i}" for i in range(25)))
    mod.token()
    assert len(json.loads(seen[1].data)["repositories"]) == 25
    monkeypatch.setenv("TOKEN_REPOSITORIES", ",".join(f"acme/p{i}" for i in range(26)))
    with pytest.raises(SystemExit):
        mod.token()
    assert "at most 25" in capsys.readouterr().out


def test_custom_audience_and_agent_identity(env, monkeypatch):
    mod = _broker()
    seen = _fake(mod)
    monkeypatch.setenv("SQUADHQ_TOKEN_BROKER_AUDIENCE", "https://api.squadhq.example")
    monkeypatch.setenv("TOKEN_IDENTITY", "agent")
    monkeypatch.setenv("TOKEN_PURPOSE", "implement prep")
    mod.token()
    assert seen[0].full_url.endswith("audience=https%3A%2F%2Fapi.squadhq.example")
    assert json.loads(seen[1].data) == {"identity": "agent", "purpose": "implement-prep"}


@pytest.mark.parametrize("status,body,want", [
    (403, {"error": "quota_exceeded", "detail": "monthly run quota reached"}, "HTTP 403 quota_exceeded: monthly run quota reached"),
    (401, {"error": "invalid_oidc", "detail": "bad\naudience"}, "HTTP 401 invalid_oidc: bad%0Aaudience"),
    (503, {}, "HTTP 503 unknown"),
    (0, {"error": "unreachable", "detail": "timed out"}, "HTTP 0 unreachable: timed out"),
])
def test_broker_refusal_reports_code_and_detail(env, capsys, status, body, want):
    mod = _broker()
    _fake(mod, broker_status=status, broker_body=body)
    with pytest.raises(SystemExit) as e:
        mod.token()
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert out.startswith(f"::add-mask::req-secret\n::add-mask::{JWT}\n::error title=squadhq-token::")
    assert want in out and JWT not in out.split("\n", 2)[2]
    # Only the refusal code (for agent-prep's notice on the issue), never a token.
    code = body.get("error", "unknown")
    assert env.read_text() == f"refused={code}\nhelp-url=\n"


def test_broker_refusal_outputs_a_safe_help_url(env, capsys):
    mod = _broker()
    _fake(mod, broker_status=403, broker_body={"error": "plan_limit", "help_url": "https://app.example/t/1/billing"})
    with pytest.raises(SystemExit):
        mod.token()
    assert env.read_text() == "refused=plan_limit\nhelp-url=https://app.example/t/1/billing\n"
    env.write_text("")
    _fake(mod, broker_status=403, broker_body={"error": "x\ny=1", "help_url": "javascript:alert(1)"})
    with pytest.raises(SystemExit):
        mod.token()
    assert env.read_text() == "refused=xy\nhelp-url=\n"


def test_token_in_an_error_body_is_still_masked(env, capsys):
    mod = _broker()
    _fake(mod, broker_status=500, broker_body={"token": TOKEN, "error": "x"})
    with pytest.raises(SystemExit):
        mod.token()
    out = capsys.readouterr().out
    assert f"::add-mask::{TOKEN}" in out and out.count(TOKEN) == 1


@pytest.mark.parametrize("var,value,msg", [
    ("SQUADHQ_TOKEN_BROKER", "http://api.example.test", "must be an https URL"),
    ("SQUADHQ_TOKEN_BROKER", "https://user@evil.test", "must be an https URL"),
    ("TOKEN_REPOSITORIES", "other/web", "outside acme"),
    ("TOKEN_REPOSITORIES", "acme/we b;x", "invalid repository name"),
    ("TOKEN_IDENTITY", "admin", "identity must be ops or agent"),
    ("TOKEN_PERMISSIONS", "contents=owner", "invalid permission"),
    ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "", "id-token: write"),
])
def test_refuses_bad_input_before_any_request(env, monkeypatch, capsys, var, value, msg):
    mod = _broker()
    seen = _fake(mod)
    monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit) as e:
        mod.token()
    assert e.value.code == 1 and msg in capsys.readouterr().out
    assert not [r for r in seen if "api.example.test" in r.full_url]


def test_no_redirects():
    mod = _broker()
    assert any(isinstance(h, mod._NoRedirect) for h in mod.OPENER.handlers)
    assert mod._NoRedirect().redirect_request(None, None, 307, "", {}, "https://evil.test") is None


def test_usage_report(env, monkeypatch, tmp_path, capsys):
    mod = _broker()
    seen = _fake(mod, broker_body={})
    result = tmp_path / "execution-result.json"
    result.write_text(json.dumps({"outcome": "success", "cost_usd": 0.42, "input_tokens": 1200,
                                  "output_tokens": 300, "turns": 4, "extra": {"pr_url": "https://github.com/acme/web/pull/7"}}))
    monkeypatch.setenv("USAGE_JOB", "implement")
    monkeypatch.setenv("USAGE_OUTCOME", "success")
    monkeypatch.setenv("USAGE_RESULT", str(result))
    mod.usage()
    assert seen[1].full_url == "https://api.example.test/v1/broker/usage"
    assert json.loads(seen[1].data) == {"run_id": "42", "job": "implement", "outcome": "success", "cost_usd": 0.42,
                                        "input_tokens": 1200, "output_tokens": 300,
                                        "pr_url": "https://github.com/acme/web/pull/7"}
    assert "usage reported" in capsys.readouterr().out


@pytest.mark.parametrize("setup", ["refused", "no_oidc", "bad_url"])
def test_usage_never_fails_the_run(env, monkeypatch, capsys, setup):
    mod = _broker()
    _fake(mod, broker_status=429, broker_body={"error": "rate_limited"})
    if setup == "no_oidc":
        monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL")
    if setup == "bad_url":
        monkeypatch.setenv("SQUADHQ_TOKEN_BROKER", "ftp://x")
    monkeypatch.setenv("USAGE_OUTCOME", "timed_out")
    with pytest.raises(SystemExit) as e:
        mod.usage()
    assert e.value.code == 0
    assert "::warning title=squadhq-token::" in capsys.readouterr().out


# ---------------------------------------------------------------- identities

def test_broker_mode_is_never_single_app(monkeypatch):
    monkeypatch.delenv("SQUADHQ_AGENT_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("SQUADHQ_TOKEN_BROKER", raising=False)
    assert settings.single_app_mode()
    monkeypatch.setenv("SQUADHQ_TOKEN_BROKER", "https://api.squadhq.example")
    assert not settings.single_app_mode()
    assert settings.token_broker() == "https://api.squadhq.example"


@pytest.mark.parametrize("name", ["router.yml", "squadhq-agent.yml"])
def test_agent_jobs_end_before_their_tokens_expire(name):
    # The prep job mints the agent's tokens; GitHub caps installation tokens at 1 hour.
    jobs = _load(os.path.join(GH, "workflows", name))["jobs"]
    agent = {n: j for n, j in jobs.items()
             if any("Unseal the agent's tokens" in str(s.get("name", "")) for s in j.get("steps") or [])}
    assert agent, f"{name}: no agent jobs found"
    for n, job in agent.items():
        assert job.get("timeout-minutes", 360) <= 50, f"{name}: {n}"


def test_jobs_that_check_out_with_their_own_token_can_read_private_repositories():
    """A job's GITHUB_TOKEN reads a private repository only with ``contents: read``; a job that
    lists its permissions without it fails to check out with "repository not found"."""
    import glob

    for wf in sorted(glob.glob(os.path.join(GH, "workflows", "*.yml"))):
        data = _load(wf)
        top = data.get("permissions")
        for name, job in (data.get("jobs") or {}).items():
            perms = job.get("permissions", top)
            for step in job.get("steps") or []:
                if "actions/checkout" in str(step.get("uses", "")) and not (step.get("with") or {}).get("token"):
                    assert not isinstance(perms, dict) or "contents" in perms, (os.path.basename(wf), name)


def test_graph_build_uses_only_commands_the_locked_graphify_has():
    """graph-build has no AI key: it must run graphify's code-only ``update`` (no ``extract``,
    which needs an LLM, and no flags the locked version lacks)."""
    job = _load(os.path.join(GH, "workflows", "router.yml"))["jobs"]["graph-build"]
    run = "\n".join(s.get("run", "") for s in job["steps"])
    assert "graphify update ." in run and "graphify extract" not in run and "--code-only" not in run


def test_merge_tokens_can_read_the_check_rollup():
    """merge_policy reads ``statusCheckRollup``; a merge token without checks/statuses read makes
    that read fail, and every unattended merge (trivial-merge, self-merge) then fails closed."""
    jobs = _load(os.path.join(GH, "workflows", "router.yml"))["jobs"]
    for name in ("trivial-merge", "self-merge"):
        steps = {s.get("id"): s for s in jobs[name]["steps"] if isinstance(s, dict)}
        app = steps["ops"]["with"]
        assert app.get("permission-checks") == "read" and app.get("permission-statuses") == "read", name
        assert app.get("permission-actions") == "read", name  # the rollup reads workflow names
        perms = steps["ops-broker"]["env"]["TOKEN_PERMISSIONS"]
        assert "checks=read" in perms and "statuses=read" in perms and "actions=read" in perms, name


def test_agents_keep_the_graph_in_their_pr_and_refresh_costs_nothing_then():
    """Code-editing agents may query and refresh the graph; a push that already carries the
    graph skips graph-refresh in its `if:`, before any runner starts."""
    from squadhq.executors import tool_profiles as tp

    edit = tp.claude_code_tools("edit-test")
    assert {"Bash(graphify query *)", "Bash(graphify update .)"} <= set(edit)
    assert not any("graphify" in t for t in tp.claude_code_tools("read-only"))
    cond = _load(os.path.join(GH, "workflows", "router.yml"))["jobs"]["graph-refresh"]["if"]
    assert "head_commit.modified), 'graphify-out/graph.json')" in cond
    with open(os.path.join(ROOT, "prompts", "_shared", "delivery.md"), encoding="utf-8") as f:
        assert "graphify update ." in f.read()


def test_every_auto_merge_falls_back_to_a_direct_merge_when_already_mergeable():
    # GitHub refuses --auto on a PR whose required checks already passed while an optional one
    # failed or is pending ("unstable status"); without a fallback the PR sat unmerged.
    with open(os.path.join(ROOT, ".github", "workflows", "router.yml")) as f:
        text = f.read()
    autos = text.count('--squash --auto --match-head-commit "$SHA"')
    fallbacks = text.count('"unstable status"*')
    assert autos == 3 and fallbacks == 3, (autos, fallbacks)


def test_agent_run_prep_restores_the_hub_tooling_after_the_subject_checkout():
    # actions/checkout into the workspace root deletes .squadhq (the root is not a clone yet),
    # so the prep's own actions under .squadhq/ vanished and every agent-run loop failed.
    with open(os.path.join(ROOT, ".github", "workflows", "agent-run.yml")) as f:
        steps = yaml.safe_load(f)["jobs"]["prep"]["steps"]
    names = [s.get("name") or s.get("id") for s in steps]
    subject = names.index("Check out the subject (detection only; nothing is executed)")
    restore = names.index("Restore the hub tooling (if the subject checkout removed it)")
    first_use = next(i for i, s in enumerate(steps) if i > subject and str(s.get("uses", "")).startswith("./.squadhq/"))
    assert subject < restore < first_use


def test_trivial_merge_answers_the_sweepers_ready_to_merge_retry():
    # A refused merge leaves ready-to-merge on a graph-refresh PR; the stuck sweeper re-applies it.
    # self-merge skips generated-only PRs, so trivial-merge must take that event or the PR sits.
    with open(os.path.join(ROOT, ".github", "workflows", "router.yml")) as f:
        cond = yaml.safe_load(f)["jobs"]["trivial-merge"]["if"]
    assert "github.event.label.name == 'ready-to-merge'" in cond and "github.event.sender.type == 'Bot'" in cond
