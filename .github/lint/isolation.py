#!/usr/bin/env python3
"""Static check of the job-isolation rules (YAML anchors resolved).

    python3 .github/lint/isolation.py [WORKFLOW_DIR ...]

Rules, for every job that runs an LLM agent (a run-agent step) or untrusted
repository code or data (stack CI, the changed-coverage gate that parses PR-built
coverage reports, the update-prs merge stage, preview QA):

  1. No App private key, no bookkeeping/server/webhook secret: the only secrets
     an agent job may reference are ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN,
     the account-pool slots ANTHROPIC_API_KEY_1..5 / CLAUDE_CODE_OAUTH_TOKEN_1..5
     and SQUADHQ_HANDOFF_KEY; an untrusted-code job references no secret at all.
     Pool slots may appear only in the run-agent step's executor inputs (never
     in env, never in another step).
  2. No token minting: no create-github-app-token or squadhq-token step, no
     token-broker / OIDC request (tokens are minted in the prep job).
  3. GITHUB_TOKEN permissions are read-only; in particular no `id-token: write`
     (an OIDC token is what the token broker trades for App tokens, ADR-0011).
  4. After the agent step: no local (`./...`) action, every remote action is
     pinned to a commit SHA, and no step references a secret.

Also checked: run-agent's executor step is the last step of the composite;
no key material crosses jobs (no `outputs.key`, no `toJSON(needs)` in env);
every `run:` step after the agent uses the hardened shell; and the agent-only
reusable workflow (agent-exec.yml) receives and declares no App secret and is
never called with id-token: write; no workflow grants id-token at the top level;
`id-token: write` appears only on jobs that mint a token (squadhq-token,
agent-prep, setup-hub without a token, the inline broker client), on callers
of reusable workflows, and on the release jobs in OIDC_OTHER; run-agent never
touches the broker.
Exit 1 on any violation.
"""
import glob
import os
import re
import sys

import yaml

POOL_SLOTS = {f"{base}_{n}" for base in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN") for n in range(1, 6)}
AGENT_SECRETS = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SQUADHQ_HANDOFF_KEY"} | POOL_SLOTS
EXECUTOR_INPUTS = {"anthropic-api-key", "claude-code-oauth-token"}
HARDENED_SHELL = "/usr/bin/env -u BASH_ENV"
UNTRUSTED_JOBS = {"stack-ci", "changed-coverage", "update-prs-merge", "qa-preview"}
SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
SHA_RE = re.compile(r"@[0-9a-f]{40}$")
# Token minting (App key or token broker). The inline broker client in the
# reusable entry workflows is recognised by its docstring.
MINT_ACTIONS = ("create-github-app-token", "actions/squadhq-token", "actions/agent-prep")
BROKER_MARKERS = ("squadhq-token broker client", "squadhq-token/broker.py", "ACTIONS_ID_TOKEN_REQUEST", "/v1/broker/")
# Jobs that need OIDC for something other than the token broker (signing, publishing).
OIDC_OTHER = {("release.yml", "build"), ("release.yml", "pypi"), ("release.yml", "image")}


def secrets_in(obj):
    return set(SECRET_RE.findall(yaml.safe_dump(obj, default_flow_style=False, width=10_000)))


MERGE_GUARDS = {
    # The ops identity is the only sender whose label/push may start these.
    "self-merge": "github.event.sender.login == (vars.SQUADHQ_BOT_LOGIN",
    "trivial-merge": "github.event.sender.login == (vars.SQUADHQ_BOT_LOGIN",
    # Started by the approving human: never a bot, trusted association only.
    "auto-merge": "github.event.review.user.type != 'Bot'",
}


TYPE_GUARDS = {
    "self-merge": "github.event.sender.type == 'Bot'",
    "trivial-merge": "github.event.sender.type == 'Bot'",
}


def _perm(perms, name):
    if isinstance(perms, str):
        return "write" if perms == "write-all" else "read" if perms == "read-all" else ""
    return str((perms or {}).get(name, "")) if isinstance(perms, dict) else ""


def mints_token(job):
    """True when the job mints an App token (either mode) and so may need id-token: write."""
    for s in job.get("steps") or []:
        u = str(s.get("uses", "")).rstrip("/")
        if any(k in u for k in MINT_ACTIONS):
            return True
        if u.endswith("actions/setup-hub") and not (s.get("with") or {}).get("token"):
            return True
        if any(m in str(s.get("run", "")) for m in BROKER_MARKERS):
            return True
    return False


def check_oidc(wf, name, job, problems):
    """id-token: write only where a token is minted (or a reusable workflow is called)."""
    if _perm(job.get("permissions"), "id-token") != "write":
        return
    if "uses" in job or (os.path.basename(wf), name) in OIDC_OTHER or mints_token(job):
        return
    problems.append(f"{wf}:{name}: id-token: write on a job that mints no token")


def check_merge_job(wf, name, job, problems):
    want = MERGE_GUARDS.get(name)
    if want and want not in str(job.get("if", "")):
        problems.append(f"{wf}:{name}: missing the sender guard ({want} ...)")
    typ = TYPE_GUARDS.get(name)
    if typ and typ not in str(job.get("if", "")):
        problems.append(f"{wf}:{name}: login comparison without the Bot type check ({typ})")
    # Every login comparison in a job condition must be paired with its type check.
    cond = str(job.get("if", ""))
    for obj in re.findall(r"(github\.event\.[a-z_.]+?)\.login ==", cond):
        if f"{obj}.type == 'Bot'" not in cond:
            problems.append(f"{wf}:{name}: {obj}.login compared without {obj}.type == 'Bot'")
    if want and "head.repo.full_name == github.repository" not in str(job.get("if", "")):
        problems.append(f"{wf}:{name}: does not refuse fork PRs")


def check_job(wf, name, job, problems):
    check_merge_job(wf, name, job, problems)
    check_oidc(wf, name, job, problems)
    steps = job.get("steps") or []
    uses = [str(s.get("uses", "")) for s in steps]
    agent_idx = next((i for i, u in enumerate(uses) if u.rstrip("/").endswith("actions/run-agent")), None)
    untrusted = name in UNTRUSTED_JOBS
    if agent_idx is None and not untrusted:
        return
    where = f"{wf}:{name}"
    allowed = AGENT_SECRETS if agent_idx is not None else set()
    bad = secrets_in(job) - allowed - {"GITHUB_TOKEN"}
    if bad:
        problems.append(f"{where}: references privileged secrets {sorted(bad)}")
    job_level = secrets_in({k: v for k, v in job.items() if k != "steps"}) & POOL_SLOTS
    if job_level:
        problems.append(f"{where}: job-level env/with references pool secrets {sorted(job_level)}")
    for i, s in enumerate(steps):
        if i == agent_idx:
            outside = {k: v for k, v in s.items() if k != "with"}
            outside["with"] = {k: v for k, v in (s.get("with") or {}).items() if k not in EXECUTOR_INPUTS}
            leaked = secrets_in(outside) & POOL_SLOTS
        else:
            leaked = secrets_in(s) & POOL_SLOTS
        if leaked:
            problems.append(f"{where}: step '{s.get('name', s.get('uses', 'run'))}' references pool secrets "
                            f"{sorted(leaked)} outside the executor inputs")
    if any("create-github-app-token" in u for u in uses):
        problems.append(f"{where}: mints a GitHub App token")
    if any(u.rstrip("/").endswith("actions/squadhq-token") for u in uses):
        problems.append(f"{where}: mints a token (squadhq-token is for prep/post jobs only)")
    for s in steps:
        if any(m in str(s.get("run", "")) for m in BROKER_MARKERS):
            problems.append(f"{where}: step '{s.get('name', s.get('id', 'run'))}' talks to the token broker / OIDC")
    perms = job.get("permissions")
    if _perm(perms, "id-token") == "write":
        problems.append(f"{where}: id-token: write — an agent or untrusted-code job must never obtain an OIDC token")
    if perms is None:
        problems.append(f"{where}: no explicit permissions")
    elif isinstance(perms, dict) and any(v == "write" for v in perms.values()):
        problems.append(f"{where}: GITHUB_TOKEN has write permissions {perms}")
    if agent_idx is not None:
        for s in steps[agent_idx + 1:]:
            u = str(s.get("uses", ""))
            if u.startswith("./"):
                problems.append(f"{where}: local action '{u}' runs after the agent")
            elif u and not SHA_RE.search(u.split(" ")[0]):
                problems.append(f"{where}: unpinned action '{u}' after the agent")
            if secrets_in(s):
                problems.append(f"{where}: step '{s.get('name', u or 'run')}' after the agent references secrets")
            if "run" in s and not str(s.get("shell", "")).startswith(HARDENED_SHELL):
                problems.append(f"{where}: run step '{s.get('name', 'run')}' after the agent does not use the hardened shell")


def check_run_agent(path, problems):
    data = yaml.safe_load(open(path))
    steps = data["runs"]["steps"]
    last = steps[-1]
    if "claude-code-action" not in str(last.get("uses", "")):
        problems.append(f"{path}: the executor is not the last step of run-agent")
    text = yaml.safe_dump(steps, width=10_000)
    if "squadhq-token" in text or any(m in text for m in BROKER_MARKERS):
        problems.append(f"{path}: run-agent mints a token or talks to the token broker")


def check_text(path, problems):
    text = open(path, encoding="utf-8").read()
    if re.search(r"outputs\.key\b", text):
        problems.append(f"{path}: key material passed through outputs (outputs.key)")
    for _m in re.finditer(r"^\s+[A-Z_]+:\s*\$\{\{\s*toJSON\(needs\)", text, re.M):
        problems.append(f"{path}: toJSON(needs) in env (printed in step logs)")


def check_uses_jobs(wf, data, problems):
    for name, job in (data.get("jobs") or {}).items():
        if isinstance(job, dict) and str(job.get("uses", "")).endswith("agent-exec.yml"):
            passed = set((job.get("secrets") or {}).keys()) if isinstance(job.get("secrets"), dict) else {"inherit"}
            bad = passed - AGENT_SECRETS
            if bad:
                problems.append(f"{wf}:{name}: passes {sorted(bad)} to the agent-only workflow")
            if _perm(job.get("permissions"), "id-token") == "write":
                problems.append(f"{wf}:{name}: calls the agent-only workflow with id-token: write")
    if _perm(data.get("permissions"), "id-token") == "write":
        problems.append(f"{wf}: id-token: write at the workflow level (grant it per minting job)")
    on = data.get("on") or data.get(True) or {}
    call = on.get("workflow_call") if isinstance(on, dict) else None
    if os.path.basename(wf) == "agent-exec.yml" and isinstance(call, dict):
        bad = set((call.get("secrets") or {}).keys()) - AGENT_SECRETS
        if bad:
            problems.append(f"{wf}: the agent-only workflow declares {sorted(bad)}")


def main(argv):
    dirs = argv[1:] or [".github/workflows"]
    problems = []
    seen_agent = 0
    for d in dirs:
        for wf in sorted(glob.glob(os.path.join(d, "*.yml"))):
            data = yaml.safe_load(open(wf)) or {}
            check_text(wf, problems)
            check_uses_jobs(wf, data, problems)
            for name, job in (data.get("jobs") or {}).items():
                if isinstance(job, dict) and "steps" in job:
                    if any(str(s.get("uses", "")).rstrip("/").endswith("actions/run-agent") for s in job["steps"]):
                        seen_agent += 1
                    check_job(os.path.basename(wf), name, job, problems)
    ra = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "actions", "run-agent", "action.yml")
    if os.path.exists(ra):
        check_run_agent(ra, problems)
    for act in glob.glob(os.path.join(os.path.dirname(ra), "..", "*", "action.yml")):
        check_text(act, problems)
    for p in problems:
        print(f"ISOLATION: {p}")
    print(f"isolation: {seen_agent} agent job(s) checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
