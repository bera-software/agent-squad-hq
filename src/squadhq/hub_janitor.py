#!/usr/bin/env python3
"""
Hub janitor: keeps the HUB repo's own open-issue list to what is still useful.

  python -m squadhq.hub_janitor [--dry-run] [--scope hub|projects]

Deterministic, no LLM. Rules live in config/flows.yml flood-guards.hub_janitor:

  1. PROTECT   never touched — protected titles (capacity escalation,
               quarterly review), protected labels, human-authored issues,
               issues a human (OWNER/MEMBER/COLLABORATOR) commented on,
               pinned issues.
  2. MERGE     identical titles (typically the same failure escalated once per
               run): the newest stays open and gets one comment listing every
               duplicate with its run link; the others close as "Duplicate of
               #N" (not planned).
  3. RETIRE    dated reports per series (Daily Brief, Growth nightly, SEO
               weekly, ...) older than the series' retain_days close as
               "Superseded by #N" (completed), N = the series' newest issue.
               The newest issue of every series is always kept, whatever its
               age. Hub series retain 0 days (newest only): the digests read
               history from agent_runner's hq_recent_reports, open or closed.

  3b. EXPIRE  point-in-time issues (run-failure escalations, legacy hub
               drafts) older than their expire rule's retain_days close as
               not planned. Hub only.

  4. PROJECTS  department reports live in project repos.
               The same PROTECT + RETIRE rules run over each registered
               project's open issues carrying project_reports.label only, with
               one flat project_reports.retain_days and no title merging.

Anything no rule matches is left alone. plan() and project_cfg() are pure
(tested in tests/test_hub_janitor.py); main() only fetches and applies.

Required env: GH_TOKEN, SQUADHQ_HUB_REPO (legacy HQ_REPO; falls back to the
current repo via `gh repo view`).
"""
import argparse
import datetime
import json
import os
import re
import sys

from squadhq import flow_reader, github, settings, untrusted
from squadhq.paths import hub_path

BADGE = "[Hub Janitor] "
RUN_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+")


def parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def protected_reason(issue, cfg):
    p = cfg["protect"]
    title = issue.get("title") or ""
    if any(re.search(rx, title) for rx in p.get("titles", [])):
        return "protected title"
    labels = {l["name"] for l in issue.get("labels", [])}
    if labels & set(p.get("labels", [])):
        return "protected label"
    # The janitor only ever closes what OUR bot opened. Anything else — a
    # human's issue, or another app's — is protected whatever its title says
    # (a look-alike title must never get someone's issue closed). This holds
    # even if protect.human_authored is switched off.
    if not untrusted.by_automation(issue):  # ops- or agent-opened reports only
        return "human-authored" if not (issue.get("author") or {}).get("is_bot") else "not opened by our bot"
    if p.get("human_engaged") and any(untrusted.by_trusted_human(c) for c in issue.get("comments", [])):
        return "approver commented"
    if p.get("pinned") and issue.get("isPinned"):
        return "pinned"
    return None


def run_link(issue):
    m = RUN_URL_RE.search(issue.get("body") or "")
    return m.group(0) if m else None


def plan(issues, now, cfg):
    """-> (actions, kept) where actions are dicts:
         {"kind": "merge", "keep": N, "close": [issues...]}
         {"kind": "retire", "close": issue, "superseded_by": N, "series": key}
       kept: [(issue, reason)] for protected issues (reported, not acted on)."""
    actions, kept, handled = [], [], set()
    live = []
    for it in issues:
        why = protected_reason(it, cfg)
        if why:
            kept.append((it, why))
        else:
            live.append(it)

    if cfg.get("merge_identical_titles"):
        by_title = {}
        for it in live:
            by_title.setdefault(it["title"].strip(), []).append(it)
        for group in by_title.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda i: parse_ts(i["createdAt"]), reverse=True)
            keep, dups = group[0], group[1:]
            actions.append({"kind": "merge", "keep": keep["number"], "close": dups})
            handled |= {d["number"] for d in dups}

    series = [(s["key"], re.compile(s["title"]), s["retain_days"]) for s in cfg["series"]]
    members = {}
    for it in live:
        if it["number"] in handled:
            continue
        for key, rx, days in series:
            if rx.search(it["title"]):
                members.setdefault(key, (days, []))[1].append(it)
                break
    for key, (days, group) in members.items():
        group.sort(key=lambda i: parse_ts(i["createdAt"]), reverse=True)
        newest, cutoff = group[0], now - datetime.timedelta(days=days)
        for it in group[1:]:
            if parse_ts(it["createdAt"]) < cutoff:
                actions.append({"kind": "retire", "close": it, "superseded_by": newest["number"], "series": key})
                handled.add(it["number"])

    for rule in cfg.get("expire") or []:
        rx, cutoff = re.compile(rule["title"]), now - datetime.timedelta(days=rule["retain_days"])
        for it in live:
            if it["number"] in handled or not rx.search(it["title"]):
                continue
            if parse_ts(it["createdAt"]) < cutoff:
                actions.append({"kind": "expire", "close": it, "rule": rule["key"], "reason": rule["reason"]})
                handled.add(it["number"])
    return actions, kept


def project_cfg(cfg):
    """The hub config narrowed for a project repo: same series and protect
    rules, every series retained project_reports.retain_days, no merging."""
    days = cfg["project_reports"]["retain_days"]
    return dict(cfg, merge_identical_titles=False, expire=[],
                series=[dict(s_, retain_days=days) for s_ in cfg["series"]])


def only_reports(issues, label):
    return [it for it in issues if label in {l["name"] for l in it.get("labels", [])}]


# ---------------------------------------------------------------- apply

def gh(args):
    return github.client().run(args)


def close(repo, n, reason, text):
    if text is None:
        # Project repos: no comment — an issue_comment event would fire the
        # project's agents stub (router.yml) once per closed report.
        return gh(["issue", "close", str(n), "-R", repo, "--reason", reason])
    return gh(["issue", "close", str(n), "-R", repo, "--reason", reason, "--comment", BADGE + text])


def apply_merge(repo, a):
    lines = []
    for d in a["close"]:
        link = run_link(d)
        lines.append(f"- #{d['number']} ({d['createdAt'][:10]}){' — ' + link if link else ''}")
    r = gh(["issue", "comment", str(a["keep"]), "-R", repo, "--body",
            f"{BADGE}Merged {len(a['close'])} duplicate issue(s) with this exact title into this one:\n\n"
            + "\n".join(lines)])
    results = [r]
    for d in a["close"]:
        results.append(close(repo, d["number"], "not planned", f"Duplicate of #{a['keep']}"))
    return results


ISSUE_JSON = "number,title,body,labels,author,createdAt,comments,isPinned"


def clean(repo, issues, cfg, dry_run, budget, comment=True):
    """Plan + apply for one repo. -> (text, budget_left)."""
    actions, kept = plan(issues, datetime.datetime.now(datetime.timezone.utc), cfg)
    rows = []
    for a in actions:
        n_close = len(a["close"]) if a["kind"] == "merge" else 1
        if budget < n_close:
            rows.append(f"| {a['kind']} | deferred (action cap) | |")
            continue
        budget -= n_close
        if a["kind"] == "merge":
            nums = ", ".join(f"#{d['number']}" for d in a["close"])
            res = "" if dry_run else "".join(
                f" FAILED: {x.stderr.strip()[:80]}" for x in apply_merge(repo, a) if x.returncode)
            rows.append(f"| merge | {nums} → #{a['keep']} | {a['close'][0]['title'][:70]}{res} |")
        elif a["kind"] == "expire":
            it = a["close"]
            res = ""
            if not dry_run:
                x = close(repo, it["number"], "not planned",
                          f"Expired — {a['reason']} (config/flows.yml flood-guards.hub_janitor.expire "
                          f"`{a['rule']}`). Closed, not deleted: it stays searchable." if comment else None)
                res = f" FAILED: {x.stderr.strip()[:80]}" if x.returncode else ""
            rows.append(f"| expire | #{it['number']} ({a['rule']}) | {it['title'][:70]}{res} |")
        else:
            it = a["close"]
            res = ""
            if not dry_run:
                x = close(repo, it["number"], "completed",
                          f"Superseded by #{a['superseded_by']} — older than the `{a['series']}` retention "
                          "window (config/flows.yml flood-guards.hub_janitor). Closed, not deleted: it stays searchable."
                          if comment else None)
                res = f" FAILED: {x.stderr.strip()[:80]}" if x.returncode else ""
            rows.append(f"| retire | #{it['number']} → #{a['superseded_by']} | {it['title'][:70]}{res} |")

    closes = sum(len(a["close"]) if a["kind"] == "merge" else 1 for a in actions)
    body = [f"## Hub janitor{' (dry run)' if dry_run else ''} — {repo}", "",
            f"{len(issues)} open · {closes} to close · {len(kept)} protected · "
            f"{len(issues) - closes} remain", ""]
    if rows:
        body += ["| action | issues | title |", "|---|---|---|", *rows, ""]
    else:
        body += ["Nothing to clean.", ""]
    if kept:
        body += ["<details><summary>Protected (never touched)</summary>", ""]
        body += [f"- #{it['number']} {it['title'][:70]} — {why}" for it, why in kept]
        body += ["", "</details>"]
    return "\n".join(body), budget


LIST_LIMIT = 500


def list_issues(repo, extra=()):
    r = gh(["issue", "list", "-R", repo, "--state", "open", "--limit", str(LIST_LIMIT), *extra, "--json", ISSUE_JSON])
    if r.returncode != 0:
        print(f"could not list issues for {repo}: {r.stderr.strip()}", file=sys.stderr)
        return None
    items = json.loads(r.stdout or "[]")
    if len(items) >= LIST_LIMIT:
        print(f"warning: {repo}: hit the {LIST_LIMIT}-issue cap — older issues not seen this run", file=sys.stderr)
    return items


def resolve_hub_repo():
    try:
        return settings.hub_repo()
    except RuntimeError:
        return gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scope", choices=("hub", "projects"), default="hub",
                    help="hub: the hub's own issues; projects: report issues in every registered project")
    args = ap.parse_args(argv)
    repo = resolve_hub_repo()
    cfg = flow_reader.get_flood_guard("hub_janitor")

    budget, texts = cfg["actions_per_run_max"], []
    if args.scope == "hub":
        issues = list_issues(repo)
        if issues is None:
            return 1
        text, budget = clean(repo, issues, cfg, args.dry_run, budget)
        texts.append(text)

    pcfg = cfg.get("project_reports")
    if args.scope == "projects" and pcfg:
        projects = json.load(open(hub_path("config", "projects.json")))["projects"]
        narrowed = project_cfg(cfg)
        for p in projects:
            found = list_issues(p["repo"], ("--label", pcfg["label"]))
            if found is None:
                texts.append(f"## Hub janitor — {p['repo']}\n\nSkipped: could not list issues.\n")
                continue
            text, budget = clean(p["repo"], only_reports(found, pcfg["label"]), narrowed, args.dry_run, budget,
                                 comment=False)
            texts.append(text)

    text = "\n".join(texts)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
