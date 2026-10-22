#!/usr/bin/env python3
"""
Ticket transitions on a merge — the EVENT path of the ticket state machine.

  python -m squadhq.ticket_transition --repo org/name --before SHA --after SHA [--dry-run]

Run by router.yml's ticket-transition job on every push to a project's default
branch. Finds the PR(s) that push merged and moves ONLY the tickets they touch,
straight away:

  merged PR                     affected tickets        transition
  ----------------------------  ----------------------  --------------------------------
  implementation (Closes #N)    #N                      done: work labels off, closed if
                                                        GitHub did not close it
                                "After: #N" tickets     dependency met: re-dispatched if
                                                        the Engineer was blocked on it
  design (Gate-for: #N)         #N                      gate cleared: agent:implement
                                                        (or triaged:ready at WIP limit)
  any                           freed WIP slot          next ready ticket dispatched

  --closed-pr N: a PR CLOSED WITHOUT MERGING (the work was rejected). Its
  tickets (Closes / Gate-for) go to needs-human with the reason — re-dispatch
  or drop is the approver's call — and the freed WIP slot is filled.

The rules are NOT restated here: every decision is squadhq.stuck_sweeper's
reconcile_issue / dependency_ready / plan_dispatch, the same functions the
hourly sweep uses — so the event path and the backstop can never disagree.
The sweep still runs for anything an event missed (GitHub drops events and
delays cron runs).

WIP is decided by stuck_sweeper.wip_slots() — the same function the sweep
uses. Because both dispatch from their own snapshot, this job and the
stuck-sweeper workflow MUST run in one shared concurrency group per project
repo (see squadhq.stuck_sweeper's docstring).

Required env: GH_TOKEN (ops App token, so added labels fire their router
jobs), SQUADHQ_HUB_REPO / HUB_REPO (for the capacity breaker; without it the
breaker is not consulted).
"""
import argparse
import os
import sys

from squadhq import capacity_gate, settings
from squadhq import stuck_sweeper as sw

WORK_LABELS = {"status:in-progress", "status:in-review", "agent:implement", "needs-design", "triaged:ready"}


def merged_prs(repo, before, after):
    """PRs merged by this push: every commit in before..after, mapped to its
    PR (squash, rebase and merge-commit merges all resolve this way)."""
    shas = []
    if before and set(before) != {"0"}:
        try:
            shas = [c["sha"] for c in sw.gh_json(["api", f"repos/{repo}/compare/{before}...{after}"]).get("commits", [])]
        except RuntimeError:
            shas = []
    numbers = set()
    for sha in (shas or [after])[-50:]:
        for p in sw.gh_json(["api", f"repos/{repo}/commits/{sha}/pulls"]) or []:
            if p.get("merged_at"):
                numbers.add(p["number"])
    # GraphQL-backed view: closingIssuesReferences is what "Closes #N" linked.
    return [sw.gh_json(["pr", "view", str(n), "-R", repo, "--json", "number,title,body,author,closingIssuesReferences"])
            for n in sorted(numbers)]


def affected(prs, open_issues):
    """-> ({issue number: triggering PR number}, set of implemented numbers)."""
    hit, implemented = {}, set()
    for pr in prs:
        gates = sw.gates(pr)
        impl = sw.closes(pr) - gates
        implemented |= impl
        for n in impl | gates:
            hit.setdefault(n, pr["number"])
    for it in open_issues:
        dep = sw.after_dep(it)  # only from a bot/trusted-authored body
        if dep is not None and dep in implemented:
            hit.setdefault(it["number"], hit[dep])
    return hit, implemented


def hub_repo():
    try:
        return settings.hub_repo()
    except RuntimeError:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--before", default="")
    ap.add_argument("--after", default="")
    ap.add_argument("--closed-pr", type=int, help="a PR closed without merging")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    del sw.CAP_WARNINGS[:]
    repo, now = args.repo, sw.utcnow()
    rows = []

    if args.closed_pr:
        prs = [sw.gh_json(["pr", "view", str(args.closed_pr), "-R", repo,
                           "--json", "number,title,body,author,closingIssuesReferences"])]
        trigger = f"PR #{args.closed_pr} closed without merging"
    else:
        prs = merged_prs(repo, args.before, args.after)
        trigger = "merged"
    if prs:
        issues = sw.gh_capped(["issue", "list", "-R", repo, "--state", "open",
                               "--json", "number,title,body,labels,author,createdAt"], 200)
        sw.enrich_trust(repo, issues)
        sw.enrich_trust(repo, prs)
        by_number = {it["number"]: it for it in issues}
        hit, implemented = affected(prs, issues)
        if args.closed_pr:
            # Nothing was implemented: "After:" dependents are NOT unblocked.
            hit = {n: p for n, p in hit.items() if n in sw.closes(prs[0]) | sw.gates(prs[0])}
        open_prs = sw.gh_capped(["pr", "list", "-R", repo, "--state", "open",
                                 "--json", "number,body,author,isDraft,closingIssuesReferences"], 100)
        closed_prs = sw.gh_capped(["pr", "list", "-R", repo, "--state", "closed",
                                   "--json", "number,body,author,closingIssuesReferences,mergedAt,closedAt"], 100)
        sw.enrich_trust(repo, open_prs)
        sw.enrich_trust(repo, closed_prs)
        facts = sw.pr_facts(open_prs, closed_prs, sw.wip_limit(), issues)
        facts["closed_issues"] = sw.closed_issue_numbers(repo)
        label_cache = set()
        for n, pr in sorted(hit.items()):
            it = by_number.get(n)
            if it is None:
                # Already closed (GitHub's "Closes #N"): only strip leftover work labels.
                closed = sw.gh_json(["issue", "view", str(n), "-R", repo, "--json", "number,state,labels"])
                drop = sw.label_names(closed) & WORK_LABELS
                if closed.get("state") == "CLOSED" and drop:
                    res = ""
                    if not args.dry_run:
                        edit = ["issue", "edit", str(n), "-R", repo]
                        for l in sorted(drop):
                            edit += ["--remove-label", l]
                        res = sw.failed(sw.gh(edit))
                    rows.append(f"| #{n} | PR #{pr} | closed — removed {', '.join(sorted(drop))}{res} |")
                continue
            ctx = {"events": sw.gh_list(f"repos/{repo}/issues/{n}/events"),
                   "comments": sw.gh_list(f"repos/{repo}/issues/{n}/comments"), "facts": facts}
            fx = sw.dependency_ready(it, ctx) if "needs-human" in sw.label_names(it) else sw.reconcile_issue(it, ctx)
            if not fx:
                rows.append(f"| #{n} | PR #{pr} | already consistent |")
                continue
            verb = "closed without merging" if args.closed_pr else "merged"
            fx = dict(fx, reason=f"PR #{pr} {verb}: {fx['reason']}" if fx["reason"] else fx["reason"])
            act = sw.describe_fix(fx)
            if not args.dry_run:
                err = sw.apply_fix(repo, it, False, "transition", fx, label_cache)
                act += f" FAILED: {err[:120]}" if err else ""
            rows.append(f"| #{n} | PR #{pr} | {act} |")

    # A merge frees a WIP slot whatever it touched — fill it now, unless the
    # capacity breaker is open (the dispatched agent would only skip).
    hub = hub_repo()
    cap, _ = capacity_gate.read_hub_json(hub, capacity_gate.CAPACITY_PATH, capacity_gate.DEFAULT_CAPACITY) \
        if hub else ({}, None)
    until = (cap or {}).get("paused_until") or ""
    until_ts = sw.safe_ts(until)
    if until and (until_ts is None or until_ts > now):
        rows.append(f"| - | - | dispatch skipped: capacity paused until {until} |")
    else:
        rows += [r.replace(f"| {repo}#", "| #").replace("| dispatch | - |", "| freed WIP slot |")
                 for r in sw.run_dispatch(repo, args.dry_run, now)]

    merged = ", ".join(f"#{p['number']}" for p in prs) or "none (direct push)"
    head = trigger if args.closed_pr else f"Merged PRs in this push: {merged}"
    body = [f"## Ticket transitions{' (dry run)' if args.dry_run else ''} — {repo}", "", head, ""]
    body += ["| ticket | trigger | transition |", "|---|---|---|", *rows] if rows else ["Nothing to move."]
    if sw.CAP_WARNINGS:
        body += [""] + [f"> ⚠ {w}" for w in sw.CAP_WARNINGS]
    text = "\n".join(body)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
