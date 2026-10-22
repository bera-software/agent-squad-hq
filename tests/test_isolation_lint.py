"""The job-isolation lint (.github/lint/isolation.py) passes on the repository,
catches pool-secret leaks, and the account-pool wiring is complete: every
workflow and stub that hands LLM credentials to an agent also declares and
passes the five pool slots per auth mode."""
import glob
import importlib.util
import os

import pytest
import yaml

from squadhq.account_pool import ACTIONS_SLOTS
from squadhq.paths import _SOURCE_ROOT as ROOT

LINT = os.path.join(ROOT, ".github", "lint", "isolation.py")
POOL = sorted(n for names in ACTIONS_SLOTS.values() for n in names if n[-2] == "_")


def _lint():
    spec = importlib.util.spec_from_file_location("isolation_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _on(doc):
    return doc.get("on", doc.get(True)) or {}


def test_repository_passes(capsys):
    assert _lint().main(["isolation.py", os.path.join(ROOT, ".github", "workflows")]) == 0
    assert "0 problem(s)" in capsys.readouterr().out


def _agent_job(with_extra=None, step_env=None, job_env=None):
    agent = {"id": "agent", "uses": "./.squadhq/.github/actions/run-agent",
             "with": {"anthropic-api-key": "${{ secrets.ANTHROPIC_API_KEY_1 }}", **(with_extra or {})}}
    steps = [{"name": "prep", "run": "true", "env": step_env or {}}, agent]
    job = {"permissions": {"contents": "read"}, "steps": steps}
    if job_env:
        job["env"] = job_env
    return job


@pytest.mark.parametrize("kw,bad", [
    ({}, False),
    ({"step_env": {"X": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_2 }}"}}, True),
    ({"with_extra": {"plan-dir": "${{ secrets.ANTHROPIC_API_KEY_3 }}"}}, True),
    ({"job_env": {"K": "${{ secrets.ANTHROPIC_API_KEY_4 }}"}}, True),
    ({"step_env": {"X": "${{ secrets.SQUADHQ_APP_PRIVATE_KEY }}"}}, True),
])
def test_pool_slots_only_in_executor_inputs(kw, bad):
    problems = []
    _lint().check_job("wf.yml", "agent", _agent_job(**kw), problems)
    assert bool(problems) is bad, problems


def test_agent_exec_may_receive_pool_slots_only():
    lint = _lint()
    problems = []
    data = {"jobs": {"agent": {"uses": "./.github/workflows/agent-exec.yml",
                               "secrets": {n: f"${{{{ secrets.{n} }}}}" for n in POOL}}}}
    lint.check_uses_jobs("agent-run.yml", data, problems)
    assert not problems
    data["jobs"]["agent"]["secrets"]["SQUADHQ_APP_PRIVATE_KEY"] = "x"
    lint.check_uses_jobs("agent-run.yml", data, problems)
    assert problems


def _load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


WIRED = ["router.yml", "agent-run.yml", "agent-exec.yml", "squadhq-agent.yml", "project-scan.yml"]


@pytest.mark.parametrize("name", WIRED)
def test_reusable_workflows_declare_pool_slots(name):
    declared = set(_on(_load(os.path.join(ROOT, ".github", "workflows", name)))["workflow_call"]["secrets"])
    assert set(POOL) <= declared


def test_every_llm_secret_pass_through_includes_the_pool():
    """Anything passing CLAUDE_CODE_OAUTH_TOKEN onward (stubs, hub callers of
    agent-run.yml / project-scan.yml) passes every pool slot too."""
    files = glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")) + \
        glob.glob(os.path.join(ROOT, "onboarding", "**", "*.stub"), recursive=True)
    checked = 0
    for path in files:
        for name, job in (_load(path).get("jobs") or {}).items():
            passed = job.get("secrets") if isinstance(job, dict) else None
            if isinstance(passed, dict) and "CLAUDE_CODE_OAUTH_TOKEN" in passed:
                checked += 1
                missing = sorted(set(POOL) - set(passed))
                assert not missing, f"{os.path.basename(path)}:{name} does not pass {missing}"
    assert checked >= 10


def test_agent_steps_map_the_selected_secret_name():
    """Each run-agent call maps prep's auth-secret over the declared slots and
    nothing else crosses jobs: prep exports the id and the secret NAME only."""
    for name in ("router.yml", "agent-exec.yml", "squadhq-agent.yml"):
        text = open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8").read()
        for slot in POOL:
            assert f"== '{slot}' && secrets.{slot}" in text, (name, slot)
    prep = _load(os.path.join(ROOT, ".github", "actions", "agent-prep", "action.yml"))
    assert prep["outputs"]["account"]["value"] == "${{ steps.gate.outputs.account }}"
    assert prep["outputs"]["auth-secret"]["value"] == "${{ steps.gate.outputs.auth_secret }}"
    post = _load(os.path.join(ROOT, ".github", "actions", "agent-post", "action.yml"))
    assert "account" in post["inputs"]


# Token-broker mode (ADR-0011): OIDC never reaches an agent job.

BROKER_RUN = "python3 -I - token <<'PY'\n\"\"\"squadhq-token broker client ...\"\"\"\nPY\n"


@pytest.mark.parametrize("mutate,msg", [
    (lambda j: j["permissions"].update({"id-token": "write"}), "id-token: write"),
    (lambda j: j["steps"].insert(0, {"uses": "./.squadhq/.github/actions/squadhq-token"}), "squadhq-token"),
    (lambda j: j["steps"].insert(0, {"name": "mint", "run": BROKER_RUN}), "token broker"),
    (lambda j: j["steps"].insert(0, {"name": "oidc", "run": 'curl "$ACTIONS_ID_TOKEN_REQUEST_URL"'}), "token broker"),
])
def test_agent_job_gets_no_oidc_and_mints_nothing(mutate, msg):
    job = _agent_job()
    mutate(job)
    problems = []
    _lint().check_job("wf.yml", "agent", job, problems)
    assert any(msg in p for p in problems), problems


@pytest.mark.parametrize("steps,ok", [
    ([{"uses": "./.squadhq/.github/actions/squadhq-token", "with": {"identity": "ops"}}], True),
    ([{"uses": "./.github/actions/setup-hub", "with": {"app-client-id": "x"}}], True),
    ([{"uses": "./.squadhq/.github/actions/agent-prep"}], True),
    ([{"id": "ops-broker", "run": BROKER_RUN}], True),
    ([{"uses": "./.github/actions/setup-hub", "with": {"token": "${{ github.token }}"}}], False),
    ([{"run": "echo hi"}], False),
])
def test_id_token_only_on_minting_jobs(steps, ok):
    job = {"permissions": {"contents": "read", "id-token": "write"}, "steps": steps}
    problems = []
    _lint().check_job("wf.yml", "deterministic", job, problems)
    assert (not problems) is ok, problems


def test_reusable_callers_and_release_jobs_may_hold_id_token():
    lint = _lint()
    problems = []
    lint.check_oidc("hub.yml", "run", {"permissions": {"id-token": "write"}, "uses": "./.github/workflows/agent-run.yml"},
                    problems)
    lint.check_oidc("release.yml", "pypi", {"permissions": {"id-token": "write"}, "steps": [{"run": "true"}]}, problems)
    assert not problems


def test_agent_exec_callers_and_workflow_level_id_token_are_refused():
    lint = _lint()
    problems = []
    data = {"permissions": {"id-token": "write"},
            "jobs": {"agent": {"uses": "./.github/workflows/agent-exec.yml", "permissions": {"id-token": "write"}}}}
    lint.check_uses_jobs("agent-run.yml", data, problems)
    assert any("agent-only workflow with id-token" in p for p in problems)
    assert any("workflow level" in p for p in problems)


def test_run_agent_never_talks_to_the_broker(tmp_path):
    lint = _lint()
    path = tmp_path / "action.yml"
    path.write_text(yaml.safe_dump({"runs": {"steps": [
        {"run": "python3 -I broker.py token  # squadhq-token/broker.py"},
        {"uses": "anthropics/claude-code-action@" + "0" * 40}]}}))
    problems = []
    lint.check_run_agent(str(path), problems)
    assert problems
