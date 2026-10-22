#!/usr/bin/env python3
"""
Failure handler: classifies a failed job run into TRANSIENT / INFRA / AGENT /
UNKNOWN, auto-retries TRANSIENT failures instead of escalating on the first
hiccup, and creates a structured hub escalation issue for everything else —
including transients that exhausted their retries (title tagged
[transient-exhausted] so it's visibly "retried, not ignored").

Modes:
  ci    claude-code-action jobs (router.yml, tech-debt-weekly.yml,
        architect-quarterly.yml, claude-concierge.yml). Classifies from the
        agent step's own `execution_file` output (steps.<agent>.outputs.
        execution_file — the action's documented output, not a hardcoded temp
        path: that path is an implementation detail the action may change,
        same reasoning squadhq.cost_ledger applies to the same output),
        falling back to a job-log grep. TRANSIENT + run_attempt<3 triggers a
        rerun of the whole run via `POST .../actions/runs/{run_id}/rerun`
        (not rerun-failed-jobs: an agent job depends on its prep job's token
        hand-off, which the post job revokes and deletes, so the prep job
        must run again too; 201 on success, requires the
        calling token's Actions:write on RUN_REPO — the job token must have
        it) instead of escalating.
  cron  hub cron workflows whose failure isn't a coding-agent step
        (squadhq.agent_runner jobs, onboard-project.yml). Classifies from a
        job-log grep only (no execution_file). Never retries — agent_runner
        already retries transient provider errors internally before ever
        reaching this handler, so anything that gets here already exhausted
        that retry.

Never raises past main() — a failure handler that itself crashes must not
mask the original failure. Always writes one $GITHUB_STEP_SUMMARY line so the
outcome is visible without opening the escalation issue.

Required env: GH_TOKEN, HUB_REPO, RUN_REPO, WORKFLOW, JOB, EVENT. Optional:
REPO (the subject/target repo for the message body — defaults to RUN_REPO;
differs from RUN_REPO for router.yml's per-project jobs and the tech-debt/
architect matrix jobs, which run IN the hub but scan a project repo),
ISSUE_OR_PR, RUN_ID, RUN_ATTEMPT (default 1), RUN_URL, EXECUTION_FILE
(ci mode), AGENT_OUTCOME (ci mode — steps.<agent>.outcome), CURRENT_MODEL (ci
mode — steps.<model>.outputs.resolved_model, the model that just failed),
CONFIG_PATH (ci mode — path to config/models.json; defaults to the hub
checkout's config/models.json, i.e. .squadhq/config/models.json in
per-project jobs), ACCOUNT (ci mode — the account-pool id the run used).

Account pool (ci mode): a capacity trip on a pool account while another
account is still available reruns the whole run (the rerun's prep job picks
the next account) instead of escalating, at most once per pool account.

Model fallback (ci mode only): a TRANSIENT failure whose error text names the
model specifically (overloaded/unavailable/not found — as distinct from
general API/network downtime) gets one extra step before the normal retry: if
config/models.json's tier for CURRENT_MODEL defines a fallback, this sets a
one-shot MODEL_OVERRIDE_<JOB> repo variable to that fallback (consumed and
cleared by squadhq.resolve_model on the retry run) before triggering the
rerun — so the retry actually runs a different model instead of hitting the
same outage again. A general (non-model-specific) transient still reruns
as-is; the same model usually recovers from a plain network/API blip. If the
override was set but the rerun call itself failed, the override is deleted
again before escalating, so it can never leak into an unrelated later run.

Escalations are deduplicated: one open issue per exact title; a recurrence
adds a "Recurred" comment instead of a new issue.
"""
import json
import os
import re
import sys

from squadhq import github
from squadhq.paths import hub_path
from squadhq.redact import redact


def sh(args):
    """Seam: ``args`` is a full ``gh ...`` argv (kept for test mocking)."""
    if args and args[0] == "gh":
        args = args[1:]
    return github.client().run(args)


def default_config_path():
    return hub_path("config", "models.json")


# Checked in this order — INFRA patterns (including billing/quota, which no
# retry fixes) first; TRANSIENT only for the narrow set of genuinely
# retryable signals. Anything else is AGENT/UNKNOWN, never a blind retry.
INFRA_PATTERNS = [
    r"credit balance is too low",
    r"billing",
    r"quota exceeded",
    r"insufficient_quota",
    r"exceeded your current quota",
    r"payment required",
    r"\b402\b",
    r"\b401\b",
    r"unauthori[sz]ed",
    r"invalid (x-)?api.?key",
    r"authentication_error",
    r"permission denied",
    r"not permitted",
    r"resource not accessible",
    r"bad credentials",
    r"403 forbidden",
    r"could not find label",
    r"label .* not found",
    r"missing (a )?secret",
    r"secret .* not set",
    r"checkout failed",
    r"fatal: could not read from remote repository",
    r"unable to resolve action",
    r"workflow file .* is invalid",
    r"the requested resource does not exist",
    r"404 not found",
    r"deny rules? (always )?beat allow rules?",
    r"tool .* is not allowed",
]

TRANSIENT_PATTERNS = [
    r"api_error",
    r"rate.?limit",
    r"too many requests",
    r"\b429\b",
    r"\b408\b",
    r"internal server error",
    r"overloaded",
    r"\boverload",
    r"timeout",
    r"timed out",
    r"etimedout",
    r"connection reset",
    r"connection refused",
    r"econnrefused",
    r"econnreset",
    r"network error",
    r"temporarily unavailable",
    r"service unavailable",
    r"bad gateway",
    r"gateway time-?out",
    r'"status"\s*:\s*(5\d\d|429|408)\b',
    r"http[s]?[^0-9]{0,10}(5\d\d|429|408)\b",
]


# Distinct from TRANSIENT_PATTERNS: these say something about THIS model
# specifically (overloaded/unavailable/deprecated/unknown), not the API in
# general — that distinction decides whether a retry should swap models
# (resolve_fallback_model) or just try the same model again.
MODEL_SPECIFIC_PATTERNS = [
    r"model.{0,25}(is )?overloaded",
    r"model.{0,25}(is )?(currently )?unavailable",
    r"model.{0,25}not found",
    r"model.{0,25}does not exist",
    r"model.{0,25}(has been )?deprecated",
    r"model_not_found",
    r"invalid model",
    r"unknown model",
    r"unsupported model",
    r"no (longer )?access to (this|that) model",
]


def _search(patterns, text):
    low = text.lower()
    for p in patterns:
        if re.search(p, low):
            return p
    return None


def is_model_specific(text):
    return _search(MODEL_SPECIFIC_PATTERNS, text) is not None


def var_name(job):
    return "MODEL_OVERRIDE_" + re.sub(r"[^A-Za-z0-9]", "_", job).upper()


def resolve_fallback_model(current_model, config_path=None):
    # Mirrors squadhq.resolve_model's tier lookup — kept as a separate,
    # tiny copy rather than a shared import so this script has zero
    # cross-file dependency (it already has to survive config/models.json
    # itself being unreadable; importing resolve_model.py would just add a
    # second way for that failure to cascade).
    if not current_model:
        return None
    try:
        with open(config_path or default_config_path()) as f:
            config = json.load(f)
    except Exception:
        return None
    for section in ("tiers", "hub_loops"):
        for tier in config.get(section, {}).values():
            primary, fallback = tier.get("primary"), tier.get("fallback")
            if current_model == primary and fallback:
                return fallback
            if current_model == fallback:
                # Already running the fallback — no further fallback to try.
                return None
    return None


def set_model_override(run_repo, job, model):
    name = var_name(job)
    r = sh(["gh", "variable", "set", name, "-R", run_repo, "-b", model])
    return r.returncode == 0


def clear_model_override(run_repo, job):
    r = sh(["gh", "variable", "delete", var_name(job), "-R", run_repo])
    return r.returncode == 0


def extract_text_from_execution_file(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        try:
            with open(path, errors="replace") as f:
                return f.read()[:4000]
        except Exception:
            return ""
    if isinstance(data, list):
        parts = []
        for m in data:
            if not isinstance(m, dict):
                continue
            if m.get("type") == "result":
                parts.append(str(m.get("result", "")))
            if m.get("is_error"):
                parts.append(json.dumps(m)[:2000])
        return "\n".join(parts) if parts else json.dumps(data)[:4000]
    if isinstance(data, dict):
        return json.dumps(data)[:4000]
    return str(data)[:4000]


def extract_text_from_log(run_repo, run_id, job_hint):
    # Fallback when execution_file is absent/empty: grep this run's own job
    # log, same technique cost_ledger uses for its metrics fallback.
    # Line-delimited pagination: pages never concatenate as `][`.
    if not (run_repo and run_id and job_hint):
        return ""
    try:
        jobs = github.client().paginate(f"repos/{run_repo}/actions/runs/{run_id}/jobs",
                                        key="jobs", default=None)
        if jobs is None:
            return ""
        candidates = [j for j in jobs if job_hint in j.get("name", "")]
        if not candidates and len(jobs) == 1:
            # Several hub cron workflows share the generic YAML job id "run"
            # (its GH Actions display name, absent a job-level `name:`
            # override) while JOB carries the more readable duty name
            # (growth/seo/legal/...) — no name match, but a single-job run
            # has no ambiguity to resolve, so just use it.
            candidates = jobs
        job = next((j for j in candidates if j.get("conclusion") == "failure"), None) \
            or (candidates[0] if candidates else None)
        if not job:
            return ""
        log = sh(["gh", "api", f"repos/{run_repo}/actions/jobs/{job['id']}/logs"])
        if log.returncode != 0:
            return ""
        return log.stdout[-8000:]  # tail — the error is almost always near the end
    except Exception:
        return ""


def classify(text, agent_outcome):
    infra_hit = _search(INFRA_PATTERNS, text)
    if infra_hit:
        return "INFRA", infra_hit
    transient_hit = _search(TRANSIENT_PATTERNS, text)
    if transient_hit:
        return "TRANSIENT", transient_hit
    if agent_outcome == "success":
        # The agent step itself completed; something else in the job failed
        # without a recognizable infra/transient signature — the agent
        # produced nothing usable rather than the infrastructure breaking.
        return "AGENT", "agent step succeeded but job still failed"
    return "UNKNOWN", ""


SUGGESTED_FIX = {
    "TRANSIENT-EXHAUSTED": "Retries exhausted on a transient provider/network "
                           "error — re-run once the provider recovers (check its "
                           "status page) or wait for the next trigger.",
    "INFRA": "Check tool grants, secrets, labels, billing/quota — this won't self-heal.",
    "AGENT": "Agent completed but produced nothing — check the prompt or "
             "ticket for ambiguity.",
    "POLICY": "A policy guard refused the run (tool allowlist, label ownership, "
              "risk floor) — fix the ticket or the policy; a retry gets the same answer.",
    "UNKNOWN": "Unclassified — inspect the run log.",
}

# ExecutionResult.error_class -> this handler's tags. CAPACITY is the capacity
# gate's business (it pauses the fleet), so here it is a non-retried INFRA —
# except on a pool account while another account is available (_pool_rerun).
_RESULT_CLASS = {"transient": "TRANSIENT", "infra": "INFRA", "agent": "AGENT",
                 "policy": "POLICY", "capacity": "INFRA"}


def step_summary(line):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as f:
        f.write(redact(line).rstrip() + "\n")


def rerun_job(run_repo, run_id):
    r = sh(["gh", "api", "--method", "POST",
            f"repos/{run_repo}/actions/runs/{run_id}/rerun"])
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def escalate(hub_repo, tag, run_url, run_attempt, job, context_repo, event, issue_or_pr, sig):
    sig = redact(sig)  # defence in depth: nothing un-redacted reaches an issue
    subject = f"{job} on {context_repo}"
    if event:
        subject += f" for {event}" + (f" #{issue_or_pr}" if issue_or_pr else "")
    title = f"ESCALATION [{tag}]: {job} failed ({context_repo})"
    body = (
        f"## Failure classification: {tag}\n"
        f"**Run:** {run_url} (attempt {run_attempt})\n"
        f"**Job:** {subject}\n"
        f"**Error signature:** {sig or '(no error text captured — see run log)'}\n"
        f"**Suggested fix:** {SUGGESTED_FIX.get(tag, SUGGESTED_FIX['UNKNOWN'])}"
    )
    number, _created = github.client().find_or_create_issue_by_title(
        hub_repo, title, body, labels=["escalation"],
        comment_body=f"Recurred: run {run_url} (attempt {run_attempt}).\n\n"
                     f"**Error signature:** {sig[:300] if sig else '(none captured)'}")
    return number is not None, title


def load_execution_result(path=None):
    """The neutral execution-result.json (squadhq.interfaces.ExecutionResult)
    named by EXECUTION_RESULT, or None when unset/missing/unreadable — callers
    then fall back to the executor-native EXECUTION_FILE."""
    path = path if path is not None else os.environ.get("EXECUTION_RESULT", "")
    if not path or not os.path.exists(path):
        return None
    try:
        from squadhq.executors.result import read_result
        return read_result(path)
    except Exception as e:
        print(f"execution result {path!r} unreadable ({e!r}) — falling back to EXECUTION_FILE")
        return None


def _pool_rerun(result, hub_repo, run_repo, run_id, run_attempt):
    """A capacity trip on one pool account while another is still available:
    rerun the whole run — its prep job re-resolves and picks the next account
    (capacity_gate check-trip, earlier in the post job, already tripped this
    one). Bounded by the pool size so a pool that keeps tripping escalates."""
    if result is None:
        return False
    capacity = result.capacity_tripped or getattr(result.error_class, "value", "") == "capacity"
    account = getattr(result, "account", "") or os.environ.get("ACCOUNT", "")
    if not capacity or not account or not run_id:
        return False
    try:
        from squadhq import capacity_gate
        configured, available, size = capacity_gate.pool_has_available(hub_repo)
    except Exception as e:
        print(f"failure_handler: pool state unreadable ({e!r}) — no pool rerun")
        return False
    if not configured or not available or run_attempt > size:
        return False
    ok, out = rerun_job(run_repo, run_id)
    if ok:
        step_summary(f"⟳ CAPACITY on pool account {account} (attempt {run_attempt}) — "
                     "rerunning on the next available account")
        print(f"failure_handler: capacity on account {account}, rerun on the next pool account")
        return True
    step_summary(f"⚠ CAPACITY on pool account {account}: rerun API call failed ({out[:200]!r}) — escalating instead")
    return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "ci"
    hub_repo = os.environ.get("HUB_REPO", "")
    run_repo = os.environ.get("RUN_REPO") or os.environ.get("REPO", "")
    context_repo = os.environ.get("REPO") or run_repo
    job = os.environ.get("JOB", "")
    event = os.environ.get("EVENT", "")
    issue_or_pr = os.environ.get("ISSUE_OR_PR") or ""
    run_id = os.environ.get("RUN_ID", "")
    try:
        run_attempt = int(os.environ.get("RUN_ATTEMPT") or "1")
    except ValueError:
        run_attempt = 1
    run_url = os.environ.get("RUN_URL", "")

    result = load_execution_result() if mode == "ci" else None
    result_tag = _RESULT_CLASS.get(getattr(getattr(result, "error_class", None), "value", ""))
    model_specific_hint = bool(result is not None and (result.extra or {}).get("model_specific"))
    if result_tag:
        # The executor already classified it — trust the neutral schema.
        text = result.error_message or ""
        classification, hit = result_tag, f"execution result error_class={result.error_class.value}"
    else:
        if mode == "ci":
            text = (result.error_message if result is not None else "") or \
                extract_text_from_execution_file(os.environ.get("EXECUTION_FILE", ""))
            if not text:
                text = extract_text_from_log(run_repo, run_id, job)
            agent_outcome = os.environ.get("AGENT_OUTCOME", "")
        else:
            text = extract_text_from_log(run_repo, run_id, job)
            agent_outcome = ""
        classification, hit = classify(text, agent_outcome)
    sig = redact(text.strip())[:500]

    if mode == "ci" and _pool_rerun(result, hub_repo, run_repo, run_id, run_attempt):
        return

    if mode == "ci" and classification == "TRANSIENT" and run_attempt < 3:
        current_model = os.environ.get("CURRENT_MODEL", "") or (result.model if result is not None else "")
        config_path = os.environ.get("CONFIG_PATH") or default_config_path()
        fallback = None
        if model_specific_hint or is_model_specific(text):
            fallback = resolve_fallback_model(current_model, config_path)
        if fallback and set_model_override(run_repo, job, fallback):
            ok, out = rerun_job(run_repo, run_id)
            if ok:
                step_summary(f"⟳ TRANSIENT (model-specific): {current_model} "
                             f"unavailable — retrying with fallback {fallback}")
                print(f"failure_handler: TRANSIENT model-specific, retrying "
                      f"{current_model} -> {fallback} (attempt {run_attempt})")
                return
            # The one-shot override must not outlive the rerun it was set for.
            cleared = clear_model_override(run_repo, job)
            step_summary(f"⚠ TRANSIENT (model-specific): set fallback "
                         f"{fallback} but retry API call failed "
                         f"({out[:200]!r}) — override {'cleared' if cleared else 'NOT cleared'}, "
                         f"escalating instead")
        else:
            ok, out = rerun_job(run_repo, run_id)
            if ok:
                step_summary(f"⟳ TRANSIENT failure (attempt {run_attempt}): {hit} — auto-retrying")
                print(f"failure_handler: TRANSIENT, retried (attempt {run_attempt})")
                return
            # The retry call itself failed (permission gap, bad run_id, etc.) —
            # fall through to escalation rather than silently dropping this.
            step_summary(f"⚠ TRANSIENT failure (attempt {run_attempt}): retry API "
                         f"call failed ({out[:200]!r}) — escalating instead")

    tag = "TRANSIENT-EXHAUSTED" if classification == "TRANSIENT" else classification
    ok, title = escalate(hub_repo, tag, run_url, run_attempt, job, context_repo,
                          event, issue_or_pr, sig)
    marker = "🚨" if ok else "⚠"
    detail = hit or (sig[:80] if sig else "no error text captured")
    suffix = f"escalated: {title}" if ok else "escalation issue creation FAILED"
    step_summary(f"{marker} {tag} failure (attempt {run_attempt}): {detail} — {suffix}")
    print(f"failure_handler: {tag}, escalated={ok}")


def run(argv=None):
    """Entry point: never raises — a crashing failure handler must not mask
    the original failure."""
    try:
        main(argv)
    except Exception as e:
        try:
            step_summary(f"⚠ failure_handler itself crashed: {e!r} — original job failure NOT auto-classified")
        except Exception as e2:
            print(f"failure_handler: could not write step summary: {e2!r}")
        print(f"failure_handler: swallowed error: {e!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
