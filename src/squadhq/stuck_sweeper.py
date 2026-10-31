#!/usr/bin/env python3
"""
Stuck-ticket sweeper: finds issues/PRs in every project repo that are waiting
on AUTOMATION past a threshold, and re-fires the event they are waiting for.

  python -m squadhq.stuck_sweeper [--dry-run] [--repo org/name]

Deterministic, no LLM. Each stuck state maps to exactly one re-fire:

  state                   waiting on        re-fire
  ----------------------  ----------------  ------------------------------------
  mention_unanswered      mention           ASK once — only a human @claude can restart it
  approver_reply_unanswered po-continue      add agent:continue (po-continue labeled path)
  impact_missing          po-shape          add agent:shape — PO adds the missing "Why this matters"
  unshaped                po-shape          add agent:shape (po-shape labeled path)
  proposal_missing        po-shape          drop stray proposal:pending, add agent:shape
  team_thread             team-thread       re-post the @team-thread trigger comment
  dispatched_not_started  implement         remove + re-add agent:implement
  in_progress_stalled     implement         drop status:in-progress, re-add agent:implement
  design_stalled          design            drop status:in-progress, re-add needs-design
  implement_stopped       (agent ran)       ESCALATE only — Engineer commented, no PR
  design_not_started      design            remove + re-add needs-design
  design_stopped          (agent ran)       ESCALATE only — Architect commented, no PR
  review_missing          review            draft toggle (fires ready_for_review)
  fix_round_not_started   review-fix        remove + re-add review:changes-requested
  merge_decision_missing  self-merge        remove + re-add ready-to-merge
  merge_recheck           self-merge        risk:low PR sent to an approver only for failing
                                            checks that pass now: drop needs-approver-merge,
                                            re-add ready-to-merge

First, each open ticket is RECONCILED with facts (reconcile_issue): its
implementation PR (Closes #N) and design PR (Gate-for: #N) — open, merged or
closed unmerged — decide its status labels, not the other way round. That
clears design gates the moment the design merges, moves tickets to review,
closes merged work, drops stale status:in-review, queues overridden parks and
escalates PRs a human closed. Closed issues lose leftover work labels.

Then each item is NORMALIZED: contradictory label sets are
fixed by removing the side that is stale (normalize_issue / normalize_pr —
e.g. triaged:ready left on a dispatched ticket, agent:implement next to an
un-cleared needs-design, ready-to-merge next to a newer change request). A
contradiction only the approver can settle (parked AND dispatched) escalates.
The corrected state is then classified like any other, so the trigger it is
waiting for fires in the same run. Label ADDS are all event-triggering (ops
App token), so the router job for the new state starts on its own.

After flood-guards.stuck_sweeper.refire_attempts_max re-fires of the same state
inside refire_window_hours, the item is escalated instead (needs-human + a
comment) so it is never stuck silently. A approver removing needs-human resets
the attempt count (only re-fires newer than the last escalation count).

CAPACITY: each run reads the hub's state/capacity.json. While the pause is
live the run does nothing. Once paused_until has passed it clears the breaker
and closes the "Claude capacity window exhausted" escalation itself (instead of
waiting for the next agent job to probe it), then sweeps as usual — which
re-fires everything the pause skipped.

Items waiting on the APPROVER are never touched (APPROVER_WAIT_LABELS, and
proposal:pending with no pending team-thread). The whole run is a no-op while
the capacity breaker is open (state/capacity.json paused_until in the future).

Every action posts a badged comment carrying a `<!-- stuck-sweeper ... -->`
marker — that marker is both the audit trail and the attempt counter. Marker
comments never contain '@team-thread' or '@claude' except the team-thread
re-fire itself, which must.

WIP: one function, wip_slots(), decides how many tickets may be in flight;
pr_facts()'s wip_free and plan_dispatch() both use it. The sweeper and
ticket_transition.py dispatch from their own snapshots, so the two
workflows (stuck-sweeper.yml and router.yml's ticket-transition job) MUST
share one concurrency group per project repo, e.g.
`concurrency: {group: squadhq-dispatch-${{ <project repo> }}, cancel-in-progress: false}`
— otherwise two runs can each see a free slot and together exceed the limit.

Every capped list call (`--limit N`) warns when it returns N items (older
items are then not seen) — the warning lands in the run summary.

Required env: GH_TOKEN (ops App token — events it causes DO trigger workflows,
unlike GITHUB_TOKEN), SQUADHQ_HUB_REPO (or HUB_REPO) for the capacity
breaker. Limits and thresholds: config/flows.yml flood-guards.stuck_sweeper.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

from squadhq import capacity_gate, debt_dedupe, flow_reader, github, settings, untrusted
from squadhq.paths import hub_path
from squadhq.policy import implementer

BADGE = "[Stuck Sweeper] "
MARKER_RE = re.compile(r"<!-- stuck-sweeper state=([a-z_]+)(?: (escalated))? -->")
TEAM_THREAD_DONE = "<!-- team-thread-complete -->"
HUMAN_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
CAPACITY_ESCALATION_TITLE = getattr(capacity_gate, "CAPACITY_ESCALATION_TITLE",
                                    "Claude capacity window exhausted")

# Waiting on a human, not on automation: never re-fire, never escalate.
APPROVER_WAIT_LABELS = {"needs-human", "status:in-review", "needs-approver-merge", "awaiting-approval"}
# Any of these means intake already produced an outcome (or the issue never
# enters PO intake at all), so the issue is not "unshaped".
SHAPED_OR_NON_INTAKE_LABELS = {
    "triaged:ready", "proposal:pending", "verdict:proceed", "verdict:backlog",
    "verdict:decline", "verdict:overridden", "agent:implement", "needs-design",
    "debt", "debt-proposal", "escalation", "status:in-progress", "P0", "P1", "P2", "P3",
}
PR_VERDICT_LABELS = {"ready-to-merge", "review:changes-requested", "needs-human", "needs-approver-merge"}
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
GATE_FOR_RE = re.compile(r"Gate-for:\s*#(\d+)", re.I)


# ---------------------------------------------------------------- time helpers

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_ts(s):
    if not s:
        return None
    t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def safe_ts(s):
    """parse_ts that returns None for an unparseable value instead of raising."""
    try:
        return parse_ts(s) if isinstance(s, str) else None
    except ValueError:
        return None


def minutes_since(ts, now):
    return (now - ts).total_seconds() / 60 if ts else None


# ---------------------------------------------------------------- gh seams

def gh(args):
    return github.client().run(args)


def gh_json(args):
    r = gh(args)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()}")
    return json.loads(r.stdout or "null")


def gh_list(path):
    """Paginated REST list, one JSON object per line (avoids gh's `][` page join)."""
    r = gh(["api", "--paginate", path, "--jq", ".[] | tojson"])
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path}: {r.stderr.strip()}")
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


# Cap warnings collected during a run; main() prints them in the summary.
CAP_WARNINGS: list[str] = []


def gh_capped(args, limit):
    """`gh <list> ... --limit N` -> items, recording a warning when the result
    fills the cap (anything beyond it is silently missing otherwise)."""
    items = gh_json([*args, "--limit", str(limit)]) or []
    if len(items) >= limit:
        what = " ".join(a for a in args[:4] if not a.startswith("-"))
        msg = f"{what}: hit the {limit}-item cap — older items were not seen this run"
        if msg not in CAP_WARNINGS:
            CAP_WARNINGS.append(msg)
            print(f"warning: {msg}", file=sys.stderr)
    return items


def failed(r):
    """' FAILED: <stderr>' for a non-zero gh result, else ''."""
    return f" FAILED: {(r.stderr or '').strip()[:120]}" if r.returncode else ""


def _benign_remove_error(r):
    # Removing a label that is not on the item is not a failure.
    err = (r.stderr or "").lower()
    return "not found" in err or "does not have" in err or "not exist" in err


# ---------------------------------------------------------------- pure logic

def label_names(item):
    return {l["name"] for l in item.get("labels", [])}


def last_labeled_at(events, label):
    times = [parse_ts(e["created_at"]) for e in events
             if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    return max(times) if times else None


# ---------------------------------------------------------------- trust
#
# Anyone who can comment can TYPE a badge ("[Engineer] ..."), a hidden marker
# ("<!-- stuck-sweeper state=... -->") or a protocol line ("Gate-for: #N").
# Every one of them that drives a decision here counts only when our bot
# wrote it; human input (approver replies, @-mentions) counts only from a
# trusted association (SQUADHQ_TRUSTED_ASSOCIATIONS, default
# OWNER/MEMBER/COLLABORATOR). Issue/PR bodies count only when their author is
# the bot or trusted — GitHub does not tell us who last EDITED a body, so a
# collaborator-level editor of someone else's issue remains a trusted path.


def bot_said(c, prefix=None, contains=None):
    """``c`` is one of OUR role comments (ops or agent identity: [Engineer],
    [Product Owner], @team-thread ... are posted by agent jobs), starting with
    ``prefix`` / containing ``contains``."""
    if not untrusted.by_automation(c):
        return False
    body = c.get("body") or ""
    return (prefix is None or body.startswith(prefix)) and (contains is None or contains in body)


def ops_said(c, prefix=None, contains=None):
    """Like :func:`bot_said` but OPS identity only: the sweeper's own state
    markers are protocol and an agent must not be able to forge them."""
    if not untrusted.by_bot(c):
        return False
    body = c.get("body") or ""
    return (prefix is None or body.startswith(prefix)) and (contains is None or contains in body)


def trusted_author(item):
    """Issue/PR written by our automation (ops or agent: agents open the PRs)
    or a trusted association. Association comes from
    ``authorAssociation``/``author_association`` (see enrich_trust); a missing
    value is untrusted (fail closed)."""
    return untrusted.by_automation_or_trusted(item)


def enrich_trust(repo, items):
    """gh issue/pr list JSON has no authorAssociation: look it up (REST) for
    non-bot items whose body carries a protocol line we would act on.
    A failed lookup leaves the item untrusted."""
    for it in items or []:
        if untrusted.association_of(it) or untrusted.by_automation(it):
            continue
        body = it.get("body") or ""
        if not (AFTER_RE.search(body) or WHY_RE.search(body) or GATE_FOR_RE.search(body) or CLOSES_RE.search(body)):
            continue
        r = gh(["api", f"repos/{repo}/issues/{it['number']}", "--jq", ".author_association"])
        it["authorAssociation"] = (r.stdout.strip().upper() if r.returncode == 0 else "") or "NONE"
    return items


def agent_spoke_after(comments, badge, since):
    """The dispatched agent already ran and commented (declined / blocked) after
    the dispatch label — a re-fire would just repeat that answer."""
    return any(bot_said(c, badge) and parse_ts(c["created_at"]) > since for c in comments)


def attempts(comments, state, window_start):
    """(refires counted toward the cap, whether an escalation is already live).
    Only re-fires after the latest escalation count, so a approver clearing
    needs-human gets a fresh set of attempts."""
    refires, last_escalation = [], None
    for c in comments:
        m = MARKER_RE.search(c.get("body") or "") if untrusted.by_bot(c) else None
        if not m or m.group(1) != state:
            continue
        at = parse_ts(c["created_at"])
        if m.group(2):
            last_escalation = max(last_escalation or at, at)
        else:
            refires.append(at)
    floor = max(window_start, last_escalation) if last_escalation else window_start
    return sum(1 for t in refires if t > floor), last_escalation is not None and last_escalation > window_start


def is_human(c):
    """A trusted-association human (not a bot of any kind)."""
    login = untrusted.author_login(c)
    return (untrusted.by_trusted_human(c) and (c.get("user") or {}).get("type") != "Bot"
            and not login.endswith("[bot]") and not login.startswith("app/"))


def is_sweeper(c):
    return ops_said(c, BADGE)


def mention_phrases():
    """The configured trigger (SQUADHQ_TRIGGER, default @squad) plus the
    legacy @claude, which stays accepted."""
    return {settings.trigger_phrase(), "@claude"}


MENTION_MAX_AGE_MIN = 7 * 24 * 60


def mention_unanswered(ctx, now, th):
    """A human's @claude comment no bot answered — the mention job was skipped
    (capacity) or failed. Only a HUMAN comment can start that job, so the
    sweeper can only ask for it again, once per mention."""
    comments = ctx["comments"]
    phrases = mention_phrases()
    asks = [i for i, c in enumerate(comments) if is_human(c) and any(p in (c.get("body") or "") for p in phrases)]
    if not asks:
        return None
    last = asks[-1]
    after = comments[last + 1:]
    if any(untrusted.by_automation(c) and not is_sweeper(c) for c in after):
        return None
    if any(is_sweeper(c) and "state=mention_unanswered" in (c.get("body") or "") for c in after):
        return None  # already asked about this one
    age = minutes_since(parse_ts(comments[last]["created_at"]), now)
    if age and age > MENTION_MAX_AGE_MIN:
        return None  # old enough that asking again is noise (e.g. mentions from a previous setup)
    return ("mention_unanswered", age) if age and age > th["mention_unanswered"] else None


def approver_reply_unanswered(issue, ctx, now, th):
    """The approver replied on a proposal / parked item and the PO never
    answered — po-continue was skipped (capacity) or failed. Re-run it via the
    agent:continue label."""
    if not label_names(issue) & {"proposal:pending", "verdict:backlog", "verdict:decline"}:
        return None
    comments = ctx["comments"]
    human = [i for i, c in enumerate(comments) if is_human(c)]
    if not human:
        return None
    last = human[-1]
    if any(bot_said(c, "[Product Owner]") for c in comments[last + 1:]):
        return None
    since = parse_ts(comments[last]["created_at"])
    if "agent:continue" in label_names(issue):
        since = max(since, last_labeled_at(ctx["events"], "agent:continue") or since)
    age = minutes_since(since, now)
    return ("approver_reply_unanswered", age) if age and age > th["approver_reply_unanswered"] else None


def classify_issue(issue, ctx, now, th):
    """Return (state, age_minutes) for an issue stuck on automation, else None.
    ctx: events, comments, open_pr_closes (set of issue numbers with an open PR
    closing them), gate_prs (set of issue numbers with an open or merged
    Gate-for PR)."""
    hit = mention_unanswered(ctx, now, th) or approver_reply_unanswered(issue, ctx, now, th)
    if hit:
        return hit
    labels = label_names(issue)
    if labels & APPROVER_WAIT_LABELS:
        return None
    n = issue["number"]
    if labels & {"triaged:ready", "agent:implement"} and not labels & STATUS \
            and n not in ctx["open_pr_closes"] and not has_why(issue, ctx["comments"]):
        since = last_labeled_at(ctx["events"], "agent:shape") if "agent:shape" in labels else None
        age = minutes_since(since or parse_ts(issue["createdAt"]), now)
        return ("impact_missing", age) if age and age > th["impact_missing"] else None

    if "agent:implement" in labels and "status:in-progress" in labels and n not in ctx["open_pr_closes"]:
        since = last_labeled_at(ctx["events"], "status:in-progress")
        age = minutes_since(since, now)
        if age and age > th["in_progress_stalled"] and not agent_spoke_after(ctx["comments"], "[Engineer]", since):
            return "in_progress_stalled", age
        return None
    if "needs-design" in labels and "status:in-progress" in labels and n not in ctx["gate_prs"]:
        since = last_labeled_at(ctx["events"], "status:in-progress")
        age = minutes_since(since, now)
        if age and age > th["in_progress_stalled"] and not agent_spoke_after(ctx["comments"], "[Architect]", since):
            return "design_stalled", age
        return None

    if "agent:implement" in labels:
        if labels & {"status:in-progress", "needs-design", "P3"} or n in ctx["open_pr_closes"]:
            return None
        since = last_labeled_at(ctx["events"], "agent:implement")
        age = minutes_since(since, now)
        if not (age and age > th["dispatched_not_started"]):
            return None
        return ("implement_stopped" if engineer_block(ctx, since, issue)
                else "dispatched_not_started"), age

    if "needs-design" in labels:
        if "status:in-progress" in labels or n in ctx["gate_prs"]:
            return None
        since = last_labeled_at(ctx["events"], "needs-design")
        age = minutes_since(since, now)
        if not (age and age > th["design_not_started"]):
            return None
        return ("design_stopped" if agent_spoke_after(ctx["comments"], "[Architect]", since)
                else "design_not_started"), age

    if "proposal:pending" in labels:
        comments = ctx["comments"]
        if not any(bot_said(c, "[Product Owner]") for c in comments):
            # The label is there but the PO never posted anything — someone
            # other than the PO applied it (seen: CoS, on issues whose po-shape
            # run had failed). Nothing for the approver to answer: re-shape.
            since = last_labeled_at(ctx["events"], "proposal:pending")
            age = minutes_since(since or parse_ts(issue["createdAt"]), now)
            return ("proposal_missing", age) if age and age > th["unshaped"] else None
        if any(bot_said(c, contains=TEAM_THREAD_DONE) for c in comments):
            return None
        triggers = [c for c in comments if bot_said(c, contains="@team-thread")]
        if not triggers:
            return None  # PO chose no team-thread: waiting on the approver's reply
        age = minutes_since(parse_ts(triggers[-1]["created_at"]), now)
        return ("team_thread", age) if age and age > th["team_thread"] else None

    if labels & SHAPED_OR_NON_INTAKE_LABELS or any(l.startswith("dept:") for l in labels):
        return None
    author = issue.get("author") or {}
    if author.get("is_bot") and not (untrusted.by_automation(issue) and "Originated-by:" in (issue.get("body") or "")):
        # Mirrors po-shape's opened gate: bot issues are pre-shaped, unless OUR
        # bot marked one self-actuated (Originated-by:). Another app's issue is
        # never intake, whatever its body says.
        return None
    since = last_labeled_at(ctx["events"], "agent:shape") if "agent:shape" in labels else None
    age = minutes_since(since or parse_ts(issue["createdAt"]), now)
    return ("unshaped", age) if age and age > th["unshaped"] else None


def classify_pr(pr, ctx, now, th):
    """Return (state, age_minutes) for a PR stuck on automation, else None.
    ctx: events, head_committed_at, review_checks (list of check-run dicts for
    the head SHA whose name is the router's review job)."""
    hit = mention_unanswered(ctx, now, th)
    if hit:
        return hit
    labels = label_names(pr)
    recovered = ctx.get("checks_recovered_since")
    if recovered and {"needs-approver-merge", "ready-to-merge"} <= labels and "needs-human" not in labels:
        age = minutes_since(recovered, now)
        return ("merge_recheck", age) if age and age > th["merge_decision_missing"] else None
    if labels & APPROVER_WAIT_LABELS or pr.get("isDraft"):
        return None

    if "ready-to-merge" in labels:
        age = minutes_since(last_labeled_at(ctx["events"], "ready-to-merge"), now)
        return ("merge_decision_missing", age) if age and age > th["merge_decision_missing"] else None

    if "review:changes-requested" in labels:
        labeled = last_labeled_at(ctx["events"], "review:changes-requested")
        head = ctx["head_committed_at"]
        if labeled and head and head > labeled:
            return None  # a fix round already pushed; review re-runs on synchronize
        age = minutes_since(labeled, now)
        return ("fix_round_not_started", age) if age and age > th["fix_round_not_started"] else None

    if labels & PR_VERDICT_LABELS:
        return None
    checks = ctx["review_checks"]
    if any(c.get("status") != "completed" for c in checks):
        return None  # queued / running
    if any(c.get("conclusion") in ("skipped", "neutral") for c in checks):
        return None  # gated off (trivial-only, base-only, a human's docs-only PR)
    if any(c.get("conclusion") == "success" for c in checks) and (
            not pr.get("headRefOid") or verdict_recorded(ctx.get("comments") or [], pr["headRefOid"])):
        return None  # reviewer ran and recorded a verdict for this head (a run that ended
        #              without one, e.g. every tool call denied, still needs a review)
    age = minutes_since(ctx["head_committed_at"], now)
    return ("review_missing", age) if age and age > th["review_missing"] else None


# ---------------------------------------------------------------- actions

REFIRE_NOTE = {
    "unshaped": "Product Owner intake never produced an outcome — re-running po-shape via `agent:shape`.",
    "impact_missing": "Shaped/dispatched without a \"Why this matters\" section, so it cannot be dispatched or implemented — re-running the Product Owner (`agent:shape`) to add it.",
    "proposal_missing": "Labeled `proposal:pending` but the Product Owner never posted a proposal — removing the stray label and re-running po-shape via `agent:shape`.",
    "approver_reply_unanswered": "Your latest reply never got a Product Owner answer (the run was skipped or failed) — re-running it via `agent:continue`.",
    "team_thread": "Team-thread assessment never completed — re-posting its trigger.",
    "in_progress_stalled": "Marked in progress, but no PR and no word from the Engineer since — the run most likely died. Re-dispatching.",
    "design_stalled": "Marked in progress, but no design PR and no word from the Architect since — the run most likely died. Re-requesting the design.",
    "dispatched_not_started": "Implementation was dispatched but never started — re-applying `agent:implement`.",
    "design_not_started": "Design was requested but never started — re-applying `needs-design`.",
    "review_missing": "No review verdict on the latest commit — re-requesting review (draft toggle).",
    "fix_round_not_started": "Changes were requested but no fix round started — re-applying `review:changes-requested`.",
    "merge_decision_missing": "Approved but the merge decision never ran — re-applying `ready-to-merge`.",
    "merge_recheck": "Sent to an approver only for failing checks, which pass now — re-running the merge decision.",
}
# States that escalate straight away instead of re-firing, with the reason.
NO_REFIRE = {
    "implement_stopped": "the Engineer already ran on this dispatch and stopped without a PR (see its comment above).",
    "design_stopped": "the Architect already ran on this request and stopped without a design PR (see its comment above).",
}


def comment(repo, n, state, text, escalated=False):
    tag = f"<!-- stuck-sweeper state={state}{' escalated' if escalated else ''} -->"
    return gh(["issue", "comment", str(n), "-R", repo, "--body", f"{BADGE}{text}\n\n{tag}"])


def remove_label(repo, n, label, is_pr=False):
    """-> the gh result; a 'label not on the item' failure counts as success."""
    r = gh(["pr" if is_pr else "issue", "edit", str(n), "-R", repo, "--remove-label", label])
    if r.returncode and _benign_remove_error(r):
        r = subprocess.CompletedProcess(getattr(r, "args", []), 0, getattr(r, "stdout", ""), r.stderr)
    return r


def relabel(repo, n, label, is_pr):
    """Remove + re-add, so the `labeled` event fires again. A real removal
    failure is returned (the re-add would not fire a fresh event)."""
    r = remove_label(repo, n, label, is_pr)
    if r.returncode:
        return r
    return gh(["pr" if is_pr else "issue", "edit", str(n), "-R", repo, "--add-label", label])


def ensure_label(repo, name, cache):
    if (repo, name) in cache:
        return
    meta = flow_reader.get_labels("dispatch").get(name) or {}
    desc = " ".join((meta.get("description") or name).split())[:100]
    gh(["label", "create", name, "-R", repo, "--color", meta.get("color", "ededed"),
        "--description", desc])  # already exists -> harmless error
    cache.add((repo, name))


def refire(repo, item, state, is_pr, ctx, label_cache):
    n = item["number"]
    if state == "unshaped":
        ensure_label(repo, "agent:shape", label_cache)
        r = relabel(repo, n, "agent:shape", False)
    elif state == "impact_missing":
        ensure_label(repo, "agent:shape", label_cache)
        r = relabel(repo, n, "agent:shape", False)
    elif state == "proposal_missing":
        ensure_label(repo, "agent:shape", label_cache)
        r = remove_label(repo, n, "proposal:pending")
        if r.returncode == 0:
            r = relabel(repo, n, "agent:shape", False)
    elif state == "approver_reply_unanswered":
        ensure_label(repo, "agent:continue", label_cache)
        r = relabel(repo, n, "agent:continue", False)
    elif state == "team_thread":
        trig = ([c for c in ctx["comments"] if bot_said(c, contains="@team-thread")]
                or [{"body": ""}])[-1]["body"]
        scope = next((l for l in trig.splitlines() if l.lower().startswith("team-input-scope:")),
                     "Team-input-scope: architect, engineer")
        r = gh(["issue", "comment", str(n), "-R", repo, "--body",
                f"{BADGE}{REFIRE_NOTE[state]}\n\n@team-thread\n{scope}\n\n"
                f"<!-- stuck-sweeper state={state} -->"])
        return r  # the trigger comment IS the audit comment
    elif state in ("in_progress_stalled", "design_stalled"):
        r = remove_label(repo, n, "status:in-progress")
        if r.returncode == 0:
            r = relabel(repo, n, "agent:implement" if state == "in_progress_stalled" else "needs-design", False)
    elif state == "dispatched_not_started":
        r = relabel(repo, n, "agent:implement", False)
    elif state == "design_not_started":
        r = relabel(repo, n, "needs-design", False)
    elif state == "fix_round_not_started":
        r = relabel(repo, n, "review:changes-requested", True)
    elif state == "merge_decision_missing":
        r = relabel(repo, n, "ready-to-merge", True)
    elif state == "merge_recheck":
        r = remove_label(repo, n, "needs-approver-merge", True)
        if r.returncode == 0:
            r = relabel(repo, n, "ready-to-merge", True)
    elif state == "review_missing":
        r = gh(["pr", "ready", str(n), "-R", repo, "--undo"])
        if r.returncode == 0:
            r = gh(["pr", "ready", str(n), "-R", repo])
    else:
        raise ValueError(state)
    if r.returncode == 0:
        comment(repo, n, state, REFIRE_NOTE[state])
    return r


def escalate(repo, item, state, is_pr, reason):
    n = item["number"]
    r = gh(["pr" if is_pr else "issue", "edit", str(n), "-R", repo, "--add-label", "needs-human"])
    if r.returncode:
        return r  # without needs-human the item would be re-fired again next run
    return comment(repo, n, state, f"Stuck in `{state}` — {reason} Needs an approver look; "
                   "check this item's recent Actions runs. Remove `needs-human` to let the "
                   "sweeper retry.", escalated=True)


# ---------------------------------------------------------------- sweep

def capacity_check(hub_repo, now, dry_run):
    """-> (paused, paused_until, note). Reads state/capacity.json from the hub
    via the Contents API (same source the job resolvers use). When the window
    has PASSED, resumes actively instead of waiting for the next agent job to
    stumble on it: clears the breaker and closes the capacity escalation
    (CoS duty 9 reads that closure as "capacity is back"). The rest of this
    run then re-fires whatever was skipped."""
    cap, _ = capacity_gate.read_hub_json(hub_repo, capacity_gate.CAPACITY_PATH, capacity_gate.DEFAULT_CAPACITY)
    raw = (cap or {}).get("paused_until") or ""
    until = safe_ts(raw)
    if raw and until is None:
        # Unparseable pause: fail toward pausing (a false pause costs one
        # hour; sweeping into a live capacity trip re-fires into skips).
        return True, None, f"Capacity breaker paused_until {raw!r} is unparseable — sweep skipped; fix state/capacity.json."
    if until and until > now:
        return True, until, f"Capacity breaker open until {until.isoformat()} — sweep skipped."
    notes = []
    if until:
        if not dry_run:
            capacity_gate.write_capacity(
                hub_repo, dict(capacity_gate.DEFAULT_CAPACITY, history=cap.get("history", [])),
                "chore(capacity): clear — window passed (stuck sweeper)", "clear")
        notes.append(f"Capacity window ended {until.isoformat()} — breaker {'would be ' if dry_run else ''}cleared.")
    for number in open_issues_titled(hub_repo, CAPACITY_ESCALATION_TITLE):
        res = ""
        if not dry_run:
            r = gh(["issue", "close", str(number), "-R", hub_repo, "--reason", "completed", "--comment",
                    f"{BADGE}Capacity is back — breaker clear{f' (window ended {until.isoformat()})' if until else ''}. "
                    "Work skipped during the pause is being re-fired by the stuck sweeper. "
                    "The next trip opens a fresh issue."])
            res = failed(r)
        notes.append(f"{'Would close' if dry_run else 'Closed'} capacity escalation #{number}.{res}")
    return False, None, " ".join(notes)


def open_issues_titled(repo, title):
    """Numbers of EVERY open issue OUR BOT opened whose title is exactly
    `title` (search is fuzzy; the equality check is not; a human-opened issue
    with the same title is never closed or edited). Empty on a failed lookup."""
    r = gh(["issue", "list", "-R", repo, "--state", "open", "--search", f'"{title}" in:title',
            "--json", "number,title,author", "--limit", "50"])
    try:
        items = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except ValueError:
        items = []
    return [it["number"] for it in items if it.get("title") == title and untrusted.by_bot(it)]


def project_repos():
    with open(hub_path("config", "projects.json")) as f:
        return [p["repo"] for p in json.load(f)["projects"]]


def is_review_check(name):
    return name == "review" or name.endswith("/ review")


DISPATCH_LABELS = {"agent:implement", "needs-design"}
PARKED_LABELS = {"verdict:backlog", "verdict:decline", "P3"}


WHY_RE = re.compile(r"why\s+this\s+matters", re.I)
AFTER_RE = re.compile(r"After:\s*#(\d+)", re.I)
DEPENDENCY_RE = re.compile(r"depend", re.I)


def has_why(issue, comments=None):
    """A "Why this matters" assessment exists: in the body of a bot- or
    trusted-authored issue (otherwise anyone could make their issue
    dispatchable), or in one of OUR comments — the Product Owner posts it as a
    comment so the author's text stays theirs. ``comments`` (REST) when the
    caller has them; else the flag :func:`enrich_why` set."""
    if WHY_RE.search(issue.get("body") or "") and trusted_author(issue):
        return True
    if comments is not None:
        return why_commented(comments)
    return bool(issue.get("_why_comment"))


# The section itself ("**Why this matters:**"), not a mention of it such as the
# Engineer's "Blocked — missing Why this matters".
WHY_SECTION_RE = re.compile(r"why\s+this\s+matters\s*\**\s*:", re.I)


def why_commented(comments):
    return any(bot_said(c) and WHY_SECTION_RE.search(c.get("body") or "") for c in comments or [])


WHY_LOOKUP_LABELS = {"triaged:ready", "debt", "agent:implement"}


def enrich_why(repo, issues):
    """Mark ready tickets whose "Why this matters" is in one of our comments
    (one comments call per ready ticket lacking it in the body)."""
    for it in issues or []:
        if not label_names(it) & WHY_LOOKUP_LABELS or has_why(it):
            continue
        it["_why_comment"] = why_commented(gh_list(f"repos/{repo}/issues/{it['number']}/comments"))
    return issues


def after_dep(issue):
    """The "After: #N" dependency, only from a bot/trusted-authored body."""
    m = AFTER_RE.search(issue.get("body") or "")
    return int(m.group(1)) if m and trusted_author(issue) else None


def dep_satisfied(dep, facts):
    """An "After: #N" dependency is met once #N has an open or merged PR, or
    #N is itself closed — a research/report ticket delivered with `Refs #N`
    (not `Closes`) only ever clears through the closed issue."""
    return dep in facts["open_impl"] or dep in facts["merged_impl"] or dep in facts.get("closed_issues", ())


def closed_issue_numbers(repo, limit=300):
    items = gh_capped(["issue", "list", "-R", repo, "--state", "closed", "--json", "number"], limit)
    return {it["number"] for it in items or []}


def engineer_block(ctx, since, issue):
    """The Engineer's latest comment after `since`, unless it is a block that
    has since been resolved (it stopped for a missing "Why this matters" and
    the body now has one) — then the dispatch is simply pending again."""
    eng = [c for c in ctx["comments"] if bot_said(c, "[Engineer]")
           and (since is None or parse_ts(c["created_at"]) > since)]
    if not eng:
        return None
    last = eng[-1]
    if WHY_RE.search(last.get("body") or "") and has_why(issue, ctx["comments"]):
        return None
    return last


def dependency_ready(issue, ctx):
    """"After: #N" ticket the Engineer stopped on because #N had no PR yet —
    now #N has an open or merged PR, so re-dispatch it. Also lifts a
    needs-human that was only ever about that ordering (no human spoke since)."""
    dep = after_dep(issue)
    if dep is None:
        return None
    f = ctx["facts"]
    if not dep_satisfied(dep, f):
        return None
    labels = label_names(issue)
    if labels & {"agent:implement", "needs-design"} - ({"agent:implement"} if "needs-human" in labels else set()):
        return None
    comments = [c for c in ctx["comments"] if not is_sweeper(c) and not bot_said(c, "[Chief of Staff]")]
    if not comments:
        return None
    last = comments[-1]
    if not (bot_said(last, "[Engineer]") and DEPENDENCY_RE.search(last.get("body") or "")):
        return None  # someone (the approver) spoke after the block: theirs now
    pr = f["open_impl"].get(dep) or f["merged_impl"].get(dep)
    how = f"now has PR #{pr}" if pr else "is closed"
    return {"remove": labels & {"needs-human", "agent:implement"}, "add": ["agent:implement"], "close": None,
            "escalate": None,
            "reason": f"its dependency #{dep} {how}, so the ordering block is resolved — re-dispatched."}


DELIVERED_MARK = "Delivered — moved to review"
STATUS = {"status:in-progress", "status:in-review"}


def delivered_report(ctx):
    """Report ticket whose result the Engineer already posted and moved to
    review (prompts/implement.md REPORT path) — status:in-review with no PR is
    legitimate there: the approver closes it when accepted."""
    since = last_labeled_at(ctx["events"], "status:in-review")
    return any(bot_said(c, "[Engineer]", DELIVERED_MARK)
               and (since is None or parse_ts(c["created_at"]) >= since) for c in ctx["comments"])


PARKED = {"P3", "verdict:decline", "verdict:backlog"}


def lost_ready(issue, events):
    """A ticket that fell out of the dispatch queue: a bot removed
    triaged:ready while it held no other state label (so nothing took it
    over), it still holds none, and no state or parking label arrived since.
    -> {"at", "by"} of that removal, or None. Replays the label events, since
    the labels around the removal decide it."""
    labels = label_names(issue)
    state = set(label_states()["state_labels"])
    if labels & (state | PARKED):
        return None
    ev = sorted((e for e in events if e.get("event") in ("labeled", "unlabeled") and e.get("label")),
                key=lambda e: e["created_at"])
    held, lost = set(), None
    for i, e in enumerate(ev):
        name = e["label"]["name"]
        if e["event"] == "labeled":
            held.add(name)
            if name in state | PARKED:
                lost = None
            continue
        held.discard(name)
        if name != "triaged:ready":
            continue
        actor = (e.get("actor") or {}).get("login", "")
        actor_is_bot = untrusted.author_is_bot(e)
        # Labels changed in the same second are one edit (remove ready + add needs-design).
        same = {x["label"]["name"] for x in ev[i + 1:]
                if x["created_at"] == e["created_at"] and x["event"] == "labeled"}
        took_over = (held | same) & state
        # needs-human held right before the removal: the ticket was waiting on
        # the approver, not on dispatch (e.g. an unanswered legal question).
        was_approvers = "needs-human" in held or any(
            x["label"]["name"] == "needs-human" and x["event"] == "unlabeled"
            and x["created_at"] == e["created_at"] for x in ev)
        lost = (None if (took_over or was_approvers or not untrusted.is_automation_login(actor, is_bot=actor_is_bot))
                else {"at": e["created_at"], "by": actor})
    return lost


SHAPED_INTO_RE = re.compile(r"^Shaped into:\s*(.+)$", re.M)


def umbrella_done(issue, ctx):
    """A proposal the PO split into tickets ("Shaped into: #A, #B" in its
    [Product Owner] comment) stays proposal:pending as the umbrella. Once every
    ticket is closed it is done -> the closed ticket numbers, else None."""
    if "proposal:pending" not in label_names(issue):
        return None
    shaped = None
    for c in ctx["comments"]:
        body = c.get("body") or ""
        m = SHAPED_INTO_RE.search(body) if bot_said(c, "[Product Owner]") else None
        if m:
            shaped = [int(x) for x in re.findall(r"#(\d+)", m.group(1))]
    if not shaped or not set(shaped) <= ctx["facts"].get("closed_issues", set()):
        return None
    return shaped


def reconcile_issue(issue, ctx):
    """FACTS OVER LABELS. Derive where an open ticket really is from its PRs and
    comments, and return the label fix that makes the labels say so — or None
    when they already agree. A fix is {"remove": set, "add": [..], "close": PR
    number or None, "escalate": reason or None, "reason": str}.

    Facts (ctx["facts"]): impl PRs (Closes #N) open / merged / closed-unmerged,
    design PRs (Gate-for: #N) open / merged / closed-unmerged, and whether a new
    dispatch fits under the WIP limit. Added labels fire their router job
    (the sweeper uses the ops App token), so fixing the label IS starting the
    next step."""
    dep = dependency_ready(issue, ctx)
    if dep:
        return dep
    labels = label_names(issue)
    n = issue["number"]
    f = ctx["facts"]
    status = labels & STATUS

    def fix(reason, remove=(), add=(), close=None, escalate=None):
        remove = set(remove) & labels
        add = [a for a in add if a not in labels]
        if not (remove or add or close or escalate):
            return None
        return {"remove": remove, "add": add, "close": close, "escalate": escalate, "reason": reason}

    if n in f["merged_impl"]:
        pr = f["merged_impl"][n]
        return fix(f"PR #{pr}, which closes it, is merged — the ticket is done.",
                   remove=status | {"agent:implement", "triaged:ready"}, close=pr)
    if n in f["open_impl"]:
        return fix(f"PR #{f['open_impl'][n]} is open for it — it is in review.",
                   remove={"status:in-progress"}, add=["status:in-review"])
    if "needs-design" in labels:
        if n in f["gate_merged"]:
            nxt = "agent:implement" if f["wip_free"] else "triaged:ready"
            why = ("implementation dispatched." if f["wip_free"] else
                   "WIP limit reached, so it is queued as `triaged:ready` for the Chief of Staff to dispatch.")
            return fix(f"design PR #{f['gate_merged'][n]} is merged — the design gate is cleared; {why}",
                       remove=status | {"needs-design"}, add=[nxt])
        if n in f["gate_open"]:
            return fix(f"design PR #{f['gate_open'][n]} is open — the design is in review.",
                       remove={"status:in-progress"}, add=["status:in-review"])
        if n in f["gate_closed"]:
            return fix("", remove=status, escalate=(
                f"its design PR #{f['gate_closed'][n]} was closed without merging — "
                "re-design (re-apply `needs-design`) or drop it; that is your call."))
        return None
    if "agent:implement" in labels:
        closed = f["closed_impl"].get(n)
        since = last_labeled_at(ctx["events"], "agent:implement")
        if closed and (since is None or closed[1] > since):
            return fix("", remove=status, escalate=(
                f"its PR #{closed[0]} was closed without merging after dispatch — "
                "re-dispatch (re-apply `agent:implement`) or drop it; that is your call."))
        if "status:in-review" in labels and not delivered_report(ctx):
            return fix("no PR is open for it, so it is not in review.", remove={"status:in-review"})
        return None
    if "status:in-review" in labels and not delivered_report(ctx):
        return fix("no open PR and no delivered report — nothing of it is in review.", remove={"status:in-review"})
    done = umbrella_done(issue, ctx)
    if done:
        return fix("every ticket it was shaped into (" + ", ".join(f"#{d}" for d in done)
                   + ") is closed — the proposal is delivered.", remove={"proposal:pending"}, close="all shaped tickets closed")
    lost = lost_ready(issue, ctx["events"])
    if lost:
        return fix(f"`triaged:ready` was removed on {lost['at'][:10]} by {lost['by']} without dispatching it, "
                   "and nothing picked it up since — re-queued for dispatch.", add=["triaged:ready"])
    if "verdict:overridden" in labels and not labels & {"triaged:ready", "proposal:pending", "P3"}:
        # Approver overrode a park; po-continue normally dispatches in the same
        # run. Nothing dispatched and no PR -> queue it for CoS dispatch (the
        # deterministic form of CoS's old verdict:overridden safety net).
        return fix("the approver overrode the park but nothing was dispatched — queued for dispatch.",
                   add=["triaged:ready"])
    return None


_LABEL_STATES: dict = {}


def label_states(kind="issue"):
    if kind not in _LABEL_STATES:
        _LABEL_STATES[kind] = flow_reader.get_label_states(kind)
    return _LABEL_STATES[kind]


def ticket_state(issue):
    """-> (combo, modifiers) per config/flows.yml label_states.issue, or
    (None, modifiers) when the issue's state labels form an illegal set."""
    return flow_reader.resolve_state(label_names(issue), label_states())


def state_name(issue):
    combo, mods = ticket_state(issue)
    name = combo["state"].split(".", 1)[1] if combo else "ILLEGAL label set"
    return name + "".join(f" +{m}" for m in mods)


def normalize_issue(issue, ctx):
    """Contradictory label sets on an open issue -> (remove, reason[, add])
    to fix them, or ("escalate", reason) when which side is right is a
    approver call. None when consistent. Every state a fix lands in is then
    re-checked by classify_issue, which re-fires its trigger if the item is
    stuck there.

    Three layers, in order:
      1. config/flows.yml label_states.issue.repairs — declarative fixes.
      2. Fact-dependent fixes below, where which label wins depends on
         whether a PR exists — not expressible as a label rule.
      3. Anything still outside label_states.issue.combos is illegal:
         escalate rather than guess."""
    labels = label_names(issue)
    n = issue["number"]
    cfg = label_states()
    rep = flow_reader.match_repair(labels, cfg)
    if rep:
        if rep.get("escalate"):
            return "escalate", rep["reason"]
        remove = labels & set(rep.get("remove") or [])
        add = [l for l in rep.get("add") or [] if l not in labels]
        return (remove, rep["reason"], add) if add else (remove, rep["reason"])
    if {"agent:implement", "needs-design"} <= labels:
        if n in ctx["merged_gate"]:
            return {"needs-design"}, "its design PR already merged — the design gate is cleared, implementation stands."
        return {"agent:implement"}, "design comes first (`needs-design` gates implementation) — the design gate re-dispatches `agent:implement` once the design PR merges."
    if {"status:in-progress", "status:in-review"} <= labels:
        if n in ctx["open_pr_closes"]:
            return {"status:in-progress"}, "a PR is open for it, so it is in review, not in progress."
        return {"status:in-review"}, "no open PR for it, so it cannot be in review."
    combo, _ = flow_reader.resolve_state(labels, cfg)
    if combo is None:
        present = sorted(labels & set(cfg["state_labels"]))
        return "escalate", (f"its state labels ({', '.join(f'`{l}`' for l in present)}) match no state in "
                            "config/flows.yml label_states.issue — no flow produces this combination, so "
                            "it is the approver's call which label is wrong.")
    return None


def normalize_pr(pr, ctx):
    """normalize_issue's PR counterpart, on label_states.pr:
    config repairs, then the event-dependent fixes, then any set outside
    the combos (or with two risk:* labels left) escalates."""
    labels = label_names(pr)
    cfg = label_states("pr")
    rep = flow_reader.match_repair(labels, cfg)
    if rep:
        if rep.get("escalate"):
            return "escalate", rep["reason"]
        remove = labels & set(rep.get("remove") or [])
        add = [l for l in rep.get("add") or [] if l not in labels]
        return (remove, rep["reason"], add) if add else (remove, rep["reason"])
    if {"ready-to-merge", "review:changes-requested"} <= labels:
        ok = last_labeled_at(ctx["events"], "ready-to-merge")
        cr = last_labeled_at(ctx["events"], "review:changes-requested")
        if ok and cr and cr > ok:
            return ({"ready-to-merge", "needs-approver-merge"} & labels,
                    "the latest review verdict is REQUEST CHANGES — the earlier approval no longer holds.")
        return {"review:changes-requested"}, "the latest review verdict is APPROVE — the earlier change request is resolved."
    for group in flow_reader.exclusive_violations(labels, cfg):
        stamped = [(last_labeled_at(ctx["events"], l), l) for l in group]
        known = [t for t in stamped if t[0]]
        if known:
            keep = max(known)[1]
            return (set(group) - {keep},
                    f"several of {', '.join(f'`{l}`' for l in group)} — kept `{keep}`, the most recently applied.")
    combo, _ = flow_reader.resolve_state(labels, cfg)
    if combo is None:
        present = sorted(labels & (set(cfg["state_labels"]) | {l for g in cfg.get("exclusive") or [] for l in g}))
        return "escalate", (f"its labels ({', '.join(f'`{l}`' for l in present)}) match no state in "
                            "config/flows.yml label_states.pr — the approver's call which label is wrong.")
    return None


def closes(pr):
    """closingIssuesReferences (computed by GitHub) plus body keywords — the
    body regex only from a bot/trusted-authored PR."""
    refs = {r["number"] for r in pr.get("closingIssuesReferences") or []}
    if not trusted_author(pr):
        return refs
    return refs | {int(x) for x in CLOSES_RE.findall(pr.get("body") or "")}


def gates(pr):
    """"Gate-for: #N" design gates — only on PRs our automation authored
    (design PRs are the Architect agent's); a human typing it could clear a
    design gate."""
    if not untrusted.by_automation(pr):
        return set()
    return {int(x) for x in GATE_FOR_RE.findall(pr.get("body") or "")}


def wip_limit():
    """policies.wip_limit from squadhq.yml (rendered into config/projects.json),
    else flows.yml's wip_limit_open_prs_per_project flood guard."""
    try:
        with open(hub_path("config", "projects.json")) as f:
            v = ((json.load(f) or {}).get("policies") or {}).get("wip_limit")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    except (OSError, ValueError, AttributeError):
        pass
    return flow_reader.get_flood_guard("wip_limit_open_prs_per_project")["value"]


def open_pr_count(open_prs):
    return len([p for p in open_prs if not p.get("isDraft")])


def inflight_issues(issues, open_impl):
    """Dispatched tickets whose PR has not appeared yet — they hold a WIP slot
    (their PR is coming). needs-human holds no agent run, so no slot."""
    return [it for it in issues if "agent:implement" in label_names(it)
            and "needs-human" not in label_names(it) and it["number"] not in open_impl]


def _free_slots(open_count, inflight_count, limit):
    return max(0, limit - open_count - inflight_count)


def wip_slots(open_prs, issues, limit):
    """THE WIP rule — free dispatch slots = limit - open non-draft PRs -
    dispatched tickets with no PR yet. Every dispatch decision (pr_facts'
    wip_free, plan_dispatch, ticket_transition) goes through this."""
    open_impl = {n for p in open_prs for n in closes(p) - gates(p)}
    return _free_slots(open_pr_count(open_prs), len(inflight_issues(issues or [], open_impl)), limit)


def pr_facts(open_prs, closed_prs, limit, issues=None):
    """Issue number -> PR number maps for each fact reconcile_issue uses.
    Latest PR wins where several match; closed_impl carries (pr, closed_at).
    Pass the open `issues` so wip_free counts dispatched-without-PR tickets."""
    f = {"open_impl": {}, "merged_impl": {}, "closed_impl": {},
         "gate_open": {}, "gate_merged": {}, "gate_closed": {},
         "wip_free": wip_slots(open_prs, issues, limit) > 0,
         "open_pr_count": open_pr_count(open_prs)}
    for p in sorted(open_prs, key=lambda p: p["number"]):
        for n in gates(p):
            f["gate_open"][n] = p["number"]
        for n in closes(p) - gates(p):
            f["open_impl"][n] = p["number"]
    for p in sorted(closed_prs, key=lambda p: p["number"]):
        merged = bool(p.get("mergedAt"))
        for n in gates(p):
            (f["gate_merged"] if merged else f["gate_closed"])[n] = p["number"]
        for n in closes(p) - gates(p):
            if merged:
                f["merged_impl"][n] = p["number"]
            else:
                f["closed_impl"][n] = (p["number"], parse_ts(p["closedAt"]))
    # A newer open PR supersedes an older closed one for the same issue.
    for n in f["open_impl"]:
        f["closed_impl"].pop(n, None)
    for n in f["gate_open"]:
        f["gate_closed"].pop(n, None)
    for n in f["gate_merged"]:
        f["gate_closed"].pop(n, None)
    return f


def find_items(repo):
    """Yield (item, is_pr, ctx) for every open issue/PR not already handed to
    the approver (needs-human). Approver-wait states are filtered later, inside
    classify_*, so normalization still sees e.g. a stale needs-approver-merge."""
    issues = gh_capped(["issue", "list", "-R", repo, "--state", "open",
                        "--json", "number,title,body,labels,author,createdAt"], 200)
    prs = gh_capped(["pr", "list", "-R", repo, "--state", "open",
                     "--json", "number,title,body,labels,author,isDraft,headRefOid,closingIssuesReferences"], 100)
    closed = gh_capped(["pr", "list", "-R", repo, "--state", "closed",
                        "--json", "number,body,author,closingIssuesReferences,mergedAt,closedAt"], 100)
    for batch in (issues, prs, closed):
        enrich_trust(repo, batch)
    facts = pr_facts(prs, closed, wip_limit(), issues)
    facts["closed_issues"] = closed_issue_numbers(repo)
    open_pr_closes = set(facts["open_impl"])
    merged_gate = set(facts["gate_merged"])
    gate_prs = set(facts["gate_open"]) | merged_gate

    for issue in issues:
        n = issue["number"]
        if "needs-human" in label_names(issue) and after_dep(issue) is None:
            continue
        yield issue, False, {"approver_owned": "needs-human" in label_names(issue),"events": gh_list(f"repos/{repo}/issues/{n}/events"),
                             "comments": gh_list(f"repos/{repo}/issues/{n}/comments"),
                             "open_pr_closes": open_pr_closes, "gate_prs": gate_prs,
                             "merged_gate": merged_gate, "facts": facts}

    for pr in prs:
        n = pr["number"]
        if "needs-human" in label_names(pr) or pr.get("isDraft"):
            continue
        sha = pr["headRefOid"]
        commit = gh_json(["api", f"repos/{repo}/commits/{sha}"])
        runs = gh_json(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])["check_runs"]
        comments = gh_list(f"repos/{repo}/issues/{n}/comments")
        yield pr, True, {"events": gh_list(f"repos/{repo}/issues/{n}/events"),
                         "comments": comments,
                         "head_committed_at": parse_ts(commit["commit"]["committer"]["date"]),
                         "review_checks": [r for r in runs if is_review_check(r.get("name", ""))],
                         "checks_recovered_since": checks_recovered_since(repo, pr, comments)}


VERDICT_MARK_RE = re.compile(r"<!--\s*squadhq:verdict sha=([0-9a-f]{7,40})")


def verdict_recorded(comments, sha):
    """review-post recorded a verdict (our marker comment) for this head."""
    return any(untrusted.by_automation(c) and m.group(1).startswith(sha[:7]) and sha.startswith(m.group(1))
               for c in comments for m in [VERDICT_MARK_RE.search(c.get("body") or "")] if m)


SELF_MERGE_CHECKS_RE = re.compile(r"^\[Self-Merge\] This PR needs an approver to merge: failing checks:")


def checks_recovered_since(repo, pr, comments):
    """When self-merge sent this risk:low PR to an approver ONLY for failing
    checks and every check's latest run now passes: the time of that decision
    (the merge decision is re-run after the merge_decision_missing wait).
    None otherwise; costs one rollup read, only for such PRs."""
    labels = label_names(pr)
    if not {"needs-approver-merge", "ready-to-merge", "risk:low"} <= labels:
        return None
    ours = [c for c in comments if bot_said(c, "[Self-Merge]")]
    if not ours or not SELF_MERGE_CHECKS_RE.match(ours[-1].get("body") or ""):
        return None
    data = gh_json(["pr", "view", str(pr["number"]), "-R", repo, "--json", "statusCheckRollup"])
    from squadhq.merge_policy import latest_checks
    for c in latest_checks((data or {}).get("statusCheckRollup")):
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        if concl not in ("SUCCESS", "SKIPPED", "NEUTRAL"):
            return None  # still failing, or still running
    return parse_ts(ours[-1]["created_at"])


def as_fix(norm):
    """normalize_* result -> the fix dict reconcile_issue returns."""
    if not norm:
        return None
    remove, reason, *add = norm
    if remove == "escalate":
        return {"remove": set(), "add": [], "close": None, "escalate": reason, "reason": reason}
    return {"remove": set(remove), "add": list(add[0]) if add else [], "close": None,
            "escalate": None, "reason": reason}


def describe_fix(fx):
    parts = []
    if fx["remove"]:
        parts.append("remove " + ", ".join(sorted(fx["remove"])))
    if fx["add"]:
        parts.append("add " + ", ".join(fx["add"]))
    if fx["close"]:
        parts.append(f"close (PR #{fx['close']} merged)" if isinstance(fx["close"], int) else f"close ({fx['close']})")
    if fx["escalate"]:
        parts.append("escalate: " + fx["escalate"][:90])
    return "; ".join(parts)


def apply_fix(repo, item, is_pr, state, fx, label_cache):
    """Removes first, then each add on its own call — an add must fire its
    `labeled` event AFTER the labels it replaces are gone (e.g. implement's
    gate check would still see needs-design otherwise). Returns an error
    string or ""."""
    n = item["number"]
    kind = "pr" if is_pr else "issue"
    if fx["remove"]:
        args = [kind, "edit", str(n), "-R", repo]
        for l in sorted(fx["remove"]):
            args += ["--remove-label", l]
        r = gh(args)
        if r.returncode:
            return r.stderr.strip()
    for l in fx["add"]:
        if l in ("agent:shape", "agent:continue"):
            ensure_label(repo, l, label_cache)
        r = gh([kind, "edit", str(n), "-R", repo, "--add-label", l])
        if r.returncode:
            return r.stderr.strip()
    if fx["escalate"]:
        r = escalate(repo, item, state, is_pr, fx["escalate"])
        return r.stderr.strip() if r.returncode else ""
    changes = describe_fix(dict(fx, close=None)).replace("remove ", "removed ").replace("add ", "added ")
    text = f"State fix: {changes} — {fx['reason']}" if changes else fx["reason"]
    if "agent:implement" in fx["add"]:
        who = implementer_for(item, implementer.enabled_specialists())
        text += f"\n\nImplementer: {who}.\n<!-- squadhq:implementer={who} -->"
    if fx["close"]:
        r = gh(["issue", "close", str(n), "-R", repo, "--reason", "completed",
                "--comment", f"{BADGE}{text}\n\n<!-- stuck-sweeper state={state} -->"])
        return r.stderr.strip() if r.returncode else ""
    r = comment(repo, n, state, text)
    return r.stderr.strip() if r.returncode else ""


def clean_closed(repo, dry_run, since_days=7, cap=20):
    """Closed issues still carrying work-in-flight labels (status:*,
    agent:implement, needs-design, triaged:ready) — the board and every reader
    would still count them as active. Hygiene only: no comment."""
    since = (utcnow() - datetime.timedelta(days=since_days)).strftime("%Y-%m-%d")
    stale = {"status:in-progress", "status:in-review", "agent:implement", "needs-design", "triaged:ready"}
    items = gh_capped(["issue", "list", "-R", repo, "--state", "closed",
                       "--search", f"updated:>={since}", "--json", "number,labels"], 100)
    out = []
    for it in items:
        drop = label_names(it) & stale
        if not drop or len(out) >= cap:
            continue
        res = ""
        if not dry_run:
            args = ["issue", "edit", str(it["number"]), "-R", repo]
            for l in sorted(drop):
                args += ["--remove-label", l]
            res = failed(gh(args))
        out.append(f"| {repo}#{it['number']} | closed-hygiene | - | remove {', '.join(sorted(drop))}{res} |")
    return out




# Parked verdicts that close on their own: label -> (flows
# stuck_sweeper key, word for the comment, the approver's override reply).
PARK_EXPIRY = {
    "verdict:decline": ("decline_close_days", "declined", "do it anyway"),
    "verdict:backlog": ("backlog_close_days", "backlogged", "do-it-now"),
}


def park_expired(issue, events, comments, now, days, label="verdict:decline"):
    """A PO DECLINE/BACKLOG left alone for `days`: `label` is on, no
    verdict:overridden, and no approver comment since it was applied (a reply
    means the approver is still weighing it). -> the verdict time, or None."""
    labels = label_names(issue)
    if label not in labels or "verdict:overridden" in labels or not days:
        return None
    since = last_labeled_at(events, label)
    if since is None or now - since < datetime.timedelta(days=days):
        return None
    if any(is_human(c) and parse_ts(c["created_at"]) > since for c in comments):
        return None
    return since


def close_parked(repo, dry_run, now, guard, cap=10):
    """Close open issues the PO declined/backlogged longer ago than their
    PARK_EXPIRY window with no approver reply since. Not planned, labels kept,
    one comment saying how to revive."""
    out = []
    for label, (key, word, reply) in PARK_EXPIRY.items():
        days = guard.get(key, 0)
        if not days:
            continue
        items = gh_capped(["issue", "list", "-R", repo, "--state", "open", "--label", label,
                           "--json", "number,labels"], 50)
        for it in items:
            if len(out) >= cap:
                return out
            n = it["number"]
            since = park_expired(it, gh_list(f"repos/{repo}/issues/{n}/events"),
                                 gh_list(f"repos/{repo}/issues/{n}/comments"), now, days, label)
            if not since:
                continue
            act = f"close ({word}, no approver reply)"
            if not dry_run:
                r = gh(["issue", "close", str(n), "-R", repo, "--reason", "not planned", "--comment",
                        f"{BADGE}Closed: the Product Owner {word} this on {since:%Y-%m-%d} and there was no "
                        f"approver reply in {days} days (config/flows.yml stuck_sweeper.{key}). "
                        f"To do it anyway, reopen it and reply `{reply}`."])
                act += f" FAILED: {r.stderr.strip()[:120]}" if r.returncode else ""
            out.append(f"| {repo}#{n} | {word} | {(now - since).days}d | {act} |")
    return out


PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2}
BLOCKING = {"agent:implement", "needs-design", "needs-human", "status:in-progress", "status:in-review",
            "proposal:pending", "debt-proposal", "P3", "verdict:backlog", "verdict:decline",
            "awaiting-approval", "escalation", "channel:external"}


def block_reason(it, facts):
    """Why a ticket is not dispatchable, in approver-readable words; "" when
    it is. The single readiness rule for plan_dispatch and the idle alarm."""
    ls = label_names(it)
    if not ls & {"triaged:ready", "debt"}:
        return "not shaped (no `triaged:ready` / `debt`)"
    blocking = ls & BLOCKING
    if blocking:
        return "blocking label " + ", ".join(f"`{l}`" for l in sorted(blocking))
    if not has_why(it):
        return "no \"Why this matters\" section"
    dep = after_dep(it)
    if dep is not None and not dep_satisfied(dep, facts):
        return f"waits on #{dep} (After:) — still open, no open or merged PR closes it"
    return ""


def plan_dispatch(issues, facts, limit, max_per_run, debt_share, now):
    """DETERMINISTIC DISPATCH — never left to a model (an LLM-only dispatcher
    let ready tickets sit for days with WIP slots free).

    Candidates: open tickets that are READY — triaged:ready (PO-shaped) or
    debt (CoS-accepted) — with a "Why this matters" section, no blocking
    label, not P3, and any "After: #N" dependency already carrying an open or
    merged PR. Slots: WIP limit minus open non-draft PRs minus tickets already
    dispatched without a PR yet (their PR is coming). Order: P0 > P1 > P2
    (unprioritised = P2), then oldest first; debt is held back while in-flight
    debt is at or above its share and non-debt work is waiting.
    Returns [(issue, reason)]."""
    def labels(it):
        return {l["name"] for l in it.get("labels", [])}

    # In flight = dispatched, no PR yet, and not parked on the approver
    # (needs-human holds no agent run, so it must not hold a WIP slot).
    inflight = inflight_issues(issues, facts["open_impl"])
    open_prs = facts.get("open_pr_count", 0)
    slots = min(max_per_run, _free_slots(open_prs, len(inflight), limit))
    if not slots:
        return []

    cands = [it for it in issues if not block_reason(it, facts)]
    def rank(it):
        return (min([PRIORITY_RANK[l] for l in labels(it) if l in PRIORITY_RANK] or [2]), it["createdAt"])
    cands.sort(key=rank)
    debt_inflight = sum(1 for it in inflight if "debt" in labels(it))
    total_inflight = len(inflight) + open_prs
    out = []
    for it in cands:
        if len(out) >= slots:
            break
        is_debt = "debt" in labels(it)
        others_waiting = any("debt" not in labels(c) for c in cands if c not in [o for o, _ in out] and c is not it)
        if is_debt and others_waiting and total_inflight and debt_inflight / total_inflight >= debt_share:
            continue
        out.append((it, f"{'debt' if is_debt else 'ready'} ticket, priority "
                        f"{next((l for l in sorted(labels(it)) if l in PRIORITY_RANK), 'P2 (default)')}, "
                        f"WIP {open_prs + len(inflight) + len(out)}/{limit} before this dispatch"))
        total_inflight += 1
        debt_inflight += is_debt
    return out


def implementer_for(issue, specialists):
    """Exactly ONE implementer per dispatch (squadhq.policy.implementer):
    a specialist only when enabled and the ticket matches its route, else
    the Engineer. The router's implement job computes the same function, so
    the recorded choice and the job that runs can never disagree."""
    return implementer.choose(label_names(issue), issue.get("body"), specialists)


DEBT_INTAKE_MAX_PER_RUN = 5
DEBT_HOLD_LABELS = {"needs-human", "verdict:decline", "verdict:backlog", "P3"}


def plan_debt_intake(issues, closed_debt):
    """DETERMINISTIC DEBT INTAKE (the Chief of Staff's rule, in code like
    dispatch: left to the model, proposals sat unaccepted). A bot-written
    ``debt-proposal`` with a "Why this matters" section is accepted as ``debt``
    with a priority (severity:high/critical -> P0, an existing P0/P1/P2, else
    P2) unless it repeats an open or recently settled debt issue (same file or
    class anchors), which closes it as a duplicate. Oldest first; a proposal
    accepted here counts as open debt for the ones after it.
    -> [(issue, "accept", priority) | (issue, "duplicate", prior issue)]."""
    open_debt = [it for it in issues if "debt" in label_names(it)]
    out = []
    for it in sorted(issues, key=lambda i: i.get("createdAt") or ""):
        ls = label_names(it)
        if "debt-proposal" not in ls or ls & DEBT_HOLD_LABELS:
            continue
        if not untrusted.by_automation(it) or not WHY_RE.search(it.get("body") or ""):
            continue
        finding = {"title": it.get("title"), "body": it.get("body"), "labels": sorted(ls)}
        prior = debt_dedupe.prior_match(finding, open_debt + list(closed_debt or []))
        if prior is not None and prior.get("number") != it.get("number"):
            out.append((it, "duplicate", prior))
            continue
        prio = "P0" if ls & {"severity:high", "severity:critical"} else \
            next((p for p in ("P0", "P1", "P2") if p in ls), "P2")
        out.append((it, "accept", prio))
        open_debt.append(it)
        if len(out) >= DEBT_INTAKE_MAX_PER_RUN:
            break
    return out


def run_debt_intake(repo, issues, dry_run):
    since = (datetime.datetime.now(datetime.timezone.utc).date()
             - datetime.timedelta(days=debt_dedupe.WINDOW_DAYS)).isoformat()
    closed_debt = gh_capped(["issue", "list", "-R", repo, "--state", "closed", "--label", "debt",
                             "--search", f"closed:>={since}", "--json", "number,title,body,author"], 100)
    rows = []
    for it, action, detail in plan_debt_intake(issues, closed_debt):
        n = it["number"]
        if action == "accept":
            act = f"accepted as debt, {detail}"
            if not dry_run:
                r = gh(["issue", "edit", str(n), "-R", repo, "--remove-label", "debt-proposal",
                        "--add-label", f"debt,{detail}"])
                if r.returncode:
                    act += f" FAILED: {r.stderr.strip()[:120]}"
                else:
                    it["labels"] = [l for l in it.get("labels", []) if l.get("name") != "debt-proposal"] \
                        + [{"name": "debt"}, {"name": detail}]
                    comment(repo, n, "debt_intake", f"Accepted as debt ({detail}); the dispatcher schedules it "
                                                    "within the debt share of WIP.")
        else:
            act = f"duplicate of #{detail['number']}"
            if not dry_run:
                r = gh(["issue", "close", str(n), "-R", repo, "--reason", "not planned", "--comment",
                        f"{BADGE}Closed as a duplicate of #{detail['number']} (same files or classes)."])
                if r.returncode:
                    act += f" FAILED: {r.stderr.strip()[:120]}"
                else:
                    it["_closed"] = True
        rows.append(f"| {repo}#{n} | debt_intake | - | {act} |")
    return rows


def run_dispatch(repo, dry_run, now):
    guard = flow_reader.get_flood_guard("stuck_sweeper")
    issues = gh_capped(["issue", "list", "-R", repo, "--state", "open",
                        "--json", "number,title,body,labels,author,createdAt"], 200)
    prs = gh_capped(["pr", "list", "-R", repo, "--state", "open",
                     "--json", "number,body,author,isDraft,closingIssuesReferences"], 100)
    closed = gh_capped(["pr", "list", "-R", repo, "--state", "closed",
                        "--json", "number,body,author,closingIssuesReferences,mergedAt,closedAt"], 100)
    for batch in (issues, prs, closed):
        enrich_trust(repo, batch)
    enrich_why(repo, issues)
    intake = run_debt_intake(repo, issues, dry_run)
    issues = [it for it in issues if not it.get("_closed")]
    facts = pr_facts(prs, closed, wip_limit(), issues)
    facts["closed_issues"] = closed_issue_numbers(repo)
    share = float(str(flow_reader.get_flood_guard("debt_refactor_share_target")["value"]).strip("~%")) / 100
    plan = plan_dispatch(issues, facts, wip_limit(), guard["dispatch_max_per_run"], share, now)
    rows = list(intake)
    specialists = implementer.enabled_specialists() if plan else set()
    for it, reason in plan:
        who = implementer_for(it, specialists)
        act = f"dispatch (agent:implement -> {who})"
        if not dry_run:
            r = None
            if "triaged:ready" in label_names(it):
                r = remove_label(repo, it["number"], "triaged:ready")
            if r is None or r.returncode == 0:
                r = gh(["issue", "edit", str(it["number"]), "-R", repo, "--add-label", "agent:implement"])
            if r.returncode:
                act += f" FAILED: {r.stderr.strip()[:120]}"
            else:
                comment(repo, it["number"], "dispatch",
                        f"Dispatched to {who} — {reason}.\n\n<!-- squadhq:implementer={who} -->")
        rows.append(f"| {repo}#{it['number']} | dispatch | - | {act}: {reason} |")
    rows += idle_alarm(repo, issues, prs, closed, facts, plan, guard, dry_run, now)
    return rows


IDLE_TITLE = "Pipeline idle — no dispatchable work"


def idle_state(issues, prs, closed, facts, plan, idle_hours, now):
    """-> (idle, since) for one repo. Idle = nothing open, nothing in flight,
    nothing dispatched this run, and no PR closed or merged in idle_hours —
    the per-item sweep sees every ticket as a valid wait, so only the repo
    as a whole shows the stall."""
    if plan or [p for p in prs if not p.get("isDraft")]:
        return False, None
    if any("agent:implement" in label_names(it) and "needs-human" not in label_names(it) for it in issues):
        return False, None
    last = max((parse_ts(p["closedAt"]) for p in closed if p.get("closedAt")), default=None)
    if last and now - last < datetime.timedelta(hours=idle_hours):
        return False, None
    return True, last


def idle_body(issues, facts, since):
    ready = [it for it in issues if label_names(it) & {"triaged:ready", "debt"}]
    lines = [f"{BADGE}No open PRs, nothing in flight, and nothing the dispatcher can start"
             + (f" — last PR closed {since.strftime('%Y-%m-%d %H:%M')} UTC." if since else "."), ""]
    if ready:
        lines.append("Shaped tickets and why each is not dispatchable:")
        lines += [f"- #{it['number']} {it['title'][:80]} [{state_name(it)}] — "
                  f"{block_reason(it, facts) or 'dispatchable (next sweep)'}"
                  for it in sorted(ready, key=lambda i: i["number"])]
    else:
        lines.append("No `triaged:ready` or `debt` tickets at all — nothing is shaped.")
    waiting = {
        "proposal:pending": "waiting on the PO",
        "debt-proposal": "waiting on CoS debt intake",
        "needs-human": "waiting on the approver",
    }
    for label, what in waiting.items():
        ns = [f"#{it['number']}" for it in issues if label in label_names(it)
              and "escalation" not in label_names(it)]
        if ns:
            lines.append(f"- `{label}` ({what}): {', '.join(sorted(ns, key=lambda x: int(x[1:])))}")
    lines += ["", "Closes itself on the first sweep that finds work in flight.",
              "", "<!-- stuck-sweeper state=idle -->"]
    return "\n".join(lines)


def idle_alarm(repo, issues, prs, closed, facts, plan, guard, dry_run, now):
    """One escalation per idle repo, refreshed in place each sweep (edit,
    not comment: no notification churn), closed once work is in flight."""
    hours = guard.get("idle_alarm_hours")
    if not hours:
        return []
    idle, since = idle_state(issues, prs, closed, facts, plan, hours, now)
    # ANY open issue with the exact title is the alarm — never a second one.
    alarms = sorted((it for it in issues if it.get("title") == IDLE_TITLE and untrusted.by_bot(it)),
                    key=lambda i: i["number"])
    if not alarms and len(issues) >= 200:  # the list was capped: ask directly
        alarms = [{"number": n, "title": IDLE_TITLE} for n in open_issues_titled(repo, IDLE_TITLE)]
    if not idle:
        rows = []
        for a in alarms:
            res = "" if dry_run else failed(gh(["issue", "close", str(a["number"]), "-R", repo,
                                                 "--reason", "completed"]))
            rows.append(f"| {repo}#{a['number']} | idle | - | resolved — closed{res} |")
        return rows
    open_alarm = alarms[0] if alarms else None
    body = idle_body([it for it in issues if it.get("title") != IDLE_TITLE], facts, since)
    if open_alarm:
        res = "" if dry_run else failed(gh(["issue", "edit", str(open_alarm["number"]), "-R", repo,
                                             "--body", body]))
        return [f"| {repo}#{open_alarm['number']} | idle | - | refreshed{res} |"]
    act = "opened escalation"
    if not dry_run:
        r = gh(["issue", "create", "-R", repo, "--title", IDLE_TITLE, "--body", body, "--label", "escalation"])
        if r.returncode:
            act += f" FAILED: {r.stderr.strip()[:120]}"
    return [f"| {repo} | idle | - | {act} |"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", help="limit to one project repo (org/name)")
    args = ap.parse_args(argv)

    guard = flow_reader.get_flood_guard("stuck_sweeper")
    th = guard["thresholds_minutes"]
    now = utcnow()
    window_start = now - datetime.timedelta(hours=guard["refire_window_hours"])
    budget = guard["actions_per_run_max"]
    rows, label_cache = [], set()

    try:
        hub_repo = settings.hub_repo()
    except RuntimeError as e:
        print(f"stuck_sweeper: {e}", file=sys.stderr)
        return 2
    del CAP_WARNINGS[:]
    paused, until, note = capacity_check(hub_repo, now, args.dry_run)
    if note:
        rows.append(note)
    if not paused:
        for repo in ([args.repo] if args.repo else project_repos()):
            try:
                rows += clean_closed(repo, args.dry_run)
                rows += close_parked(repo, args.dry_run, now, guard)
                for item, is_pr, ctx in find_items(repo):
                    ref = f"{repo}#{item['number']}"
                    # 1. RECONCILE: facts (PRs, design PRs, delivered reports)
                    #    over labels. 2. NORMALIZE: label sets that contradict
                    #    themselves. Both before step 3, so it judges the item by
                    #    its real state, not a stale or mixed one.
                    stop = False
                    if ctx.get("approver_owned"):
                        # needs-human: the approver's — except an ordering block
                        # the Engineer raised that has since resolved itself.
                        passes = (("reconcile", dependency_ready(item, ctx)),)
                    else:
                        passes = (("reconcile", None if is_pr else reconcile_issue(item, ctx)),
                                  ("normalize", as_fix((normalize_pr if is_pr else normalize_issue)(item, ctx))))
                    for state, fx in passes:

                        if not fx:
                            continue
                        done, escalated = attempts(ctx["comments"], state, window_start)
                        if escalated:
                            rows.append(f"| {ref} | {state} | - | already escalated, waiting on approver |")
                            stop = True
                            break
                        if budget <= 0:
                            rows.append(f"| {ref} | {state} | - | deferred (action cap reached) |")
                            stop = True
                            break
                        if not fx["escalate"] and done >= guard["refire_attempts_max"]:
                            fx = dict(fx, add=[], close=None, escalate=(
                                f"its labels were corrected {done} times in {guard['refire_window_hours']}h and "
                                f"keep coming back ({fx['reason']}) — something keeps re-applying them."))
                        act = describe_fix(fx)
                        if not args.dry_run:
                            err = apply_fix(repo, item, is_pr, state, fx, label_cache)
                            act += f" FAILED: {err[:120]}" if err else ""
                        budget -= 1
                        rows.append(f"| {ref} | {state} | - | {act} |")
                        # An added label IS the next step's trigger (its router
                        # job starts on the labeled event) — judging the item
                        # again this run would read the pre-fix history.
                        if fx["escalate"] or fx["close"] or fx["add"]:
                            stop = True
                            break
                        gone = fx["remove"]
                        item = dict(item, labels=[l for l in item["labels"] if l["name"] not in gone]
                                    + [{"name": l} for l in fx["add"]])
                    if stop or ctx.get("approver_owned"):
                        continue

                    # 2. RE-FIRE: the item's (now consistent) state is waiting on
                    #    automation past its threshold -> fire the trigger again.
                    hit = (classify_pr if is_pr else classify_issue)(item, ctx, now, th)
                    if not hit:
                        continue
                    state, age = hit
                    done, escalated = attempts(ctx["comments"], state, window_start)
                    if escalated:
                        rows.append(f"| {ref} | {state} | {age:.0f}m | already escalated, waiting on approver |")
                        continue
                    if budget <= 0:
                        rows.append(f"| {ref} | {state} | {age:.0f}m | deferred (action cap reached) |")
                        continue
                    if state == "mention_unanswered":
                        # Only a human comment can start the mention job: ask,
                        # once per mention (classify skips it after this).
                        trig = settings.trigger_phrase()
                        act = f"asked approver to repeat {trig}"
                        if not args.dry_run:
                            r = comment(repo, item["number"], state,
                                        f"Your `{trig}` request above got no reply — most likely the run was "
                                        "skipped by a capacity pause or failed. A bot can't restart it: "
                                        f"reply with `{trig}` again to retry.")
                            act += f" FAILED: {r.stderr.strip()[:120]}" if r.returncode else ""
                        budget -= 1
                        rows.append(f"| {ref} | {state} | {age:.0f}m | {act} |")
                        continue
                    if state in NO_REFIRE:
                        act, reason = "escalate", NO_REFIRE[state]
                    elif done >= guard["refire_attempts_max"]:
                        act, reason = "escalate", f"{done} automatic re-fires in the last {guard['refire_window_hours']}h did not unstick it."
                    else:
                        act, reason = f"refire {done + 1}/{guard['refire_attempts_max']}", ""
                    if not args.dry_run:
                        r = (escalate(repo, item, state, is_pr, reason) if act == "escalate"
                             else refire(repo, item, state, is_pr, ctx, label_cache))
                        if r.returncode != 0:
                            act += f" FAILED: {r.stderr.strip()[:120]}"
                    budget -= 1
                    rows.append(f"| {ref} | {state} | {age:.0f}m | {act} |")
                # 4. DISPATCH ready work into free WIP slots — after the
                #    per-item pass, so it sees reconciled labels.
                rows += run_dispatch(repo, args.dry_run, now)
            except Exception as e:  # one broken repo must not stop the sweep
                rows.append(f"| {repo} | error | - | {str(e)[:160]} |")

    head = f"## Stuck sweeper{' (dry run)' if args.dry_run else ''}\n\n"
    table = [r for r in rows if r.startswith("|")]
    body = head + ("\n".join(r for r in rows if not r.startswith("|")) + "\n" if len(table) != len(rows) else "")
    body += ("| item | state | age | action |\n|---|---|---|---|\n" + "\n".join(table)) if table else "Nothing stuck."
    if CAP_WARNINGS:
        body += "\n\n" + "\n".join(f"> ⚠ {w}" for w in CAP_WARNINGS)
    print(body)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(body + "\n")
    # A repository the sweep could not read is a failed run (and escalates), not a green one.
    return 1 if any(r.startswith("|") and r.split("|")[2].strip() == "error" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
