#!/usr/bin/env python3
"""
Cost ledger: records one action-based claude-code-action run as a JSON-line
comment on the pinned monthly hub issue "Cost ledger — <month>" (created on
first use, label dept:finance). Comments are append-only, queryable via gh,
and survive runner death.

LEDGER SCHEMA v7 (see the "Record cost" step in router.yml's `review` job for
the canonical env-var wiring — copy verbatim to all call sites): date, repo,
workflow, job, event, issue_or_pr, cost_usd, model, run_id, run_attempt,
run_url, duration_ms, num_turns, billing (v3: "subscription"|"api", the
capacity resolver's resolved auth_mode — null on rows predating the resolver),
skipped_capacity (v3: true when the capacity breaker skipped the agent step
entirely — cost_usd 0, duration/turns null), session_resumed (v4: true|false
when SESSION_RESUMED is set — only review-fix/mention set it, via their "Check
session context" step; null everywhere else, including rows predating v4 —
lets finance compare fix-round turns/duration resumed-vs-cold), model_fallback
(v5: true|false from MODEL_FALLBACK — set by every claude-code-action call
site's "Resolve model" step, steps.model.outputs.used_fallback; null on rows
predating v5 — lets finance report fallback frequency per §2/§7's model-
fallback policy), auto_merged (v6: true|false from AUTO_MERGED — set ONLY by
router.yml's `self-merge` job, one row per ready-to-merge label event: true
when the PR merged with no approver click (either merge_mode:"auto" or a
config/flows.yml merge_policy.auto_merge match), false when the policy did not
match and an approver merges. null everywhere else, including every row
predating v6 — so the weekly rollup reports "N PRs auto-merged this week, M
awaited an approver" by counting true-vs-false over NON-NULL rows only, and never
mistakes "this job has no opinion" for "awaited an approver"), review_skipped
(v7: true|false from REVIEW_SKIPPED — set by router.yml's `trivial-merge` job,
the only path where a PR merges with NO reviewer verdict at all because every
changed file matched config/flows.yml merge_policy.trivial_paths. null
everywhere else, including every row predating v7. Counting true rows is how
finance reports how many Sonnet reviews the trivial tier saved). `model` itself
always reflects the model that ACTUALLY ran
(the "Resolve model" step's resolved_model output — the tier's primary, or a
fallback when one shot the model-specific TRANSIENT retry), never the
configured primary blindly. Field test for additions: does it change what
finance flags or what the approver clicks? If not, leave it out — fat lines
make the finance gather expensive.

Never raises past main() — cost recording must never fail the calling job.
Invoked as: python -m squadhq.cost_ledger (env-driven, see router.yml etc).
Dates and the ledger month are UTC.

Required env: HUB_REPO, REPO, WORKFLOW, JOB, EVENT, MODEL. Optional:
ISSUE_OR_PR, RUN_ID, RUN_ATTEMPT, RUN_URL, AUTH_MODE (the capacity resolver's
gate output — "api"|"subscription"|"skip"; skip short-circuits the whole line),
EXECUTION_FILE (steps.<agent>.outputs.execution_file), SESSION_RESUMED
("true"|"false" from the "Check session context" step; only review-fix/mention
set it — anything else leaves session_resumed null in the recorded line),
MODEL_FALLBACK ("true"|"false" from the "Resolve model" step; anything else
leaves model_fallback null in the recorded line), AUTO_MERGED ("true"|"false"
from router.yml's self-merge and trivial-merge jobs; anything else leaves
auto_merged null), REVIEW_SKIPPED ("true"|"false" from router.yml's
trivial-merge job; anything else leaves review_skipped null),
GITHUB_RUN_ID (for the log-grep fallback). GH_TOKEN must be set for gh CLI.
"""
import datetime
import json
import os
import re
import sys

from squadhq import github


def sh(args):
    """Seam: ``args`` is a full ``gh ...`` argv (kept for test mocking)."""
    if args and args[0] == "gh":
        args = args[1:]
    return github.client().run(args)


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def extract_metrics(execution_file):
    # execution_file is the action's own documented output (the path to the
    # Claude Code execution output file) — not a hardcoded path, since that
    # path is an implementation detail the action itself may change. It's the
    # CLI's --output-format json "result" message: total_cost_usd, duration_ms,
    # num_turns are documented top-level fields on that message.
    empty = {"cost_usd": None, "duration_ms": None, "num_turns": None}
    if not execution_file or not os.path.exists(execution_file):
        return empty
    try:
        with open(execution_file) as f:
            data = json.load(f)
    except Exception:
        return empty
    if isinstance(data, list):
        results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
        data = results[-1] if results else (data[-1] if data else {})
    if not isinstance(data, dict):
        return empty

    def num(key):
        v = data.get(key)
        return v if isinstance(v, (int, float)) else None

    cost = num("total_cost_usd")
    return {
        "cost_usd": round(cost, 6) if cost is not None else None,
        "duration_ms": num("duration_ms"),
        "num_turns": num("num_turns"),
    }


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


def metrics_from_result(result):
    """ExecutionResult -> the ledger's metric fields (None when not priced)."""
    if result is None or result.cost_usd is None:
        return None
    return {
        "cost_usd": round(result.cost_usd, 6),
        "duration_ms": int(result.duration_s * 1000) if result.duration_s is not None else None,
        "num_turns": result.turns,
    }


def extract_metrics_from_log(hub_repo, run_id, job_hint):
    # Fallback if the execution_file path/shape ever changes: grep this job's
    # own (by-then-finalized) log for the same fields.
    empty = {"cost_usd": None, "duration_ms": None, "num_turns": None}
    if not (hub_repo and run_id and job_hint):
        return empty
    try:
        # Line-delimited pagination: pages never concatenate as `][`.
        jobs = github.client().paginate(f"repos/{hub_repo}/actions/runs/{run_id}/jobs",
                                        key="jobs", default=None)
        if jobs is None:
            return empty
        job = next((j for j in jobs if job_hint in j.get("name", "")), None)
        if not job:
            return empty
        log = sh(["gh", "api", f"repos/{hub_repo}/actions/jobs/{job['id']}/logs"])
        if log.returncode != 0:
            return empty

        def last_match(pattern):
            m = re.findall(pattern, log.stdout)
            return m[-1] if m else None

        cost = last_match(r'"total_cost_usd"\s*:\s*([0-9.]+)')
        duration = last_match(r'"duration_ms"\s*:\s*([0-9.]+)')
        turns = last_match(r'"num_turns"\s*:\s*([0-9.]+)')
        return {
            "cost_usd": round(float(cost), 6) if cost is not None else None,
            "duration_ms": int(float(duration)) if duration is not None else None,
            "num_turns": int(float(turns)) if turns is not None else None,
        }
    except Exception:
        return empty


LEDGER_BODY = ("Append-only cost ledger. One comment per action-based agent run, "
               "each a single JSON line (schema v7: date, repo, workflow, job, "
               "event, issue_or_pr, cost_usd, model, run_id, run_attempt, run_url, "
               "duration_ms, num_turns, billing, skipped_capacity, session_resumed, "
               "model_fallback, auto_merged, review_skipped — older comments may "
               "lack newer fields, parse tolerantly). Machine-read by the finance agent.")


def find_or_create_ledger_issue(hub_repo, title):
    """The month's ledger issue, open OR closed (a closed past month is still
    the ledger for that month), created on first use."""
    number, _created = github.client().find_or_create_issue_by_title(
        hub_repo, title, LEDGER_BODY, labels=["dept:finance"], state="all")
    return number


def main():
    def opt_int(name):
        v = os.environ.get(name)
        try:
            return int(v) if v not in (None, "") else None
        except ValueError:
            return None

    def opt_bool(name):
        # "Check session context" steps emit the literal strings "true"/"false";
        # anything else (unset, empty, a job that never wires this env var) is
        # "not applicable" — null, not False, so finance can tell "cold" apart
        # from "the concept doesn't apply to this job".
        v = os.environ.get(name)
        return True if v == "true" else False if v == "false" else None

    base = {
        "date": utc_today().isoformat(),
        "repo": os.environ.get("REPO", ""),
        "workflow": os.environ.get("WORKFLOW", ""),
        "job": os.environ.get("JOB", ""),
        "event": os.environ.get("EVENT", ""),
        "issue_or_pr": os.environ.get("ISSUE_OR_PR") or None,
        "model": os.environ.get("MODEL", ""),
        "run_id": opt_int("RUN_ID"),
        "run_attempt": opt_int("RUN_ATTEMPT"),
        "run_url": os.environ.get("RUN_URL") or None,
        "session_resumed": opt_bool("SESSION_RESUMED"),
        "model_fallback": opt_bool("MODEL_FALLBACK"),
        # v6. Only router.yml's self-merge job sets this. null everywhere
        # else means "this job never decides who merges" — NOT the same as
        # "an approver merged", so the rollup must count true-vs-false over
        # non-null rows only.
        "auto_merged": opt_bool("AUTO_MERGED"),
        # v7. Only router.yml's trivial-merge job sets this. null elsewhere
        # means "a reviewer's involvement was never in question for this job",
        # NOT "a review happened" — count true rows, never false-vs-null.
        "review_skipped": opt_bool("REVIEW_SKIPPED"),
    }

    auth_mode = os.environ.get("AUTH_MODE", "")
    if auth_mode == "skip":
        # Capacity breaker skipped the agent step entirely — nothing ran, no
        # execution_file to read. Record the skip itself so finance can see
        # paused windows in the same ledger it already reads.
        line = dict(base, cost_usd=0, billing=None, skipped_capacity=True,
                    duration_ms=None, num_turns=None)
    else:
        result = load_execution_result()
        if result is not None and not base["model"] and result.model:
            base["model"] = result.model
        metrics = metrics_from_result(result) or extract_metrics(os.environ.get("EXECUTION_FILE", ""))
        if metrics["cost_usd"] is None:
            metrics = extract_metrics_from_log(
                os.environ.get("HUB_REPO", ""),
                os.environ.get("GITHUB_RUN_ID", ""),
                os.environ.get("JOB", ""),
            )
        line = dict(base, cost_usd=metrics["cost_usd"],
                    billing=auth_mode or None,
                    duration_ms=metrics["duration_ms"], num_turns=metrics["num_turns"])
        if result is not None and getattr(result, "account", ""):
            # Only with an account pool (absent otherwise, so lines stay lean):
            # finance can see which account a metered run billed.
            line["account"] = result.account

    hub_repo = os.environ.get("HUB_REPO", "")
    month = utc_today().strftime("%Y-%m")
    number = find_or_create_ledger_issue(hub_repo, f"Cost ledger — {month}")
    if number is None:
        print("cost_ledger: could not find or create the ledger issue — skipping")
        return

    if not github.client().comment(hub_repo, number, "```json\n" + json.dumps(line) + "\n```"):
        print(f"cost_ledger: could not comment on #{number} — line NOT recorded: {line}")
        return
    print(f"cost_ledger: recorded {line}")


def run():
    """Entry point: never raises — cost recording must never fail the job."""
    try:
        main()
    except Exception as e:
        print(f"cost_ledger: swallowed error: {e!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
