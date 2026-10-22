#!/usr/bin/env python3
"""
Generic agent runner for the HQ repo.

Subcommands:
  cos    -> Chief of Staff triage loop (runs every 30-60 min via cron)
  brief  -> Daily Brief (runs once per day before your check-in)

Flags:
  --dry-run   Calls the model for real (real tokens spent, real cost) but
              never writes to GitHub and never commits state/usage.json —
              prints what it WOULD have done instead. Position-independent:
              `agent_runner.py brief --dry-run` and `agent_runner.py
              --dry-run brief` both work.

Design:
  1. Gather context from GitHub (issues/PRs across every registered project repo in
     config/projects.json, plus the hub repo) via `gh` CLI.
  2. Enforce a monthly token budget (state/usage.json in the hub repo,
     updated with a compare-and-set write through the Contents API).
  3. Call the configured LLM provider with the agent's prompt file + context.
  4. The model returns a JSON list of actions; we execute them via `gh`
     (create_issue / comment / add_labels / remove_labels / close_issue only,
     each gated by the role's flows.yml authority — agents here can never
     merge, push code, or touch deployments).

Required env:
  GH_TOKEN            GitHub token, minted per-run from the hub's GitHub App
                       (covers both the hub and every registered project repo)
  SQUADHQ_HUB_REPO    owner/name of the hub (legacy HQ_REPO accepted)
  provider API key    for whichever provider the mode's model uses
                       (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...)
Optional env:
  MONTHLY_BUDGET_USD  default "50"

Model selection is NOT an env var — config/models.json's "hub_loops" tiers are
the single source of truth (entries may be "provider/model"; a bare id uses
SQUADHQ_DEFAULT_PROVIDER, and the call goes through squadhq.providers) (a mode's tier is whichever hub_loops.*.jobs list
contains it — named "jobs" for schema consistency with the tiers.*.jobs lists
claude-code-action jobs use, even though these entries are mode arguments, not
JOB env values). This mirrors squadhq.resolve_model's tier lookup for
claude-code-action jobs; see that script's docstring and config/models.json's
_notes for the fallback policy and the research behind the chosen IDs.
"""
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time  # noqa: F401  (call_claude's retry backoff; tests patch agent_runner.time.sleep)
import urllib.request
from collections import namedtuple

from squadhq import board_sync, flow_reader, github, label_guard, settings, untrusted
from squadhq.redact import redact
from squadhq.paths import hub_path


class AnthropicAPIError(RuntimeError):
    """Raised by call_claude when the API call fails after exhausting retries.
    Never caught anywhere in this file — must propagate uncaught so the CI
    step fails and the workflow's `if: failure()` -> failure_handler.py cron
    path escalates it. A typed subclass (not bare RuntimeError) so it reads
    unambiguously in a traceback/job log as "the API call itself failed",
    distinct from a model reply that came back empty (see LLMReply below,
    which is what call_claude returns on SUCCESS — including a successful
    call that produced zero output text, which is a different failure mode
    entirely from this one)."""

# One LLM call's full result: the text (may be empty — a valid, billable
# response with zero output text is NOT an error, see incident #46) plus
# enough diagnostics (stop_reason, usage, which model actually answered) to
# root-cause an empty or unparseable reply without re-running the job.
LLMReply = namedtuple("LLMReply", ["text", "stop_reason", "input_tokens", "output_tokens", "model", "raw"])

def _mode_from_argv(argv=None):
    # Skip flag-like args (e.g. --dry-run) so `agent_runner.py --dry-run brief`
    # and `agent_runner.py brief --dry-run` both resolve mode correctly.
    for a in (sys.argv[1:] if argv is None else argv):
        if not a.startswith("--"):
            return a
    return "cos"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_today():
    return _utc_now().date()


def _load_model_tier(mode, config_path=None):
    config_path = config_path or hub_path("config", "models.json")
    try:
        with open(config_path) as fh:
            config = json.load(fh)
    except Exception as e:
        print(f"agent_runner: could not read {config_path}: {e!r} — "
              f"falling back to a hardcoded default model", flush=True)
        return None
    for tier in config.get("hub_loops", {}).values():
        if mode in tier.get("jobs", []):
            return tier
    print(f"agent_runner: mode {mode!r} not in any hub_loops tier's jobs "
          f"list — falling back to a hardcoded default model", flush=True)
    return None

# Run configuration. Nothing here reads argv, the environment or the disk at
# import time — configure() (called from main) fills these in. The defaults
# keep the module importable and call_claude usable from tests.
DEFAULT_MODEL = "claude-haiku-5-5"
MODE = "cos"
DRY_RUN = False
MODEL = DEFAULT_MODEL
FALLBACK_MODEL = None
BUDGET = 50.0
HQ = None
PROJECTS: list[dict] = []
ALLOWED_REPOS: set[str] = set()
USAGE_FILE = "state/usage.json"  # path inside the hub repo (and the local checkout)


def _hq():
    """The hub repo: configure()'s value, else resolved (and cached) from the
    environment on first use — so importing the module never needs it."""
    global HQ
    if HQ is None:
        HQ = settings.hub_repo()
    return HQ


def _budget_from_env():
    try:
        return float(os.environ.get("MONTHLY_BUDGET_USD") or "50")
    except ValueError:
        return 50.0


def _load_projects():
    try:
        with open(hub_path("config", "projects.json")) as fh:
            return (json.load(fh) or {}).get("projects", []) or []
    except FileNotFoundError:
        return []


def configure(argv=None):
    """Resolve the run's mode, model tier, hub repo, project registry and
    budget. Called from main(); returns the mode."""
    global MODE, DRY_RUN, MODEL, FALLBACK_MODEL, BUDGET, HQ, PROJECTS, ALLOWED_REPOS
    argv = sys.argv[1:] if argv is None else list(argv)
    MODE = _mode_from_argv(argv)
    DRY_RUN = "--dry-run" in argv
    tier = _load_model_tier(MODE)
    MODEL = tier["primary"] if tier else DEFAULT_MODEL
    FALLBACK_MODEL = tier.get("fallback") if tier else None
    BUDGET = _budget_from_env()
    HQ = settings.hub_repo()
    PROJECTS = _load_projects()
    ALLOWED_REPOS = {p["repo"] for p in PROJECTS} | {HQ}
    return MODE
# Prices, long-prompt tiers and per-model "thinking off" values now live in
# src/squadhq/providers/prices.yml (the model catalogue every provider
# adapter reads). These three names are thin compatibility shims built from
# it for code that still imports them; edit prices.yml, not these.
from squadhq.providers import prices as _model_catalogue  # noqa: E402 - kept beside the shims it feeds
from squadhq.providers import resolve as _resolve_model_ref  # noqa: E402

def _catalogue_rates():
    out = {}
    for prov in _model_catalogue.catalogue().get("providers", {}):
        for name, e in _model_catalogue.models(prov).items():
            if "input" in e and "output" in e:
                pair = (float(e["input"]), float(e["output"]))
                out[f"{prov}/{name}"] = pair
                if prov == "anthropic":
                    out[name] = pair  # bare Claude ids, as config/models.json uses them
    return out

def _catalogue_long_rates():
    out = {}
    for prov in _model_catalogue.catalogue().get("providers", {}):
        for name, e in _model_catalogue.models(prov).items():
            lc = e.get("long_context")
            if isinstance(lc, dict):
                v = (int(lc["threshold"]), (float(lc["input"]), float(lc["output"])))
                out[f"{prov}/{name}"] = v
                if prov == "anthropic":
                    out[name] = v
    return out

# $/MTok (input, output), keyed by bare Claude id and by "provider/model".
RATES = _catalogue_rates()
# Long-prompt pricing: the WHOLE request is re-priced once input exceeds the
# threshold (Haiku 5.5: $0.50/$2.50 above 100k input tokens).
LONG_PROMPT_RATES = _catalogue_long_rates()

def rate_for(model, input_tokens):
    """(input, output) $/MTok for a model ref at this prompt size.

    Unknown models keep the historical conservative default (3.0, 15.0) so
    the monthly budget guard never under-counts."""
    prov, name = _resolve_model_ref(model)
    found = _model_catalogue.rates(prov, name, input_tokens)
    return found if found is not None else (3.0, 15.0)

# Per-model "thinking off" setting (#46: adaptive thinking is on by default
# from Sonnet 5 on and counts against max_tokens). Sourced from prices.yml's
# thinking_off entries — the Anthropic adapter sends exactly these values;
# models without one (Haiku 4.5, Sonnet 4.6) get no thinking field, since a
# wrong value is a 400 ("disabled" is rejected by Sonnet 5.5).
THINKING_OFF = {
    name: dict(e["thinking_off"])
    for name, e in _model_catalogue.models("anthropic").items()
    if isinstance(e.get("thinking_off"), dict)
}
# Board sync only applies to project-repo issues; the hub has no board column for itself.
BOARD_STATUS_LABELS = {"status:in-progress": "in_progress", "status:in-review": "in_review"}

def board_sync_note(repo, number, state, error):
    msg = f"⚠️ Board sync failed for {repo}#{number} -> {state}: {error!r}"
    print(msg, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"\n{msg}\n")

_SUBPROCESS_TIMEOUT = 120


def _exec(args):
    """-> CompletedProcess. `gh` goes through the shared client (timeouts,
    transient retries); anything else (git) runs with a timeout."""
    if args and args[0] == "gh":
        return github.client().run(args[1:])
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timeout after {_SUBPROCESS_TIMEOUT}s")


def sh(args, check=True):
    r = _exec(args)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(args)}\n{r.stderr}")
    return r.stdout

def gh_json(args):
    out = sh(["gh"] + args)
    return json.loads(out) if out.strip() else []


def _limit_of(args):
    for i, a in enumerate(args):
        if a in ("--limit", "-L") and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                return None
    return None


def gh_json_safe(args, warnings, label):
    # A single repo/API failure (e.g. transient gh/token error) must not kill the whole gather loop.
    try:
        out = gh_json(args)
    except Exception as e:
        warnings.append(f"{label}: {e}")
        return []
    limit = _limit_of(args)
    if limit and isinstance(out, list) and len(out) >= limit:
        # A capped list silently drops the rest — say so, so the model (and
        # the run summary) knows the view is partial.
        warnings.append(f"{label}: hit the {limit}-item cap — older items not included")
    return out


def bot_git_identity():
    """(name, email) for bot commits — the shared, cached App identity."""
    return github.bot_identity()

CHECK_STATE_MAP = {
    "SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail",
    "PENDING": "pending", "IN_PROGRESS": "pending", "QUEUED": "pending",
    "SKIPPED": "skipped", "CANCELLED": "cancelled", "NEUTRAL": "neutral",
}

def enrich_pr_checks(pr, repo):
    # `gh pr checks` exits non-zero when any check fails, so call subprocess
    # directly rather than through sh()'s check=True.
    try:
        r = _exec(["gh", "pr", "checks", str(pr["number"]), "-R", repo, "--json", "name,state"])
        checks = json.loads(r.stdout) if r.stdout.strip() else []
        counts = {}
        for c in checks:
            label = CHECK_STATE_MAP.get(c.get("state", "").upper(), c.get("state", "unknown").lower())
            counts[label] = counts.get(label, 0) + 1
        pr["checks"] = checks
        pr["checks_summary"] = " / ".join(f"{v} {k}" for k, v in counts.items()) if counts else "no checks"
    except Exception:
        pr["checks"] = "unavailable"
        pr["checks_summary"] = "unavailable"

# ---------- budget ----------
def _empty_usage(month):
    return {"month": month, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}


def _month():
    return _utc_today().strftime("%Y-%m")


def _usage_for_month(u, month):
    """A usage dict for `month`: `u` when it is a well-formed record for that
    month (the shipped seed is `{}`), else a zeroed one."""
    if not isinstance(u, dict) or u.get("month") != month:
        return _empty_usage(month)
    out = _empty_usage(month)
    out.update(u)
    return out


def load_usage():
    month = _month()
    try:
        with open(hub_path(USAGE_FILE)) as fh:
            u = json.load(fh)
    except (FileNotFoundError, ValueError):
        u = None
    return _usage_for_month(u, month)

def load_cost_ledger():
    # Reads this month's "Cost ledger — <month>" hub issue (squadhq.cost_ledger
    # posts one JSON-line comment per action-based claude-code-action run) and
    # sums by workflow/job. Complements load_usage(), which only covers the
    # cheap runner agents (cos/brief/etc) — this covers the unmetered ones
    # (Reviewer, Implementer, Designer, Mention, Concierge, debt/quarterly scans).
    #
    # billing (v3 field) splits runs into "subscription" (Max plan, $0
    # marginal — reported via run-count/duration/turns proxies, not dollars)
    # and "api" (metered — dollars count toward total_usd). Legacy rows from
    # before the capacity resolver existed have no billing field; those were
    # all API-key runs (the only mode that existed then), so they're folded
    # into the api total for continuity rather than dropped.
    month = _month()
    title = f"Cost ledger — {month}"
    result = {"issue": None, "total_usd": 0.0, "run_count": 0, "unpriced_runs": 0,
              "capacity_skips": 0, "by_job": {}, "by_billing": {},
              "fallback_activations": {"count": 0, "by_job": {}, "by_model": {}, "recent": []}}
    try:
        issues = gh_json(["issue", "list", "-R", _hq(), "--search", f'"{title}" in:title',
                           "--state", "all", "--json", "number,title,author"])
    except Exception:
        return result
    # Only the bot's own ledger issue, and below only the bot's own lines: a
    # forged issue or a pasted JSON line must not move the totals.
    number = next((it["number"] for it in issues if it.get("title") == title and untrusted.by_bot(it)), None)
    if number is None:
        return result
    result["issue"] = number
    try:
        # REST, not `gh issue view --json comments`: REST gives the unforgeable
        # `<slug>[bot]` login plus user.type, so a human named like the bot is rejected.
        comments = github.client().paginate(f"repos/{_hq()}/issues/{number}/comments")
    except Exception:
        return result

    def num(row, key):
        # Tolerant of schema v1/v2 rows (no run_id/duration_ms/billing/etc):
        # a missing key is just a missing value, never a parse failure.
        v = row.get(key)
        return v if isinstance(v, (int, float)) else None

    for c in comments:
        if not untrusted.by_bot(c):
            result.setdefault("ignored_untrusted_rows", 0)
            result["ignored_untrusted_rows"] += 1
            continue
        m = re.search(r"```json\s*(\{.*?\})\s*```", c.get("body", ""), re.S)
        if not m:
            continue
        try:
            row = json.loads(m.group(1))
        except ValueError:  # noqa: S112 — a malformed ledger line is skipped, by design
            continue
        result["run_count"] += 1
        if row.get("skipped_capacity"):
            result["capacity_skips"] += 1
            continue

        if row.get("model_fallback"):
            # A row's own "job" field is one of the router.yml/tech-debt/
            # architect/concierge JOB values (review, scan, ...), distinct
            # from "workflow" — fallback frequency is naturally per-JOB since
            # config/models.json's tiers key off that same value.
            fb = result["fallback_activations"]
            fb["count"] += 1
            job_name = row.get("job", "?")
            model_name = row.get("model", "?")
            fb["by_job"][job_name] = fb["by_job"].get(job_name, 0) + 1
            fb["by_model"][model_name] = fb["by_model"].get(model_name, 0) + 1
            if len(fb["recent"]) < 10:
                fb["recent"].append({
                    "job": job_name, "repo": row.get("repo"), "model": model_name,
                    "date": row.get("date"), "run_url": row.get("run_url"),
                })

        billing = row.get("billing") or "api"  # pre-resolver rows were all api
        bb = result["by_billing"].setdefault(billing, {"runs": 0, "cost_usd": 0.0})
        bb["runs"] += 1

        key = f"{row.get('workflow', '?')}/{row.get('job', '?')}"
        bucket = result["by_job"].setdefault(key, {
            "cost_usd": 0.0, "runs": 0,
            "_duration_sum": 0.0, "_duration_n": 0,
            "_turns_sum": 0.0, "_turns_n": 0,
            "top_cost_usd": None, "top_run_url": None,
        })
        bucket["runs"] += 1
        cost = num(row, "cost_usd")
        if cost is not None:
            bucket["cost_usd"] += cost
            bb["cost_usd"] += cost
            if billing == "api":
                result["total_usd"] += cost
            if bucket["top_cost_usd"] is None or cost > bucket["top_cost_usd"]:
                bucket["top_cost_usd"] = cost
                bucket["top_run_url"] = row.get("run_url")
        else:
            result["unpriced_runs"] += 1
        duration = num(row, "duration_ms")
        if duration is not None:
            bucket["_duration_sum"] += duration
            bucket["_duration_n"] += 1
        turns = num(row, "num_turns")
        if turns is not None:
            bucket["_turns_sum"] += turns
            bucket["_turns_n"] += 1

    for bucket in result["by_job"].values():
        bucket["avg_duration_ms"] = (
            round(bucket["_duration_sum"] / bucket["_duration_n"])
            if bucket["_duration_n"] else None)
        bucket["avg_num_turns"] = (
            round(bucket["_turns_sum"] / bucket["_turns_n"], 1)
            if bucket["_turns_n"] else None)
        for k in ("_duration_sum", "_duration_n", "_turns_sum", "_turns_n"):
            del bucket[k]

    result["top_jobs"] = sorted(
        ({"job": k, **v} for k, v in result["by_job"].items()),
        key=lambda x: x["cost_usd"], reverse=True)[:3]
    return result


def load_capacity_state():
    # Reads state/capacity.json straight from the hub repo (this script
    # already runs there, but the Contents API read matches the same
    # fail-open pattern as capacity_gate.py, and needs the SAME view a
    # resolver would see). Used by finance to cross-reference API spend on
    # days the breaker tripped (failover cost) against the trip history.
    try:
        out = sh(["gh", "api", f"repos/{_hq()}/contents/state/capacity.json",
                   "--jq", ".content"], check=False)
        if not out.strip():
            return {"paused_until": "", "tripped_by": "", "tripped_at": "", "history": []}
        return json.loads(base64.b64decode(out.strip()).decode())
    except Exception:
        return {"paused_until": "", "tripped_by": "", "tripped_at": "", "history": []}

LESSONS_FILE = "org/lessons.md"
OKRS_FILE = "org/strategy/okrs.md"


def load_org_memory(warnings):
    # Organizational memory (org/lessons.md, org/strategy/okrs.md) for the two
    # digest agents. They have NO file-read tool — same constraint that forced
    # flow_reader.context_slice_for_role() — so a "read org/lessons.md" line in
    # their prompt would be inert text. Inject the data instead.
    #
    # Only stats and the current-quarter OKR block are injected, never the whole
    # lessons file: brief/rollup report ON the file's health, they don't need to
    # quote its contents, and a growing file would silently inflate every run's
    # input tokens forever.
    out = {
        "lessons": {"total_entries": None, "added_last_7d": None,
                    "last_modified": None, "weeks_since_change": None},
        "okrs": {"current_quarter": None, "body": None, "placeholder_krs": None,
                 "total_krs": None},
    }
    try:
        with open(hub_path(LESSONS_FILE)) as fh:
            lines = fh.read().splitlines()
        # A real entry is "- [date] [scope] lesson". Commented-out examples in
        # the header start with '#' and must not be counted as content — that
        # miscount is exactly what would make a stagnant file look healthy.
        entries = [l for l in lines if l.lstrip().startswith("- [")]
        out["lessons"]["total_entries"] = len(entries)
    except Exception as e:
        warnings.append(f"org_memory: could not read {LESSONS_FILE}: {e!r}")

    try:
        since = (_utc_today() - datetime.timedelta(days=7)).isoformat()
        # Count added ENTRY lines, not added lines. `--numstat` would also count
        # header/comment edits and a reflowed paragraph, which is how a file that
        # gained no actual lessons can look like it grew. Match the same
        # "- [date] ..." shape total_entries uses, so the two numbers are
        # comparable — "3 of 21" means something, "3 of 21" where the 3 counts
        # markdown does not.
        raw = sh(["git", "-C", hub_path(), "log", f"--since={since}", "-p", "--format=",
                  "--", LESSONS_FILE], check=False)
        out["lessons"]["added_last_7d"] = sum(
            1 for l in raw.splitlines() if l.startswith("+- ["))
        last = sh(["git", "-C", hub_path(), "log", "-1", "--format=%cI", "--", LESSONS_FILE],
                  check=False).strip()
        if last:
            out["lessons"]["last_modified"] = last
            try:
                changed = datetime.datetime.fromisoformat(last).date()
                out["lessons"]["weeks_since_change"] = (
                    _utc_today() - changed).days // 7
            except ValueError:  # noqa: S110 — unparseable git date: leave weeks_since_change None
                pass
    except Exception as e:
        warnings.append(f"org_memory: could not read {LESSONS_FILE} history: {e!r}")

    try:
        with open(hub_path(OKRS_FILE)) as fh:
            text = fh.read()
        # Current quarter only: the live "## Q<N> <year>" section, cut before
        # "## Archive". Commented-out example blocks (<!-- ... -->) are stripped
        # so the model never reports an example objective as a real one.
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        m = re.search(r"^##\s+(Q\d\s+\d{4}[^\n]*)$", text, re.M)
        if m:
            out["okrs"]["current_quarter"] = m.group(1).strip()
            body = text[m.end():]
            stop = re.search(r"^##\s+Archive", body, re.M)
            if stop:
                body = body[:stop.start()]
            # 8000, not 3000: the real Q3 2026 block is ~3.1k chars and the old
            # cap silently truncated the last objective mid-file, so the digests
            # would have reported on O1/O2 and never known O3 existed. A
            # truncated strategy doc is worse than an expensive one — this is
            # injected once per brief/rollup run, so the extra ~1k tokens is
            # noise against the cost of reporting against a partial anchor.
            body = body.strip()[:8000]
            out["okrs"]["body"] = body
            # Count KRs whose target is still a placeholder, so the digests can
            # say "the OKRs are not written yet" instead of reporting confident
            # progress against template text. Matches the real formats in use:
            # a bold "- **KR1:** ..." bullet as well as a bare "KR1: ...", and
            # placeholders written as <angle>, $X, or a bare X%/N% target.
            # The earlier `KR\d:\s*<` pattern caught none of these — it assumed
            # an unbolded KR followed immediately by an angle bracket, and so
            # scored a file with a real "< $X/month" target as fully written.
            kr_lines = [l for l in body.splitlines() if re.search(r"\bKR\d+\b", l)]
            placeholder = re.compile(r"<[^<>\n]{1,60}>|\$X\b|\bX%|\bN%|\bTBD\b", re.I)
            out["okrs"]["total_krs"] = len(kr_lines)
            out["okrs"]["placeholder_krs"] = sum(
                1 for l in kr_lines if placeholder.search(l))
    except Exception as e:
        warnings.append(f"org_memory: could not read {OKRS_FILE}: {e!r}")

    return out


def load_hub_open_prs(warnings):
    # The hub's own open PRs. gather() collects PRs per PROJECT repo only, so
    # without this the digests cannot see the Lessons Harvester's or the OKR
    # Tracker's output at all — both deliver a hub PR, not an issue.
    prs = gh_json_safe(["pr", "list", "-R", _hq(), "--state", "open", "--limit", "30",
                        "--json", "number,title,url,createdAt,headRefName,isDraft"],
                       warnings, "hub_open_prs")
    for pr in prs:
        pr["state"] = "OPEN"
    return prs


WHY_RE = re.compile(r"why\s+this\s+matters", re.I)


def _body_trusted(item):
    """An issue body counts as an approved statement only from our bot or a
    trusted association. gh issue list/view return author but not the
    association, so a body with no association info is trusted only when the
    bot wrote it — or when authorAssociation is present and trusted."""
    return untrusted.by_automation_or_trusted(item)  # PO/agents write these bodies


def has_why_this_matters(body):
    return bool(WHY_RE.search(body or ""))


DELIVERED_MARK = "Delivered — moved to review"
TRIAGED_WAIT_HOURS = 24


def _hours_since(ts):
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return round((_utc_now() - t).total_seconds() / 3600, 1)


def gh_list_safe(path, warnings, label):
    """Every element of a paginated REST list (no 100-item truncation);
    a failure is a warning, never an exception."""
    try:
        return github.client().paginate(path)
    except Exception as e:
        warnings.append(f"{label}: {e}")
        return []


def load_approver_queue(repo, open_issues, warnings):
    """Two states only an approver (or the LLM Chief of Staff) can move, which
    the deterministic stuck sweeper deliberately leaves alone — computed here
    in code so the Daily Brief lists them from facts, not from its own reading:

      reports_to_close  status:in-review, no open PR, and the Engineer's REPORT
                        result ("Delivered — moved to review") posted — the
                        approver closes it when the result is accepted.
      triaged_waiting   triaged:ready for > TRIAGED_WAIT_HOURS without dispatch,
                        with the Chief of Staff's latest comment on it (usually
                        why it is holding: WIP, missing "Why this matters", ...).
    """
    q = {"reports_to_close": [], "triaged_waiting": []}
    labelled = [(it, {l["name"] for l in it.get("labels", [])}) for it in open_issues]
    review = [it for it, ls in labelled if "status:in-review" in ls]
    waiting = [it for it, ls in labelled if "triaged:ready" in ls
               and not ls & {"agent:implement", "needs-design", "needs-human"}]
    if not (review or waiting):
        return q
    prs = gh_json_safe(["pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                        "--json", "number,closingIssuesReferences"], warnings, "approver_queue_prs")
    with_pr = {r["number"] for p in prs for r in (p.get("closingIssuesReferences") or [])}
    for it in review:
        if it["number"] in with_pr:
            continue
        comments = gh_list_safe(f"repos/{repo}/issues/{it['number']}/comments",
                                warnings, f"comments#{it['number']}")
        hit = [c for c in comments if untrusted.by_automation(c) and (c.get("body") or "").startswith("[Engineer]")
               and DELIVERED_MARK in (c.get("body") or "")]
        if hit:
            q["reports_to_close"].append({"number": it["number"], "title": it["title"],
                                          "delivered_at": hit[-1]["created_at"],
                                          "waiting_hours": _hours_since(hit[-1]["created_at"]),
                                          "url": hit[-1].get("html_url")})
    for it in waiting:
        events = gh_list_safe(f"repos/{repo}/issues/{it['number']}/events",
                              warnings, f"events#{it['number']}")
        times = [e["created_at"] for e in events
                 if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == "triaged:ready"]
        hours = _hours_since(max(times)) if times else None
        if hours is None or hours < TRIAGED_WAIT_HOURS:
            continue
        comments = gh_list_safe(f"repos/{repo}/issues/{it['number']}/comments",
                                warnings, f"comments#{it['number']}")
        cos = [c for c in comments if untrusted.by_bot(c) and (c.get("body") or "").startswith("[Chief of Staff]")]
        q["triaged_waiting"].append({"number": it["number"], "title": it["title"], "waiting_hours": hours,
                                     "last_cos_note": (_fence_text(cos[-1]["body"], f"{repo}#{it['number']}",
                                                                   cos[-1].get("user"), 300,
                                                                   cos[-1].get("author_association"))
                                                       if cos else None)})
    return q


VERDICT_LABELS = {"verdict:proceed", "verdict:backlog", "verdict:decline", "verdict:overridden"}

def load_po_verdicts(days, warnings):
    # PO "verdict:*" labels (prompts/product-owner.md's VERDICT section) are
    # an audit trail on PROJECT-repo issues, distinct from priority/dispatch
    # labels. A verdict:proceed issue CLOSES once shaped/dispatched — search
    # --state all, not just open issues, or every proceed verdict silently
    # drops out and pushback-health skews toward "all backlog/decline" no
    # matter how healthy the real ratio is. "Updated in the last N days" is a
    # proxy for "verdict applied recently," not an exact verdict-date filter
    # (same staleness-proxy convention used elsewhere in this file, e.g. the
    # 2-hour stall check) — a label applied weeks ago on an issue that got an
    # unrelated comment yesterday would still match; acceptable at rollup/
    # brief's cadence and volume.
    since = (_utc_today() - datetime.timedelta(days=days)).isoformat()
    result = {"since": since, "by_project": {}}
    for p in PROJECTS:
        repo = p["repo"]
        issues = gh_json_safe(
            ["issue", "list", "-R", repo, "--state", "all", "--search", f"updated:>={since}",
             "--limit", "100", "--json", "number,title,labels,updatedAt,body,author"],
            warnings, f"po_verdicts({repo})")
        counts = {k: 0 for k in VERDICT_LABELS}
        items = []
        for it in issues:
            names = {l["name"] for l in it.get("labels", [])}
            hit = names & VERDICT_LABELS
            if not hit:
                continue
            for k in hit:
                counts[k] += 1
            items.append({
                "number": it["number"], "title": it.get("title", ""),
                "verdicts": sorted(hit), "updatedAt": it.get("updatedAt"),
                "body": _fence_text(it.get("body"), f"{repo}#{it['number']}", it.get("author"), 400),
            })
        result["by_project"][repo] = {"counts": counts, "items": items}
    return result

def _usage_delta(u, before):
    before = before or {}
    return {k: (u.get(k) or 0) - (before.get(k) or 0)
            for k in ("input_tokens", "output_tokens", "est_cost_usd")}


def _usage_warning(text):
    print(f"⚠️  {text}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"\n## ⚠️ USAGE COMMIT LOST\n{text}\n")


def save_usage(u, before=None):
    """Persist this run's spend. Writes the local file, then ADDS this run's
    delta (u - before) onto the hub's state/usage.json with a compare-and-set
    write (Contents API sha): concurrent runs re-read and re-apply instead of
    overwriting each other. -> True when the hub copy was updated."""
    delta = _usage_delta(u, before)
    try:
        path = hub_path(USAGE_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(u, fh, indent=2)
    except OSError as e:
        print(f"agent_runner: could not write local usage file: {e!r}", flush=True)
    try:
        _hq()
    except RuntimeError as e:
        _usage_warning(f"USAGE WRITE SKIPPED: {e}")
        return False
    month = u.get("month") or _month()

    def mutate(remote):
        cur = _usage_for_month(remote, month)
        cur["input_tokens"] += delta["input_tokens"]
        cur["output_tokens"] += delta["output_tokens"]
        cur["est_cost_usd"] = round(cur["est_cost_usd"] + delta["est_cost_usd"], 6)
        for k in ("last_model", "last_model_fallback"):
            if k in u:
                cur[k] = u[k]
        u.update({k: cur[k] for k in ("input_tokens", "output_tokens", "est_cost_usd")})
        return cur

    try:
        ok = github.client().update_json_file(
            _hq(), USAGE_FILE, mutate, f"chore: usage {month} +${delta['est_cost_usd']:.4f}", default={})
    except Exception as e:
        ok = False
        print(f"agent_runner: usage write raised {e!r}", flush=True)
    if not ok:
        _usage_warning(
            f"USAGE WRITE FAILED: state/usage.json for {month} (+${delta['est_cost_usd']:.4f} this run) "
            f"was NOT saved to {_hq()} — every compare-and-set attempt lost or errored. Cost tracking "
            f"under-counts this run until it is added by hand.")
    return ok


def budget_guard(u, mode):
    if u["est_cost_usd"] >= BUDGET:
        # Open (or find) an escalation issue and stop. Any OPEN issue with the
        # exact title counts — the title is month-scoped already.
        title = f"BUDGET CAP HIT: ${BUDGET:.0f} for {u['month']}"
        badge = MODE_BADGE.get(mode, "[Runner]")
        # find_or_create matches only an issue OUR bot opened (bot_only), so a
        # pre-opened look-alike cannot swallow the escalation.
        github.client().find_or_create_issue_by_title(
            _hq(), title, f"{badge} Agent loops halted until you raise MONTHLY_BUDGET_USD or a new month starts.",
            labels=["escalation"])
        print("Budget cap reached — exiting.")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            ts = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(summary, "a") as f:
                f.write(f"⏭ SKIP {mode}: monthly budget cap (${BUDGET:.0f}) reached "
                        f"for {u['month']} — halted until raised or month rolls over [{ts}]\n")
        sys.exit(0)

# ---------- context ----------
ISSUE_FIELDS = "number,title,labels,updatedAt,body,author"
REPORT_LABEL = "report"
PR_FIELDS = "number,title,labels,updatedAt,isDraft,url"

def _login(author):
    if isinstance(author, dict):
        return author.get("login") or "unknown"
    return author if isinstance(author, str) and author else "unknown"


def _fence_text(text, source, author=None, limit=800, association=None):
    """Truncate untrusted text and wrap it in an <untrusted> fence (see
    prompts/_untrusted-input.md). None/empty stays as-is."""
    if not text:
        return text
    return untrusted.fence(text, source=source, author=_login(author),
                           association=association or "UNKNOWN", max_chars=limit)


def _associations(repo, warnings):
    """{issue number: author_association} for a repo's open issues (REST;
    the gh CLI's issue JSON lacks the field). Failure -> {} (only bot-authored
    bodies are then trusted — fail safe)."""
    try:
        items = github.client().paginate(f"repos/{repo}/issues?state=open&per_page=100")
    except Exception as e:
        warnings.append(f"author associations: {e}")
        return {}
    return {it.get("number"): str(it.get("author_association") or "").upper() for it in items
            if isinstance(it, dict)}


def _fence_body(item, repo, limit=800):
    item["body"] = _fence_text(item.get("body"), f"{repo}#{item.get('number', '?')}",
                               item.get("author"), limit, item.get("authorAssociation"))


def gather(mode):
    ctx = {"today": str(_utc_today()), "hq_repo": _hq(), "projects": [], "warnings": []}
    for p in PROJECTS:
        proj = {"name": p["name"], "repo": p["repo"],
                "config": {k: v for k, v in p.items() if k not in ("name", "repo")},
                "warnings": []}
        # Department reports (label "report", 2026-10-10) live in the project
        # repo now but are not work items: kept out of open_issues so they
        # never eat its cap, and gathered separately as recent_reports — the
        # Rollup/Brief read them, reporters read their own last one to avoid
        # repeats. 15 days covers the janitor's 14-day retention.
        proj["open_issues"] = gh_json_safe(
            ["issue", "list", "-R", p["repo"], "--state", "open", "--limit", "60",
             "--search", f"-label:{REPORT_LABEL}", "--json", ISSUE_FIELDS],
            proj["warnings"], "open_issues")
        since = (_utc_today() - datetime.timedelta(days=15)).isoformat()
        proj["recent_reports"] = gh_json_safe(
            ["issue", "list", "-R", p["repo"], "--state", "all", "--label", REPORT_LABEL,
             "--limit", "30", "--search", f"created:>={since}",
             "--json", ISSUE_FIELDS + ",createdAt,state"],
            proj["warnings"], "recent_reports")
        for it in proj["recent_reports"]:
            _fence_body(it, p["repo"])
        proj["open_prs"] = gh_json_safe(
            ["pr", "list", "-R", p["repo"], "--state", "open", "--limit", "30", "--json", PR_FIELDS],
            proj["warnings"], "open_prs")
        for pr in proj["open_prs"]:
            enrich_pr_checks(pr, p["repo"])
            pr["state"] = "OPEN"
        if mode in ("cos", "brief", "finance", "rollup"):
            # cos needs merged PR bodies to detect "Gate-for: #N" design PRs and
            # auto-dispatch implementation once the gate clears.
            proj["recent_merged"] = gh_json_safe(
                ["pr", "list", "-R", p["repo"], "--state", "merged", "--limit", "15",
                 "--json", "number,title,mergedAt,body,author"], proj["warnings"], "recent_merged")
            for pr in proj["recent_merged"]:
                _fence_body(pr, p["repo"])
                pr["state"] = "MERGED"
        if mode == "brief":
            proj["approver_queue"] = load_approver_queue(p["repo"], proj["open_issues"], proj["warnings"])
        if mode == "cos":
            # Debt intake dedupe needs SETTLED debt too, not only open issues —
            # a re-proposal of a declined item is the duplicate that got through.
            since = (_utc_today() - datetime.timedelta(days=90)).isoformat()
            proj["recently_closed_debt"] = gh_json_safe(
                ["issue", "list", "-R", p["repo"], "--state", "closed", "--label", "debt", "--limit", "60",
                 "--search", f"closed:>={since}", "--json", "number,title,stateReason,closedAt"],
                proj["warnings"], "recently_closed_debt")
        # gh issue list has no authorAssociation field; the REST list does.
        assoc = _associations(p["repo"], proj["warnings"]) if proj["open_issues"] else {}
        for it in proj["open_issues"]:
            it.setdefault("authorAssociation", assoc.get(it.get("number"), ""))
            # Decided in code on the FULL body, before truncation: the model
            # used to re-judge this every cycle (and flip) from an 800-char cut.
            # A section typed by an outsider is not an impact justification:
            # only bot- or trusted-authored bodies count.
            it["has_why_this_matters"] = has_why_this_matters(it.get("body")) and _body_trusted(it)
            _fence_body(it, p["repo"])
        ctx["projects"].append(proj)
    ctx["hq_open_issues"] = gh_json_safe(
        ["issue", "list", "-R", _hq(), "--state", "open", "--limit", "100", "--json", ISSUE_FIELDS],
        ctx["warnings"], "hq_open_issues")
    for it in ctx["hq_open_issues"]:
        _fence_body(it, _hq())
    if mode in ("brief", "finance", "rollup"):
        # The hub janitor keeps only the NEWEST issue of each report series
        # open (2026-10-17), so a week of briefs/weeklies is no longer in
        # hq_open_issues. The digests read that history here, open or closed,
        # exactly like a project's recent_reports.
        since = (_utc_today() - datetime.timedelta(days=8)).isoformat()
        ctx["hq_recent_reports"] = gh_json_safe(
            ["issue", "list", "-R", _hq(), "--state", "all", "--limit", "60",
             "--search", f"created:>={since} -label:escalation",
             "--json", ISSUE_FIELDS + ",createdAt,state"],
            ctx["warnings"], "hq_recent_reports")
        for it in ctx["hq_recent_reports"]:
            _fence_body(it, _hq())
    if mode in ("brief", "finance", "rollup"):
        ctx["usage"] = load_usage()
    if mode in ("brief", "finance", "rollup"):
        # brief needs this for fallback_activations.recent (Health section:
        # "any run in the last 24h used a fallback model") — finance/rollup
        # already used the rest of load_cost_ledger() for their own reports.
        ctx["cost_ledger"] = load_cost_ledger()
    if mode in ("brief", "rollup"):
        # brief filters "items" down to yesterday's date itself (Priority
        # calls: backlog/decline from yesterday); rollup uses the full
        # 7-day window for pushback-health counts. One shared 7-day fetch
        # covers both rather than querying twice.
        ctx["po_verdicts"] = load_po_verdicts(7, ctx["warnings"])
        # Organizational memory + the hub's own open PRs. Both digests report on
        # the Lessons Harvester and OKR Tracker, and BOTH of those deliver a hub
        # PR rather than an issue — invisible to the per-project PR gather above
        # and to hq_open_issues. Without these two lines the "Lessons health" and
        # "OKR progress" sections of those prompts would be instructions to
        # report data the model cannot reach.
        ctx["org_memory"] = load_org_memory(ctx["warnings"])
        ctx["hub_open_prs"] = load_hub_open_prs(ctx["warnings"])
    if mode == "finance":
        ctx["capacity"] = load_capacity_state()
    try:
        # config/flows.yml is this agent's label registry/authority/limits —
        # this mode's prompt (agents/<mode>/prompt.md) points here for those
        # rather than embedding them, since a hub-loop agent has no file-read
        # tool to follow a bare pointer at runtime (see
        # flow_reader.context_slice_for_role's docstring). Fails open (a
        # missing/broken flows.yml must never block a triage cycle) — a
        # warning surfaces instead of a silent empty ctx["flows"].
        ctx["flows"] = flow_reader.context_slice_for_role(MODE_ROLE.get(mode, mode))
    except Exception as e:
        ctx["warnings"].append(f"flows.yml: could not load context for mode {mode!r}: {e!r}")
    return ctx

def _is_no_inputs(ctx, mode):
    # "No inputs" = every data source we tried came back empty AND at least
    # one source failed outright (ctx["warnings"]) — i.e. there's a concrete
    # reason to believe the emptiness is a fetch problem, not a genuinely
    # quiet day. A clean, warning-free quiet day (zero issues/PRs, no fetch
    # errors) still calls the model: agents/daily-brief/prompt.md explicitly
    # wants a "<name>: quiet." line posted even when nothing happened —
    # skipping the call on every quiet day would silently break the daily
    # habit this agent exists for, and gather()'s ctx never becomes
    # STRUCTURALLY empty either way (today/hq_repo/usage/cost_ledger/
    # po_verdicts are always present, even on total fetch failure) — so
    # "empty" alone can't distinguish the two cases; the warnings can.
    # Only 'brief' has a clean, uniform "genuinely nothing to report" shape
    # to check this way today; other modes are left alone.
    if mode != "brief" or not ctx.get("warnings"):
        return False
    for proj in ctx.get("projects", []):
        if proj.get("open_issues") or proj.get("open_prs") or proj.get("recent_merged"):
            return False
    return not ctx.get("hq_open_issues")

# ---------- model call ----------
# Retried as transient (5xx, request timeout, rate limit); anything else fails
# immediately — retrying a malformed/unauthorized request three times just
# burns the same wrong call, it never becomes right.
_TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_MAX_RETRIES = 2  # 3 attempts total

def call_claude(system_prompt, user_content, usage, use_web=False):
    """One hub-loop LLM call through the provider registry.

    Despite the historical name, MODEL / FALLBACK_MODEL may be any
    "provider/model" ref (config/models.json hub_loops tiers; bare ids mean
    SQUADHQ_DEFAULT_PROVIDER, default anthropic). Retry/fallback policy lives
    in squadhq.providers.complete_with_fallback: transient failures retry the
    same model with backoff, model-specific ones switch to the fallback, and
    the last of _MAX_RETRIES + 1 attempts always goes to the fallback.

    Returns LLMReply (text may be empty — a valid, billable empty reply is not
    an error, #46). Raises AnthropicAPIError (any provider) once attempts are
    exhausted or on a non-retryable error; never swallowed into "".
    """
    import socket
    from squadhq.interfaces import AdapterError, Message
    from squadhq.providers import complete_with_fallback, get_provider, price_for

    def _urlopen_compat(req, timeout):
        # Routes through urllib.request.urlopen(req) — the seam the tests
        # mock — while still bounding the call with a socket timeout.
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            return urllib.request.urlopen(req)  # noqa: S310 - squadhq.http validated the https scheme
        finally:
            socket.setdefaulttimeout(old)

    try:
        result = complete_with_fallback(
            MODEL, FALLBACK_MODEL,
            system=system_prompt,
            messages=[Message(role="user", content=user_content)],
            # 12000 (2026-10-10, was 8000): Haiku/Sonnet 5.5 use the newer
            # tokenizer — the same text is ~30% more tokens than on Haiku 4.5.
            max_tokens=12000,
            web_search=use_web,
            timeout=300.0,
            max_attempts=_MAX_RETRIES + 1,
            provider_factory=lambda name: get_provider(name, transport=_urlopen_compat),
            on_event=lambda m: print(f"call_claude: {m}", flush=True),
        )
    except AdapterError as e:
        prov = _resolve_model_ref(MODEL)[0]
        label = "Anthropic" if prov == "anthropic" else prov
        if e.status is not None:
            raise AnthropicAPIError(f"{label} API {e.status}: {e.body}") from None
        raise AnthropicAPIError(f"{label} API error: {e}") from None

    reply = result.reply
    inp, out = reply.usage.input_tokens, reply.usage.output_tokens
    cost = price_for(result.used_ref, reply.usage)
    if cost is None:
        print(f"call_claude: no price for {result.used_ref!r} in prices.yml — "
              f"cost counted at the conservative default rate", flush=True)
        ri, ro = rate_for(result.used_ref, inp)
        cost_usd = inp / 1e6 * ri + out / 1e6 * ro
    else:
        cost_usd = float(cost)
    usage["input_tokens"] += inp
    usage["output_tokens"] += out
    usage["est_cost_usd"] += cost_usd
    # Snapshot of the model THIS run actually used — overwritten every call,
    # not accumulated; a breadcrumb for "what ran last", not a breakdown.
    usage["last_model"] = result.used_ref
    usage["last_model_fallback"] = result.used_fallback
    # stop_reason stays provider-native (Anthropic: end_turn / max_tokens /
    # tool_use) except that every provider's token-cap stop reads
    # "max_tokens", which _empty_reply_escalation keys on.
    stop = "max_tokens" if reply.stop_reason == "max_tokens" else (reply.raw_stop_reason or reply.stop_reason)
    return LLMReply(text=reply.text, stop_reason=stop, input_tokens=inp, output_tokens=out,
                    model=result.used_ref, raw=reply.raw)

# ---------- action executor ----------
ALLOWED = {"create_issue", "comment", "add_labels", "remove_labels", "close_issue"}
URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")

def _run(cmd):
    r = _exec(cmd)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()

def _current_labels(repo, number):
    # Best-effort idempotency check for add_labels/remove_labels — catches
    # model-level duplicate actions (e.g. re-applying agent:implement to an
    # already-dispatched gate-clear on every cycle) at the executor layer,
    # not just via prompt instructions. Returns None (unknown) on any
    # failure so the caller falls back to the original always-call-gh
    # behavior instead of skipping on bad information.
    code, stdout, _ = _run(["gh", "issue", "view", str(number), "-R", repo, "--json", "labels"])
    if code != 0:
        return None
    try:
        return {l["name"] for l in json.loads(stdout).get("labels", [])}
    except Exception:
        return None

# Models shorten the HQ repo to an alias ("hub", "hq") instead of copying
# ctx["hq_repo"]. Map aliases (and a missing/empty repo) to HQ; anything else
# stays as-is and still has to pass ALLOWED_REPOS.
HQ_ALIASES = {"hub", "hq", "hub repo", "hq repo", settings.DEFAULT_HUB_NAME}


def _hq_aliases():
    names = set(HQ_ALIASES)
    if _hq():
        names.add(_hq().split("/", 1)[-1].lower())
    return names


def _repo_of(a):
    repo = a.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        return _hq()
    repo = repo.strip()
    return _hq() if repo.lower() in _hq_aliases() else repo


_NUMBER_RE = re.compile(r"^#?\s*(\d{1,9})$")


def _as_number(value):
    """A model-supplied issue number -> int, or None when it is not one.
    bool is rejected explicitly (True is an int in Python)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        m = _NUMBER_RE.match(value.strip())
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


# Kinds that may proceed when the role's authority cannot be read (flows.yml
# broken): low-impact, reversible. create/close fail CLOSED.
_FAIL_OPEN_KINDS = {"comment", "add_labels", "remove_labels"}


def _role_authority(role):
    """flows.yml roles.<role>.authority -> {"action_types": set, "repos": set},
    or None when it cannot be read."""
    try:
        auth = (flow_reader.get_role(role) or {}).get("authority") or {}
    except Exception as e:
        print(f"  role authority unavailable for {role!r}: {e!r}", flush=True)
        return None
    return {"action_types": set(auth.get("action_types") or []), "repos": set(auth.get("repos") or [])}


def _authority_block_reason(kind, repo, authority):
    """Why `kind` on `repo` is outside the role's authority, or None."""
    if authority is None:
        return None if kind in _FAIL_OPEN_KINDS else "role authority unreadable (config/flows.yml) — failing closed"
    if kind not in authority["action_types"]:
        return f"'{kind}' is not in this role's authority.action_types {sorted(authority['action_types'])}"
    if kind in ("create_issue", "close_issue") and authority["repos"]:
        scope = "hub" if repo == _hq() else "project"
        if scope not in authority["repos"]:
            return f"'{kind}' on a {scope} repo is outside authority.repos {sorted(authority['repos'])}"
    return None


# Issues no hub loop may close, whatever the model says: escalations are the
# approver's, and the budget/capacity notices are the only signal those
# breakers are open.
PROTECTED_CLOSE_LABELS = {"escalation"}
PROTECTED_CLOSE_TITLES = ("BUDGET CAP HIT", "Claude capacity window exhausted")


def _close_block_reason(repo, number):
    code, out, _ = _run(["gh", "issue", "view", str(number), "-R", repo, "--json", "labels,title"])
    if code != 0:
        return "could not read the issue's labels/title — refusing to close blind"
    try:
        it = json.loads(out)
    except ValueError:
        return "could not parse the issue's labels/title — refusing to close blind"
    names = {l.get("name") for l in it.get("labels", []) if isinstance(l, dict)}
    if names & PROTECTED_CLOSE_LABELS:
        return f"it carries {sorted(names & PROTECTED_CLOSE_LABELS)} — escalations are closed by an approver"
    title = it.get("title") or ""
    if any(title.startswith(t) for t in PROTECTED_CLOSE_TITLES):
        return "it is a budget/capacity breaker notice — only its owner closes it"
    return None

def _resolve(a, keys):
    # Models drift on field names (label vs labels, issue vs number, comment vs
    # body) — try every known synonym instead of a bare a["key"] that raises.
    for k in keys:
        if k in a and a[k] not in (None, ""):
            return a[k]
    return None

def _new_rec(kind, repo, ident):
    return {"kind": kind, "repo": repo, "target": ident, "number": None,
            "result": "", "success": False, "cmd": None, "exit_code": None,
            "stdout": "", "stderr": "", "classification": None}

def _label_owners():
    """config/flows.yml labels.*.applied_by, via squadhq.label_guard — the
    same decision the gh wrapper enforces in agent jobs. Fails open to None (no
    guard) — a broken flows.yml must not block the cycle, same as ctx["flows"]."""
    try:
        return label_guard.load_owners()
    except Exception as e:
        print(f"  label guard unavailable: {e!r}", flush=True)
        return None


def _own_comment_within(repo, number, badge, hours):
    """True if this role already commented on the item within `hours`."""
    code, out, _ = _run(["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments",
                         "--jq", ".[] | [.created_at, .user.login, (.body[0:40])] | @tsv"])
    if code != 0:
        return False
    cutoff = _utc_now() - datetime.timedelta(hours=hours)
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not untrusted.is_bot_login(parts[1]):
            continue  # only OUR comments count toward the cooldown
        at, head = parts[0], parts[2]
        try:
            when = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if head.startswith(badge.strip()) and when >= cutoff:
            return True
    return False


def _cos_comment_block_reason(repo, number, body):
    """Comment-only CoS actions that are wrong by construction:
    - on a needs-human item: an approver owns it now; CoS narration is noise.
    - asking for "Why this matters" on an issue whose body HAS the section
      (the model misjudged it repeatedly; the fact is checked here instead)."""
    # REST, not `gh issue view`: it carries user + author_association, which
    # decide whether the body's "Why this matters" counts.
    code, out, _ = _run(["gh", "api", f"repos/{repo}/issues/{number}"])
    if code != 0:
        return None
    try:
        it = json.loads(out)
    except ValueError:
        return None
    if "needs-human" in {l["name"] for l in it.get("labels", [])}:
        return "item is needs-human (approver-owned) — CoS stays silent"
    if WHY_RE.search(body or "") and has_why_this_matters(it.get("body")) and _body_trusted(it):
        return "asks for 'Why this matters' but the body already has it"
    return None


def _comment_only_cooldown_hours():
    try:
        return flow_reader.get_flood_guard("cos_comment_only_cooldown_hours")["value"]
    except Exception:
        return 24


# Labels a role must never REMOVE. triaged:ready is the dispatch queue; the
# stuck sweeper removes it when it dispatches — a model removing it drops the
# ticket out of the queue silently.
QUEUE_KEEPERS = {"chief-of-staff": {"triaged:ready"}}


def execute(actions, dry_run=False, system=False):
    """Run model-produced actions (capped, validated, authority-gated).
    system=True marks actions this module wrote itself (escalations): they
    skip the role-authority and narration checks, never the repo allowlist."""
    batch = [a for a in actions if isinstance(a, dict)][:20]  # hard cap per run
    owners = _label_owners()
    role = MODE_ROLE.get(MODE, MODE)
    authority = None if dry_run or system else _role_authority(role)
    # NARRATION GUARD (CoS, 2026-09-26): a comment with no label change or
    # close on the same item in this batch is status narration ("holding",
    # "not touching", restated labels). One per item per cooldown window at
    # most — the rest are dropped here, whatever the prompt produced.
    acted = {(_repo_of(a), str(_as_number(_resolve(a, ["number", "issue", "issue_number"]))))
             for a in batch
             if (a.get("type") or a.get("action") or a.get("op")) in ("add_labels", "remove_labels", "close_issue")}
    total = len(batch)
    done = []
    records = []  # structured per-action results, for the step-summary table
    ok = fail = 0
    for i, a in enumerate(batch, 1):
        # Models drift on the key name for the action kind ("type" vs "action" vs
        # "op") — resolve tolerantly instead of silently dropping the action.
        kind = a.get("type") or a.get("action") or a.get("op")
        repo = _repo_of(a)
        ident = a.get("title", a.get("number", ""))
        print(f"ACTION {i}: {kind} repo={repo} title/number={ident}", flush=True)
        rec = _new_rec(kind, repo, ident)
        if dry_run:
            # --dry-run: the model was called for real, but nothing below this
            # point touches GitHub. No gh call, no field validation beyond
            # what's already printed above.
            rec["result"] = f"[dry-run] would {kind}: {json.dumps(a, default=str)[:300]}"
            rec["success"] = True
            rec["classification"] = "dry-run"
            done.append(rec["result"])
            records.append(rec)
            ok += 1
            continue
        if repo not in ALLOWED_REPOS | {_hq()}:
            rec["result"] = f"SKIPPED: repo {repo!r} not allowed"
            rec["classification"] = "invalid-repo"
            done.append(rec["result"])
            records.append(rec)
            fail += 1
            continue
        if not kind or kind not in ALLOWED:
            rec["result"] = f"SKIPPED: unrecognized kind {kind!r} keys={sorted(a.keys())}"
            rec["classification"] = "unrecognized-kind"
            done.append(rec["result"])
            records.append(rec)
            fail += 1
            continue
        # what this role may do at all comes from flows.yml, not
        # from what the model asked for.
        why = None if system else _authority_block_reason(kind, repo, authority)
        if why:
            rec["result"] = f"SKIPPED {kind}: {why}"
            rec["classification"] = "forbidden-action"
            done.append(rec["result"])
            records.append(rec)
            fail += 1
            continue

        # Tolerant field resolution for the required fields of each kind. An
        # unresolvable field fails only this action, with full diagnostics — it
        # never raises and never drops silently.
        number = labels = body = None
        if kind in ("comment", "add_labels", "remove_labels", "close_issue"):
            number = _resolve(a, ["number", "issue", "issue_number"])
            if number is None:
                rec["result"] = (f"FAILED {kind}: unresolvable field (number/issue/"
                                  f"issue_number) keys={sorted(a.keys())} "
                                  f"action={json.dumps(a, default=str)[:300]!r}")
                rec["classification"] = "unresolvable-fields"
                done.append(rec["result"])
                records.append(rec)
                fail += 1
                continue
            parsed = _as_number(number)
            if parsed is None:
                rec["result"] = f"FAILED {kind}: issue number {number!r} is not a positive integer"
                rec["classification"] = "invalid-number"
                done.append(rec["result"])
                records.append(rec)
                fail += 1
                continue
            number = parsed
            rec["target"] = number
        if kind == "close_issue":
            why = _close_block_reason(repo, number)
            if why:
                rec["result"] = f"SKIPPED close_issue on {repo}#{number}: {why}"
                rec["classification"] = "protected-issue"
                done.append(rec["result"])
                records.append(rec)
                fail += 1
                continue
        if kind in ("add_labels", "remove_labels"):
            labels = github.normalize_labels(_resolve(a, ["labels", "label", "names"]))
            if kind == "add_labels" and owners is not None and labels:
                blocked = label_guard.blocked_labels(role, labels, owners)
                labels = [l for l in labels if l not in blocked]
                if blocked:
                    print(f"  label guard: {role} may not add {blocked} (flows.yml labels.*.applied_by)", flush=True)
                if blocked and not labels:
                    rec["result"] = f"SKIPPED add_labels: {blocked} are not this role's to apply"
                    rec["classification"] = "forbidden-label"
                    done.append(rec["result"])
                    records.append(rec)
                    fail += 1
                    continue
            if kind == "remove_labels" and labels and role in QUEUE_KEEPERS:
                kept = [l for l in labels if l in QUEUE_KEEPERS[role]]
                labels = [l for l in labels if l not in kept]
                if kept:
                    print(f"  queue guard: {role} may not remove {kept} — the stuck sweeper does on dispatch", flush=True)
                if kept and not labels:
                    rec["result"] = f"SKIPPED remove_labels: {kept} is the dispatch queue, removed only on dispatch"
                    rec["classification"] = "forbidden-label"
                    done.append(rec["result"])
                    records.append(rec)
                    fail += 1
                    continue
            if not labels:
                rec["result"] = (f"FAILED {kind}: unresolvable field (labels/label/"
                                  f"names) keys={sorted(a.keys())} "
                                  f"action={json.dumps(a, default=str)[:300]!r}")
                rec["classification"] = "unresolvable-fields"
                done.append(rec["result"])
                records.append(rec)
                fail += 1
                continue
        if kind == "comment":
            body = _resolve(a, ["body", "comment"])
            if body is None:
                rec["result"] = (f"FAILED comment: unresolvable field (body/comment) "
                                  f"keys={sorted(a.keys())} "
                                  f"action={json.dumps(a, default=str)[:300]!r}")
                rec["classification"] = "unresolvable-fields"
                done.append(rec["result"])
                records.append(rec)
                fail += 1
                continue
            why = _cos_comment_block_reason(repo, number, body) if MODE == "cos" and repo != _hq() \
                and not system and (repo, str(number)) not in acted else None
            if why:
                rec["result"] = f"SKIPPED comment on {repo}#{number}: {why}"
                rec["classification"] = "narration-suppressed"
                rec["success"] = True
                done.append(rec["result"])
                records.append(rec)
                ok += 1
                continue
            if MODE == "cos" and not system and (repo, str(number)) not in acted and repo != _hq() \
                    and _own_comment_within(repo, number, MODE_BADGE.get(MODE, ""), _comment_only_cooldown_hours()):
                rec["result"] = f"SKIPPED comment on {repo}#{number}: comment-only, already commented within cooldown"
                rec["classification"] = "narration-suppressed"
                rec["success"] = True
                done.append(rec["result"])
                records.append(rec)
                ok += 1
                continue

        try:
            if kind == "create_issue":
                title = a.get("title")
                if not isinstance(title, str) or not title.strip():
                    rec["result"] = f"FAILED create_issue: missing title keys={sorted(a.keys())}"
                    rec["classification"] = "unresolvable-fields"
                    done.append(rec["result"])
                    records.append(rec)
                    fail += 1
                    continue
                title = title[:200]
                req = github.normalize_labels(a.get("labels"))
                drop = label_guard.blocked_labels(role, req, owners) if owners is not None else []
                if drop:
                    print(f"  label guard: {role} may not add {drop} (flows.yml labels.*.applied_by)", flush=True)
                label_str = ",".join(l for l in req if l not in drop)
                cmd = ["gh", "issue", "create", "-R", repo, "-t", title,
                       "-b", str(a.get("body") or "")[:6000]]
                if label_str:
                    cmd += ["-l", label_str]
                code, stdout, stderr = _run(cmd)
                labels_missing = False
                if code != 0 and label_str and "not found" in stderr.lower():
                    # A missing label must never block the report — retry unlabeled.
                    labels_missing = True
                    cmd = ["gh", "issue", "create", "-R", repo, "-t", title,
                           "-b", str(a.get("body") or "")[:6000]]
                    code, stdout, stderr = _run(cmd)
                rec["cmd"] = " ".join(cmd)
                rec["exit_code"] = code
                rec["stdout"] = stdout[:300]
                rec["stderr"] = stderr[:300]

                if code != 0:
                    rec["result"] = (f"FAILED create_issue: exit {code} "
                                      f"stdout={stdout[:300]!r} stderr={stderr[:300]!r}")
                    rec["classification"] = "nonzero-exit"
                    done.append(rec["result"])
                    fail += 1
                    records.append(rec)
                    continue

                # gh prints the created issue's URL to stdout (stderr in some
                # versions) — a URL match IS the ground truth; trust it over a
                # title search, which can miss a just-created issue due to
                # GitHub's search-index lag (the exact false-negative this fixes).
                url_match = URL_RE.search(stdout) or URL_RE.search(stderr)
                if url_match:
                    n = int(url_match.group(1))
                    url = url_match.group(0)
                    rec["number"] = n
                    if labels_missing:
                        rec["result"] = (f"created #{n} {url} "
                                          f"(labels missing: {label_str} — create them in {repo})")
                    else:
                        rec["result"] = f"created #{n} {url}"
                    rec["success"] = True
                    rec["classification"] = "url-match"
                    done.append(rec["result"])
                    ok += 1
                else:
                    # No URL in either stream (rare/older gh) — fall back to a
                    # title search. A search miss is NOT proof of failure (index
                    # lag), so it does not count against the action or trigger
                    # an escalation.
                    verify = gh_json(["issue", "list", "-R", repo, "--search", title,
                                       "--json", "number", "--limit", "1"])
                    if verify:
                        n = verify[0]["number"]
                        rec["number"] = n
                        rec["result"] = f"created #{n} (verified via search)"
                        rec["success"] = True
                        rec["classification"] = "verify-match"
                        done.append(rec["result"])
                        ok += 1
                    else:
                        rec["result"] = "created (unverified — search lag)"
                        rec["success"] = True
                        rec["classification"] = "verify-miss"
                        done.append(rec["result"])
                        ok += 1
                records.append(rec)
                continue

            elif kind == "comment":
                cmd = ["gh", "issue", "comment", str(number), "-R", repo, "-b", str(body)[:6000]]
                code, stdout, stderr = _run(cmd)
                rec["cmd"] = " ".join(cmd)
                rec["exit_code"] = code
                rec["stdout"] = stdout[:300]
                rec["stderr"] = stderr[:300]
                rec["number"] = number
                if code != 0:
                    rec["result"] = f"FAILED comment: exit {code} stderr={stderr[:300]!r}"
                    rec["classification"] = "nonzero-exit"
                    fail += 1
                else:
                    rec["result"] = f"commented on {repo}#{number}"
                    rec["success"] = True
                    ok += 1
                done.append(rec["result"])

            elif kind == "add_labels":
                current = _current_labels(repo, number)
                if current is not None and set(labels) <= current:
                    rec["number"] = number
                    rec["result"] = f"skipped add_labels: {labels} already present on {repo}#{number}"
                    rec["success"] = True
                    rec["classification"] = "idempotent-skip"
                    ok += 1
                    done.append(rec["result"])
                    records.append(rec)
                    continue
                cmd = ["gh", "issue", "edit", str(number), "-R", repo,
                       "--add-label", ",".join(labels)]
                code, stdout, stderr = _run(cmd)
                rec["cmd"] = " ".join(cmd)
                rec["exit_code"] = code
                rec["stdout"] = stdout[:300]
                rec["stderr"] = stderr[:300]
                rec["number"] = number
                if code != 0:
                    rec["result"] = f"FAILED add_labels: exit {code} stderr={stderr[:300]!r}"
                    rec["classification"] = "nonzero-exit"
                    fail += 1
                else:
                    rec["result"] = f"labeled {repo}#{number} +{labels}"
                    rec["success"] = True
                    ok += 1
                    # CoS-driven status labels move the board column too. Never let a
                    # board-sync failure break the action loop — it's a nice-to-have.
                    if repo != _hq():
                        for lbl in labels:
                            state = BOARD_STATUS_LABELS.get(lbl)
                            if state:
                                try:
                                    board_sync.sync(repo, number, state)
                                except Exception as e:
                                    board_sync_note(repo, number, state, e)
                                break
                done.append(rec["result"])

            elif kind == "remove_labels":
                current = _current_labels(repo, number)
                if current is not None and not (set(labels) & current):
                    rec["number"] = number
                    rec["result"] = f"skipped remove_labels: {labels} already absent on {repo}#{number}"
                    rec["success"] = True
                    rec["classification"] = "idempotent-skip"
                    ok += 1
                    done.append(rec["result"])
                    records.append(rec)
                    continue
                cmd = ["gh", "issue", "edit", str(number), "-R", repo,
                       "--remove-label", ",".join(labels)]
                code, stdout, stderr = _run(cmd)
                rec["cmd"] = " ".join(cmd)
                rec["exit_code"] = code
                rec["stdout"] = stdout[:300]
                rec["stderr"] = stderr[:300]
                rec["number"] = number
                if code != 0:
                    rec["result"] = f"FAILED remove_labels: exit {code} stderr={stderr[:300]!r}"
                    rec["classification"] = "nonzero-exit"
                    fail += 1
                else:
                    rec["result"] = f"unlabeled {repo}#{number}"
                    rec["success"] = True
                    ok += 1
                done.append(rec["result"])

            elif kind == "close_issue":
                close_body = _resolve(a, ["body", "comment"]) or "Closed by Chief of Staff."
                cmd = ["gh", "issue", "close", str(number), "-R", repo, "-c", close_body]
                code, stdout, stderr = _run(cmd)
                rec["cmd"] = " ".join(cmd)
                rec["exit_code"] = code
                rec["stdout"] = stdout[:300]
                rec["stderr"] = stderr[:300]
                rec["number"] = number
                if code != 0:
                    rec["result"] = f"FAILED close_issue: exit {code} stderr={stderr[:300]!r}"
                    rec["classification"] = "nonzero-exit"
                    fail += 1
                else:
                    rec["result"] = f"closed {repo}#{number}"
                    rec["success"] = True
                    ok += 1
                done.append(rec["result"])
        except Exception as e:
            # repr(), never str() — an exception with an empty message must still
            # show its class so a silent failure never reads as a blank log line.
            rec["result"] = f"FAILED {kind}: {repr(e)}"
            rec["classification"] = "exception"
            done.append(rec["result"])
            fail += 1
        records.append(rec)
    print("\n".join(done))
    print(f"executed {ok} ok / {fail} failed of {total} actions")
    return done, ok, fail, records

def _log_diagnostics(mode, reply_obj, parsed_ok, num_actions):
    # Self-diagnosing forensics for an empty or unparseable model reply (the
    # #46 incident: a valid 200 response, tokens billed, zero output text) —
    # printed to the job log in FULL (never truncated, unlike the old
    # "RAW REPLY (truncated): ..." line this replaces) and mirrored to the
    # step summary (this repo's CI-artifact convention) so it survives after
    # the run without needing to dig through raw Action logs.
    parse_errors = None
    if not reply_obj.text.strip():
        reason = "empty reply — 0 output text in a successful API response"
    elif not parsed_ok:
        reason = "reply had content but no parseable JSON action list found in it"
        # WHY, not just THAT — the exact JSONDecodeError (message + position)
        # per extraction attempt, so a malformed reply (unescaped quote,
        # stray comma, literal control character the model forgot to
        # escape...) is diagnosable directly instead of re-guessed from a
        # truncated excerpt every time this recurs.
        parse_errors = _diagnose_parse_failure(reply_obj.text)
    else:
        reason = f"{num_actions} action(s) parsed"
    print(
        f"[{mode}] diagnostics ({reason}):\n"
        f"  model: {reply_obj.model}\n"
        f"  stop_reason: {reply_obj.stop_reason!r}\n"
        f"  usage: {reply_obj.input_tokens} in / {reply_obj.output_tokens} out tokens\n"
        + (f"  parse errors per attempt:\n{parse_errors}\n" if parse_errors else "")
        + f"  raw reply ({len(reply_obj.text)} chars): {redact(reply_obj.text)!r}",
        flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"\n### {mode} — diagnostics ({reason})\n\n")
            f.write(f"- Model: `{reply_obj.model}`\n")
            f.write(f"- stop_reason: `{reply_obj.stop_reason}`\n")
            f.write(f"- Usage: {reply_obj.input_tokens} in / {reply_obj.output_tokens} out tokens\n")
            if parse_errors:
                f.write(f"- Parse errors per attempt:\n\n```\n{parse_errors}\n```\n")
            # 20k-char sanity ceiling on the SUMMARY embed only (GitHub step
            # summaries have their own ~1MB cap) — the job-log print above has
            # no cap at all, so nothing is ever truly lost, only the summary
            # copy is capped for a pathological case.
            f.write(f"- Raw reply ({len(reply_obj.text)} chars):\n\n```\n{redact(reply_obj.text[:20000])}\n```\n")

def _empty_reply_escalation(mode, reply_obj):
    # Builds the title+body for a genuinely empty (0 output text) model
    # reply. stop_reason == "max_tokens" gets its own title: this is the
    # CONFIRMED mechanism behind incident #46 (real run diagnostics,
    # 2026-08-27 — model=claude-sonnet-5, output_tokens==max_tokens, zero
    # text) — claude-sonnet-5's adaptive thinking (on by default) consumed
    # the entire budget before emitting visible text. The thinking:disabled
    # + higher max_tokens fix in call_claude should prevent recurrence for
    # that model; this distinct title exists so a recurrence — on this or
    # another model, if adaptive thinking defaults change again — is
    # triaged as "budget exhausted" and not confused with a genuinely-empty
    # end_turn reply, which has a different, unknown cause and a different
    # fix. Split out from main() purely for direct testability.
    if reply_obj.stop_reason == "max_tokens":
        title = f"ESCALATION: {mode} run — hit max_tokens before producing output"
        mechanism = (
            f"Output hit the max_tokens cap ({reply_obj.output_tokens} tokens billed) "
            f"with zero visible text — the model spent the entire budget on something "
            f"other than the answer (confirmed cause, #46: adaptive thinking, on by "
            f"default, counts against max_tokens — see THINKING_OFF in "
            f"agent_runner.py).\n\n"
            f"If `{reply_obj.model}` is in THINKING_OFF and this recurred anyway, "
            f"the guard didn't help — try raising max_tokens further. If it is "
            f"NOT in THINKING_OFF, add it with the thinking value its docs accept "
            f"(\"disabled\" is a 400 on some models).")
    else:
        title = f"ESCALATION: {mode} run — model replied with empty output"
        mechanism = (
            f"0 output text, valid HTTP response, stop_reason `{reply_obj.stop_reason}` "
            f"(not max_tokens — the known thinking-budget mechanism doesn't explain "
            f"this one). Cause unconfirmed; check the full raw reply in this run's "
            f"job log/step summary.")
    body = (
        f"{MODE_BADGE.get(mode, '[Runner]')}\n"
        f"Mode '{mode}' returned a completely empty reply from the model "
        f"(tokens were billed).\n\n"
        f"- Model: `{reply_obj.model}`\n"
        f"- stop_reason: `{reply_obj.stop_reason}`\n"
        f"- Usage: {reply_obj.input_tokens} in / {reply_obj.output_tokens} out tokens\n\n"
        f"{mechanism}\n\n"
        f"Full diagnostics (raw reply, full usage) are in this run's job log "
        f"and step summary.")
    return title, body

def find_recent_open_issue(repo, title, hours=None):
    """Dedup guard: ANY open issue with exactly this title (not only one from
    the last 24h — a recurring escalation must keep landing on the issue a
    human has not dealt with yet). `hours` is accepted for compatibility and
    ignored."""
    try:
        return github.client().find_issue_by_title(repo, title, state="open")  # bot-authored only
    except Exception:
        return None


def create_or_comment(repo, title, body, labels):
    # Before opening a NEW escalation/fallback issue, check for an existing
    # open one with the identical title and comment on it instead of creating
    # a duplicate. These actions are authored by this code, not the model, so
    # they are not subject to the role's action authority.
    existing = find_recent_open_issue(repo, title)
    if existing:
        action = {"type": "comment", "repo": repo, "number": existing,
                  "body": f"Recurred — see below.\n\n{body}"[:6000]}
    else:
        action = {"type": "create_issue", "repo": repo, "title": title,
                  "body": body, "labels": labels}
    return execute([action], system=True)

def _extraction_attempts(text):
    # Shared by parse_actions() and _diagnose_parse_failure() so diagnosis
    # always tries exactly what parsing tried — no drift between the two.
    # Model must return a JSON array; try progressively looser extraction
    # since models sometimes wrap it in prose despite instructions.
    text = text.strip()

    candidate = text
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1]
        if candidate.startswith("json"):
            candidate = candidate[4:]
    attempts = [("fenced-block-split", candidate)]

    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if m:
        attempts.append(("fenced-block-regex", m.group(1).strip()))

    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        attempts.append(("bracket-slice", text[start:end + 1]))

    return attempts

def parse_actions(text):
    # Returns (actions, parsed_ok) — parsed_ok distinguishes a deliberate
    # empty "[]" reply (no-op) from a reply we couldn't find any JSON list
    # in at all. See _diagnose_parse_failure for WHY a given attempt failed
    # (the JSONDecodeError itself, not just "didn't work") when this returns
    # ([], False) on a non-empty reply.
    #
    # strict=False (not the json module default): confirmed root cause of a
    # real recurrence (2026-08-27, brief run) — a long "body" field (markdown
    # embedded inside the model's own JSON) can contain a literal, unescaped
    # newline/control character instead of the model correctly writing "\n".
    # Strict JSON rejects that ("Invalid control character") even though the
    # intent is completely unambiguous; strict=False accepts raw control
    # characters inside string values, which is exactly what an LLM
    # generating long embedded text needs. This is a standard, narrow
    # relaxation — it does NOT tolerate trailing commas, comments, or any
    # other genuine syntax error; only this one specific, common LLM slip.
    for _, attempt in _extraction_attempts(text):
        try:
            data = json.loads(attempt, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data, True

    return [], False

def _diagnose_parse_failure(text):
    # Re-runs the SAME extraction attempts as parse_actions (same strict=False)
    # but captures WHY each one failed instead of silently moving to the
    # next — the exact JSONDecodeError (message + line/col/char position)
    # plus a snippet of the text around that position, so a malformed reply
    # is diagnosable (e.g. "the model forgot to escape a quote at char 2103")
    # instead of just "no parseable actions found in a 3719-char reply".
    lines = []
    for name, attempt in _extraction_attempts(text):
        try:
            data = json.loads(attempt, strict=False)
            lines.append(f"  {name}: parsed as valid JSON but wasn't a list "
                         f"(top-level type: {type(data).__name__})")
        except json.JSONDecodeError as e:
            snippet_start = max(0, e.pos - 40)
            snippet = attempt[snippet_start:e.pos + 40]
            lines.append(f"  {name}: {e.msg} at line {e.lineno} col {e.colno} "
                         f"(char {e.pos}/{len(attempt)}) — near: {snippet!r}")
        except Exception as e:
            lines.append(f"  {name}: {e!r}")
    return "\n".join(lines) if lines else "  (no extraction attempt was even applicable — "\
                                           "text has neither a ``` fence nor a [ ... ] span)"

MODES = {
    "cos":     ("agents/chief-of-staff/prompt.md", False),
    "brief":   ("agents/daily-brief/prompt.md",    False),
    "growth":  ("agents/growth/prompt.md",         True),
    "seo":     ("agents/seo/prompt.md",            True),
    "legal":   ("agents/legal/prompt.md",          True),
    "finance": ("agents/finance/prompt.md",        False),
    "rollup":  ("agents/rollup/prompt.md",         False),
}

# Natural label per mode, for the fallback-delivery issue. cos has none.
MODE_LABEL = {
    "brief": "daily-brief", "growth": "dept:growth", "seo": "dept:growth",
    "legal": "dept:legal", "finance": "dept:finance", "rollup": "weekly-rollup",
}

# Role badge per mode (org/CLAUDE-org.md IDENTITY rule) — prepended to any
# comment/issue body THIS SCRIPT constructs itself (escalations, fallback
# delivery, budget-cap notices). Model-authored bodies (the normal case —
# CoS/Brief/Growth/etc. write their own "body" field per their prompt) carry
# their own badge per that mode's agents/*/prompt.md IDENTITY instruction;
# this dict only covers the scripted paths where agent_runner.py itself
# writes the text, not the model.
MODE_BADGE = {
    "cos": "[Chief of Staff]", "brief": "[Daily Brief]", "growth": "[Growth]",
    "seo": "[SEO]", "legal": "[Legal]", "finance": "[Finance]", "rollup": "[Weekly Rollup]",
}

# Mode -> config/flows.yml roles.* key, for flow_reader.context_slice_for_role()
# (injected into gather()'s ctx as ctx["flows"] — see that function's docstring
# for why these agents need the data injected rather than a "see
# config/flows.yml" pointer in their prompt text).
MODE_ROLE = {
    "cos": "chief-of-staff", "brief": "daily-brief", "growth": "growth",
    "seo": "seo", "legal": "legal", "finance": "finance", "rollup": "weekly-rollup",
}

OUTCOME_BADGE = {
    "actions-ok": "✅ actions-ok",
    "fallback-delivered": "📄 fallback-delivered",
    "escalation-created": "🚨 escalation-created",
    "no-op": "⚪ no-op (empty [] reply)",
    "dry-run": "🧪 dry-run (nothing executed)",
    "no-inputs": "⚪ no-inputs (model not called)",
}

def _evidence_table(records):
    # Self-diagnosing forensics for a failed action: kind, target, the exact
    # command run, exit code, stdout/stderr (truncated), and a classification —
    # so an escalation is never just "all actions failed" with no way to tell why.
    failed = [rec for rec in records if not rec["success"]]
    if not failed:
        return ""
    lines = ["| kind | repo | target | classification | exit | cmd | stdout | stderr |",
             "|---|---|---|---|---|---|---|---|"]
    for rec in failed:
        target = f"#{rec['number']}" if rec["number"] is not None else str(rec["target"] or "")
        lines.append(
            f"| {rec['kind']} | {rec['repo']} | {target} | {rec.get('classification') or ''} | "
            f"{rec.get('exit_code') if rec.get('exit_code') is not None else ''} | "
            f"`{redact((rec.get('cmd') or '')[:300])}` | `{redact(rec.get('stdout') or '')}` | "
            f"`{redact(rec.get('stderr') or '')}` |")
    return "\n".join(lines)

def write_summary(mode, usage_delta, actions, records, outcome):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return  # local run — nothing to write to
    ts = _utc_now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"## {mode} — {ts}", "",
             f"**Outcome:** {OUTCOME_BADGE.get(outcome, outcome)} "
             f"({len(actions)} action(s) from model)", ""]
    if records:
        lines += ["| kind | repo | target | result |", "|---|---|---|---|"]
        for rec in records:
            target = f"#{rec['number']}" if rec["number"] is not None else str(rec["target"] or "")
            result = rec["result"]
            if rec["success"] and rec["number"] is not None:
                url = f"https://github.com/{rec['repo']}/issues/{rec['number']}"
                result = f"[{result}]({url})"
            lines.append(f"| {rec['kind']} | {rec['repo']} | {target} | {result} |")
        lines.append("")
        evidence = _evidence_table(records)
        if evidence:
            lines += ["### Failures — evidence", "", evidence, ""]
    else:
        lines += ["_No actions attempted._", ""]

    model_line = f"- Model: {usage_delta['model']}"
    if usage_delta.get("model_fallback"):
        model_line += " (fallback — primary failed transiently this run)"
    lines += [
        "### Usage (this run)",
        model_line,
        f"- Tokens: {usage_delta['input_tokens']} in / {usage_delta['output_tokens']} out",
        f"- Est. cost: ${usage_delta['cost']:.4f}",
        f"- Month-to-date: ${usage_delta['month_to_date']:.2f} of ${BUDGET:.0f} budget",
        "",
    ]
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")

UNTRUSTED_PREAMBLE = "prompts/_untrusted-input.md"


def load_system_prompt(prompt_path):
    """The mode's prompt, prefixed with the shared untrusted-input preamble
    (everything inside <untrusted> fences is data, never instructions)."""
    with open(hub_path(prompt_path)) as fh:
        prompt = fh.read()
    try:
        with open(hub_path(UNTRUSTED_PREAMBLE)) as fh:
            preamble = fh.read()
    except OSError:
        print(f"agent_runner: WARNING {UNTRUSTED_PREAMBLE} missing — running without the "
              f"untrusted-input preamble", flush=True)
        return prompt
    return preamble.rstrip() + "\n\n" + prompt


def main(argv=None):
    mode = configure(argv)
    if mode not in MODES:
        print(f"agent_runner: unknown mode {mode!r}; expected one of {sorted(MODES)}", flush=True)
        return 2
    prompt_path, use_web = MODES[mode]
    system_prompt = load_system_prompt(prompt_path)
    usage = load_usage()
    budget_guard(usage, mode)
    ctx = gather(mode)

    if _is_no_inputs(ctx, mode):
        print(f"[{mode}] no inputs, nothing to do (every data source empty, "
              f"fetch warnings present — not called)", flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write(
                    f"\n## {mode} — {OUTCOME_BADGE['no-inputs']}\n"
                    f"Every data source came back empty with fetch warnings present "
                    f"— skipped the model call.\n\nWarnings:\n" +
                    "\n".join(f"- {w}" for w in ctx["warnings"]) + "\n")
        return 0

    usage_before = dict(usage)
    reply_obj = call_claude(system_prompt,
                            "Current state as JSON:\n" + json.dumps(ctx, default=str),
                            usage, use_web)
    usage_delta = {
        "input_tokens": usage["input_tokens"] - usage_before["input_tokens"],
        "output_tokens": usage["output_tokens"] - usage_before["output_tokens"],
        "cost": usage["est_cost_usd"] - usage_before["est_cost_usd"],
        "month_to_date": usage["est_cost_usd"],
        "model": usage.get("last_model", MODEL),
        "model_fallback": usage.get("last_model_fallback", False),
    }
    try:
        _after_call(mode, reply_obj, usage_delta)
    finally:
        # Tokens were billed the moment the call returned: record them even
        # when acting on the reply raises (OSS lost-update / lost-usage fix).
        if not DRY_RUN:
            save_usage(usage, usage_before)
    return 0


def _after_call(mode, reply_obj, usage_delta):
    reply = reply_obj.text
    actions, parsed_ok = parse_actions(reply)
    if not reply.strip() or not parsed_ok:
        _log_diagnostics(mode, reply_obj, parsed_ok, len(actions))
    results, ok, fail, records = execute(actions, dry_run=DRY_RUN)

    if DRY_RUN:
        if not parsed_ok:
            note = ("would fall back to an unstructured delivery issue (non-empty, unparseable)"
                     if reply.strip() else "would escalate (empty reply)")
        elif not actions:
            note = "no-op (empty [] reply)"
        else:
            note = f"{len(actions)} action(s) would execute"
        print(f"[dry-run] {mode}: {note} — model was called for real (real tokens/cost); "
              f"nothing written to GitHub, usage NOT committed", flush=True)
        write_summary(mode, usage_delta, actions, records, "dry-run")
        print(f"[{mode}] {len(actions)} actions -> (dry-run) " + "; ".join(results[:20]))
        return

    # A completed run must never end with zero artifacts: exactly one of
    # actions-ok / fallback-delivered / escalation-created / no-op, always.
    if not parsed_ok:
        if reply.strip():
            # Non-empty reply, but no parseable JSON action list in it — the
            # model drifted into prose despite instructions. Still deliver
            # what it actually said (better than nothing) rather than only
            # escalating, but the title makes clear this was a parse
            # failure, not a normal report — distinct from the "model
            # replied with nothing" escalation below (a different failure
            # mode: a valid response with zero output text).
            label = MODE_LABEL.get(mode)
            title = f"{mode} report — {_utc_today()} (PARSE FAILURE — unstructured fallback)"
            preface = (
                f"{MODE_BADGE.get(mode, '[Runner]')}\n"
                f"> ⚠ Delivered as-is: the model's reply had content but wasn't valid, "
                f"parseable JSON — every extraction attempt's error is below. Everything "
                f"after the horizontal rule is the model's raw text, unmodified (it may "
                f"carry its own badge too — that's expected, not a duplicate mistake).\n\n"
                f"<details><summary>Parse errors per attempt</summary>\n\n```\n"
                f"{_diagnose_parse_failure(reply)}\n```\n</details>\n\n---\n\n")
            results, ok, fail, records = create_or_comment(
                _hq(), title, preface + redact(reply), [label] if label else [])
            outcome = "fallback-delivered"
            print("FALLBACK-DELIVERED")
        else:
            # Empty reply: a normal, billable, HTTP-200 response with zero
            # output text — NOT an API failure (those raise AnthropicAPIError
            # above and never reach here) and NOT "nothing to report" (the
            # no-inputs check above already ruled that out before the model
            # was even called). Full diagnostics were already logged by
            # _log_diagnostics above; this issue carries a summary of them.
            title, body = _empty_reply_escalation(mode, reply_obj)
            _, _, _, records = create_or_comment(_hq(), title, body, ["escalation"])
            outcome = "escalation-created"
            print("ESCALATION-CREATED")
    elif not actions:
        outcome = "no-op"
        print("NO-OP")
    elif ok == 0:
        # Never emit "all actions failed" without the per-action evidence table —
        # this is what let a real create_issue success (URL on stdout, search-verify
        # miss) get reported as a spurious failure.
        body = (f"{MODE_BADGE.get(mode, '[Runner]')}\n"
                f"All {fail} action(s) failed for mode '{mode}':\n\n" +
                _evidence_table(records))
        title = f"ESCALATION: {mode} run — all actions failed"
        _, _, _, esc_records = create_or_comment(_hq(), title, body[:6000], ["escalation"])
        records = records + esc_records
        outcome = "escalation-created"
        print("ESCALATION-CREATED")
    else:
        outcome = "actions-ok"
        print("ACTIONS-OK")

    write_summary(mode, usage_delta, actions, records, outcome)
    print(f"[{mode}] {len(actions)} actions -> " + "; ".join(results[:20]))

if __name__ == "__main__":
    sys.exit(main())
