#!/usr/bin/env python3
"""
Auth & capacity resolver/breaker for claude-code-action jobs.

Two subcommands, run as separate steps around the claude-code-action step:

  resolve      Before the agent step. Reads config/auth.json + state/capacity.json
               from the HUB repo (always via the Contents API, never the local
               checkout, so the decision reflects main regardless of what ref
               this job happened to check out). Writes `auth_mode` to
               $GITHUB_OUTPUT: one of "api", "subscription", "skip". Handles
               probe recovery (clears state/capacity.json when paused_until has
               passed) and posts a dedup "Paused" comment for label-triggered
               work when skipping.

  check-trip   After the agent step, `if: always()`. No-op unless the resolved
               auth_mode was "subscription" (api-mode runs can't hit a
               subscription session/weekly/Opus limit, and rate/overload errors
               are transient and already auto-retried by Claude Code itself —
               not our concern). On a real trip: writes state/capacity.json
               (paused_until = now+60m) and creates/refreshes the hub escalation
               issue. Re-arms identically if a post-probe canary trips again.

Fail-open throughout, with ONE deliberate exception: any resolver error
(network, parse, 404 on first use) falls back to auth_mode=subscription with an
open breaker — never blocks a job on a resolver bug. A missing
state/capacity.json is created on the first trip. Every gh call that isn't
essential to the decision is wrapped so it can't raise.

The exception is the FAILOVER POLICY itself (DEFAULT_AUTH's
fallback_on_capacity_trip). An unreadable config/auth.json, or one without
the key, defaults to False, so a run that hits an already-tripped breaker with no readable policy SKIPS
rather than failing over to metered API. See the comment on DEFAULT_AUTH for
why this one case is fail-closed. An unreadable policy on an UNTRIPPED breaker
still proceeds normally on subscription — pausing is only ever the outcome when
the breaker is genuinely open.

Account pool (``pool`` in config/auth.json, from providers.anthropic.pool):
resolve picks the account for this job (squadhq.account_pool.select: first
untripped, or rotating with pool_strategy round_robin) and additionally writes
``account`` (the non-secret id) and ``auth_secret`` (the secret NAME the agent
job maps to a credential) to $GITHUB_OUTPUT; auth_mode is that account's auth.
check-trip trips only the account that ran (ACCOUNT, or the execution
result's ``account``), regardless of auth mode. Only when EVERY pool account is
tripped does the fleet behaviour above apply (skip, or metered API with
fallback_on_capacity_trip). ``select`` prints the id resolve would pick, or
``none``.

Required env for both subcommands: HUB_REPO, GH_TOKEN (for gh CLI).
resolve additionally needs: REPO, ISSUE_OR_PR (optional), COMMENT_ON_SKIP
(optional "true" for label-triggered jobs).
check-trip additionally needs: EXECUTION_FILE, RUN_URL, JOB, WORKFLOW,
AUTH_MODE (the resolve step's own output, passed back in), ACCOUNT (pool only).
"""
import datetime
import json
import os
import random
import sys
import time

from squadhq import account_pool, github, untrusted

CAPACITY_PATH = "state/capacity.json"
AUTH_PATH = "config/auth.json"
CAPACITY_ESCALATION_TITLE = "Claude capacity window exhausted"
# Fail-closed on policy-read failure: if we can't read the policy, we can't know
# the approver wants failover, so we pause. A false pause is cheaper than a
# surprise bill.
#
# This is the ONLY fail-closed default in this file; everything else fails
# open (see the module docstring). Failing open on an unreadable resolver never
# costs money, but failing open on an unreadable *policy* is precisely how an
# unattended fleet runs up metered spend the approver had switched off.
DEFAULT_AUTH = {"coding_agents_auth": "subscription", "fallback_on_capacity_trip": False}
DEFAULT_CAPACITY = {"paused_until": "", "tripped_by": "", "tripped_at": "", "history": []}

TRIP_SIGNATURES = (
    "hit your session limit",
    "hit your weekly limit",
    "hit your Opus limit",
)


def sh(args):
    """Seam: ``args`` is a full ``gh ...`` argv (kept for test mocking)."""
    if args and args[0] == "gh":
        args = args[1:]
    return github.client().run(args)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def now_iso():
    return utcnow().isoformat()


def parse_ts(value):
    """ISO-8601 -> aware UTC datetime, or None when empty/unparseable. Naive
    timestamps are taken as UTC; "Z" and offsets are honoured."""
    if not value or not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc)


def fallback_enabled(auth):
    """a missing key means NO failover to metered spend."""
    return (auth or {}).get("fallback_on_capacity_trip", DEFAULT_AUTH["fallback_on_capacity_trip"]) is True


def read_hub_json(hub_repo, path, default):
    """-> (parsed JSON, blob sha). Missing/unreadable -> (copy of default,
    None) — fail open."""
    try:
        text, sha = github.client().contents_get(hub_repo, path)
    except Exception:
        return dict(default), None
    if text is None:
        return dict(default), None
    try:
        data = json.loads(text)
    except ValueError:
        return dict(default), sha
    if not isinstance(data, dict):
        return dict(default), sha
    return data, sha


def bot_identity():
    return github.bot_identity()


def write_capacity(hub_repo, new_state, message, action):
    """action="trip"|"clear". Compare-before-write so concurrent resolvers
    don't fight: a trip already covered by a later paused_until, or a clear
    against an already-clear state, is success with no write. A missing file
    is created (PUT without sha); a lost race (stale sha) re-reads and
    retries."""
    gh = github.client()
    for attempt in range(1, 4):
        current, sha = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
        cur_until = parse_ts(current.get("paused_until"))
        if action == "clear" and cur_until is None:
            return True
        new_until = parse_ts(new_state.get("paused_until"))
        if action == "trip" and cur_until and new_until and cur_until >= new_until:
            return True
        text = json.dumps(new_state, indent=2) + "\n"
        if gh.contents_put(hub_repo, CAPACITY_PATH, text, message, sha=sha):
            return True
        if attempt < 3:
            time.sleep(random.uniform(2, 5))  # noqa: S311 - jitter, not crypto
    return False


def dedupe_comment(repo, issue_or_pr, body):
    if not issue_or_pr:
        return
    existing = github.client().paginate(f"repos/{repo}/issues/{issue_or_pr}/comments", default=None)
    # Only OUR earlier notice dedupes: a human pasting the same text must not
    # suppress it.
    bodies = {c.get("body") for c in existing or [] if isinstance(c, dict) and untrusted.by_bot(c)}
    if body in bodies:
        return
    sh(["gh", "issue", "comment", str(issue_or_pr), "-R", repo, "-b", body])


def find_or_create_escalation(hub_repo, title, body):
    return github.client().find_or_create_issue_by_title(
        hub_repo, title, body, labels=["escalation"], comment_body=body)


_EMITTED = False


def _append(path, text):
    with open(path, "a") as f:
        f.write(text)


# --------------------------------------------------------------------------
# account pool
# --------------------------------------------------------------------------

def actions_pool(auth):
    """(usable accounts, strategy, configured?) for Actions mode. An account
    whose secret is not one of the declared workflow secret slots can never
    reach the agent job, so it is skipped (with a log line, never fatal)."""
    accounts, strategy = account_pool.parse_pool(auth)
    usable = []
    for a in accounts:
        if account_pool.actions_slot_ok(a):
            usable.append(a)
        else:
            print(f"capacity_gate: pool account {a.id!r} uses secret {a.secret} which Actions workflows do not "
                  f"declare (use {account_pool.EXECUTOR_ENV[a.auth]}_1..{account_pool.POOL_MAX}) — skipped")
    return usable, strategy, bool(accounts)


def rotation_seed(job):
    return account_pool.rotation_seed(os.environ.get("GITHUB_RUN_ID", ""),
                                      os.environ.get("GITHUB_RUN_ATTEMPT", ""), job)


def select_account(auth, capacity, job="", now=None):
    """The pool account the next job uses, or None (pool exhausted or not configured)."""
    accounts, strategy, _ = actions_pool(auth)
    return account_pool.select(accounts, capacity, now or utcnow(), strategy, rotation_seed(job))


def pool_has_available(hub_repo, now=None):
    """-> (pool configured, an account is available now, pool size). Fail-safe:
    an unreadable state reads as 'all available' only for the decision to
    rerun, which itself re-resolves from scratch."""
    auth, _ = read_hub_json(hub_repo, AUTH_PATH, DEFAULT_AUTH)
    accounts, _strategy, configured = actions_pool(auth)
    if not configured:
        return False, False, 0
    capacity, _ = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
    return True, bool(account_pool.available(accounts, capacity, now or utcnow())), len(accounts)


def write_account_trip(hub_repo, account_id, by, now=None):
    """Trip one pool account (compare-before-write: an existing later window
    is success with no write; a lost race re-reads and retries)."""
    gh = github.client()
    now = now or utcnow()
    want = now + datetime.timedelta(minutes=account_pool.TRIP_MINUTES)
    for attempt in range(1, 4):
        current, sha = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
        cur = account_pool.tripped_until(current, account_id, now)
        if cur and cur >= want:
            return True
        new_state = account_pool.trip(current, account_id, now, by)
        text = json.dumps(new_state, indent=2) + "\n"
        if gh.contents_put(hub_repo, CAPACITY_PATH, text,
                           f"chore(capacity): trip account {account_id} until {want.isoformat()}", sha=sha):
            return True
        if attempt < 3:
            time.sleep(random.uniform(2, 5))  # noqa: S311 - jitter, not crypto
    return False


def cmd_resolve():
    # Decide the mode FIRST and emit it immediately — every side effect below
    # (probe-clear write, step summary, dedup comment) is best-effort and
    # individually guarded so a side-effect failure can never retroactively
    # change (or re-emit over) an already-decided, already-written auth_mode.
    hub_repo = os.environ.get("HUB_REPO", "")
    repo = os.environ.get("REPO", "")
    issue_or_pr = os.environ.get("ISSUE_OR_PR") or ""
    job = os.environ.get("JOB", "job")
    comment_on_skip = os.environ.get("COMMENT_ON_SKIP", "") == "true"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    out_path = os.environ["GITHUB_OUTPUT"]

    def emit(mode, account=None, secret=""):
        # account/auth_secret are written only on the pool path, so a
        # single-credential install keeps its one-line output unchanged.
        global _EMITTED
        text = f"auth_mode={mode}\n"
        if account is not None:
            text += f"account={account}\nauth_secret={secret}\n"
        _append(out_path, text)
        _EMITTED = True

    def best_effort(fn):
        try:
            fn()
        except Exception as e:
            print(f"capacity_gate: best-effort step failed: {e!r}")

    auth, _ = read_hub_json(hub_repo, AUTH_PATH, DEFAULT_AUTH)
    coding_auth = auth.get("coding_agents_auth", "subscription")
    fallback = fallback_enabled(auth)

    accounts, strategy, pooled = actions_pool(auth)
    if pooled:
        _resolve_pool(accounts, strategy, fallback, emit, best_effort, job, repo, issue_or_pr,
                      comment_on_skip, summary_path, hub_repo)
        return

    if coding_auth == "api":
        emit("api")
        return

    capacity, _ = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
    raw_until = capacity.get("paused_until") or ""
    paused_until = parse_ts(raw_until)

    if paused_until is None:
        if raw_until:
            print(f"capacity_gate: unparseable paused_until {raw_until!r} — treating the breaker as closed")
        emit("subscription")
        return

    if paused_until <= utcnow():
        # Probe recovery: the first resolver through clears it and proceeds on
        # subscription as canary. If this run trips again, check-trip re-arms
        # the same +60m window.
        emit("subscription")
        best_effort(lambda: write_capacity(
            hub_repo, dict(DEFAULT_CAPACITY, history=capacity.get("history", [])),
            "chore(capacity): clear", "clear"))
        if summary_path:
            best_effort(lambda: _append(summary_path,
                f"\ncapacity breaker probe [{now_iso()}]: window passed, clearing "
                f"and retrying {job} on subscription\n"))
        return

    # Still tripped.
    if fallback:
        emit("api")
        if summary_path:
            best_effort(lambda: _append(summary_path,
                f"\ncapacity tripped [{now_iso()}]: {job} failing over to metered API\n"))
        return

    emit("skip")
    if summary_path:
        # Normalized skip format: "⏭ SKIP <step>: <reason> [<timestamp UTC>]".
        best_effort(lambda: _append(summary_path,
            f"\n⏭ SKIP {job}: capacity breaker open until {raw_until} [{now_iso()}]\n"))
    if comment_on_skip and repo and issue_or_pr:
        best_effort(lambda: dedupe_comment(
            repo, issue_or_pr,
            f"Paused — capacity window exhausted, resuming after {raw_until}"))


def _resolve_pool(accounts, strategy, fallback, emit, best_effort, job, repo, issue_or_pr,
                  comment_on_skip, summary_path, hub_repo):
    capacity, _ = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
    now = utcnow()
    acct = account_pool.select(accounts, capacity, now, strategy, rotation_seed(job))
    if acct is not None:
        emit(acct.auth, acct.id, acct.secret)
        if summary_path:
            best_effort(lambda: _append(summary_path,
                f"\naccount pool [{now_iso()}]: {job} runs on account {acct.id} ({acct.auth}, {strategy})\n"))
        return
    until = account_pool.earliest_recovery(accounts, capacity, now)
    raw_until = until.isoformat() if until else "(no usable pool account)"
    if fallback:
        emit("api", "", "")  # the single ANTHROPIC_API_KEY: metered
        if summary_path:
            best_effort(lambda: _append(summary_path,
                f"\ncapacity tripped on every pool account [{now_iso()}]: {job} failing over to metered API\n"))
        return
    emit("skip", "", "")
    if summary_path:
        best_effort(lambda: _append(summary_path,
            f"\n⏭ SKIP {job}: every pool account's capacity breaker is open until {raw_until} [{now_iso()}]\n"))
    if comment_on_skip and repo and issue_or_pr:
        best_effort(lambda: dedupe_comment(
            repo, issue_or_pr,
            f"Paused — capacity window exhausted on every pool account, resuming after {raw_until}"))


def cmd_select(argv=()):
    """Print the pool account id the next job would use, or "none".
    ``--auth FILE --state FILE`` read local files instead of the hub."""
    args = list(argv)

    def opt(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else ""

    def local(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return dict(default)
        return data if isinstance(data, dict) else dict(default)

    hub_repo = os.environ.get("HUB_REPO", "")
    auth = local(opt("--auth"), DEFAULT_AUTH) if opt("--auth") else read_hub_json(hub_repo, AUTH_PATH, DEFAULT_AUTH)[0]
    capacity = (local(opt("--state"), DEFAULT_CAPACITY) if opt("--state")
                else read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)[0])
    acct = select_account(auth, capacity, os.environ.get("JOB", ""))
    print(acct.id if acct else "none")
    return acct


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


def _final_error_text(data):
    """Error text from the executor's OWN final result event only — never
    tool outputs, assistant text or user/issue content echoed into the
    transcript (an issue saying "hit your session limit" must not pause the
    fleet). Claude Code's stream-json ends with one {"type": "result"} event;
    its `result` text only counts when the event is an error."""
    if isinstance(data, list):
        results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
        data = results[-1] if results else None
    if not isinstance(data, dict) or data.get("type") not in (None, "result"):
        return ""
    parts = []
    for key in ("error", "error_message"):
        v = data.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict) and isinstance(v.get("message"), str):
            parts.append(v["message"])
    if data.get("is_error") is True and isinstance(data.get("result"), str):
        parts.append(data["result"])
    return "\n".join(parts)


def trip_in_execution_file(path):
    """True when the executor's final result event reports a plan limit.
    Unreadable or non-JSON files never trip (fail safe against injected text)."""
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            raw = f.read()
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        # JSONL stream: one event per line; only the last result event counts.
        events = []
        for line in raw.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        data = events
    text = _final_error_text(data)
    return any(sig in text for sig in TRIP_SIGNATURES)


def cmd_check_trip():
    auth_mode = os.environ.get("AUTH_MODE")
    result = load_execution_result()
    # The account comes from the prep job (trusted outputs), never the agent.
    account = (getattr(result, "account", "") if result is not None else "") or os.environ.get("ACCOUNT", "")
    if account and not account_pool.ID_RE.match(account):
        print(f"capacity_gate: ignoring malformed account id {account[:40]!r}")
        account = ""
    if auth_mode == "skip" or (not account and auth_mode != "subscription"):
        return  # skipped runs, and single-credential api runs, can't hit a plan limit
    if result is not None:
        # The executor's normalized verdict is the ONLY source when present:
        # transcript text can contain anything the agent read.
        tripped = bool(result.capacity_tripped)
    else:
        tripped = trip_in_execution_file(os.environ.get("EXECUTION_FILE", ""))
    if not tripped:
        return

    hub_repo = os.environ.get("HUB_REPO", "")
    run_url = os.environ.get("RUN_URL", "")
    job = os.environ.get("JOB", "")
    workflow = os.environ.get("WORKFLOW", "")
    if account:
        _trip_pool_account(hub_repo, account, run_url, job, workflow)
        return
    now = now_iso()
    paused_until = (utcnow() + datetime.timedelta(minutes=60)).isoformat()

    capacity, _ = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
    history = list(capacity.get("history") or []) + [{"at": now, "action": "trip", "by": run_url}]
    new_state = {
        "paused_until": paused_until,
        "tripped_by": run_url,
        "tripped_at": now,
        "history": history[-20:],
    }
    write_capacity(hub_repo, new_state, f"chore(capacity): trip until {paused_until}", "trip")

    auth, _ = read_hub_json(hub_repo, AUTH_PATH, DEFAULT_AUTH)
    if fallback_enabled(auth):
        body = (f"fleet failed over to metered API; costs accruing until the window "
                f"resets or you set fallback false.\n\nRun: {run_url} ({workflow} / {job})")
    else:
        body = (f"coding agents paused until {paused_until}.\n\nRun: {run_url} "
                f"({workflow} / {job})")
    body += ("\n\nOverride: edit state/capacity.json (empty paused_until = resume now; "
             "later timestamp = extend) or config/auth.json to change policy — web UI "
             "edits are fine.")
    find_or_create_escalation(hub_repo, CAPACITY_ESCALATION_TITLE, body)


def _trip_pool_account(hub_repo, account, run_url, job, workflow):
    now = utcnow()
    write_account_trip(hub_repo, account, run_url, now)
    auth, _ = read_hub_json(hub_repo, AUTH_PATH, DEFAULT_AUTH)
    accounts, _strategy, _ = actions_pool(auth)
    capacity, _ = read_hub_json(hub_repo, CAPACITY_PATH, DEFAULT_CAPACITY)
    left = [a.id for a in account_pool.available(accounts, capacity, now) if a.id != account]
    until = (now + datetime.timedelta(minutes=account_pool.TRIP_MINUTES)).isoformat()
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    line = (f"capacity: pool account {account} tripped until {until}; "
            + (f"{len(left)} account(s) left: {', '.join(left)}" if left else "no pool account left"))
    print(f"capacity_gate: {line}")
    if summary_path:
        try:
            _append(summary_path, f"\n{line}\n")
        except OSError as e:
            print(f"capacity_gate: could not write step summary: {e!r}")
    if left:
        return  # the next job (or the failure handler's rerun) uses the next account
    recovery = account_pool.earliest_recovery(accounts, capacity, now)
    resume = recovery.isoformat() if recovery else until
    if fallback_enabled(auth):
        body = (f"every pool account is exhausted; the fleet failed over to metered API (ANTHROPIC_API_KEY); "
                f"costs accrue until {resume} or you set fallback false.\n\nRun: {run_url} ({workflow} / {job})")
    else:
        body = f"every pool account is exhausted; coding agents paused until {resume}.\n\nRun: {run_url} ({workflow} / {job})"
    body += ("\n\nOverride: edit state/capacity.json (clear accounts.<id>.tripped_until = that account resumes "
             "now) or squadhq.yml providers.anthropic.pool — web UI edits are fine.")
    find_or_create_escalation(hub_repo, CAPACITY_ESCALATION_TITLE, body)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "resolve":
        cmd_resolve()
    elif cmd == "check-trip":
        cmd_check_trip()
    elif cmd == "select":
        cmd_select(argv[1:])
    else:
        print(f"capacity_gate: unknown subcommand {cmd!r}")


def run(argv=None):
    """Entry point: fail open — an unhandled resolver error must never block
    the job. If resolve threw before emitting auth_mode, default to
    subscription with an open breaker."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        main(argv)
    except Exception as e:
        print(f"capacity_gate: swallowed error: {e!r}")
        out_path = os.environ.get("GITHUB_OUTPUT")
        # Only when nothing was emitted yet: a second auth_mode line would
        # override the decision already written (last value wins).
        if argv[:1] == ["resolve"] and out_path and not _EMITTED:
            try:
                _append(out_path, "auth_mode=subscription\n")
            except Exception as e2:
                print(f"capacity_gate: could not write fallback auth_mode: {e2!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
