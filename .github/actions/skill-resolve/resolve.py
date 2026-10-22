#!/usr/bin/env python3
"""Match an event to an enabled skill, or render a skill's prompt.

    resolve.py match  --event label|command|schedule|skill --value V [--project owner/name]
    resolve.py render --skill ID [--repo owner/name] [--number N] [--event E] [--project owner/name]

Both write GitHub step outputs. Enablement comes from
``python -P -m squadhq.skills list --enabled-for squadhq.yml`` when the hub has a
squadhq.yml, otherwise from each skill's default_enabled. Legacy skills
(product-owner, architect, engineer, reviewer, review-fix) and deterministic
skills are run by their own jobs and never matched here.
"""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile

HOME = os.environ.get("SQUADHQ_HOME", ".")
# Labels the router's dedicated jobs own; a skill never takes these over.
RESERVED_LABELS = {"agent:implement", "needs-design", "agent:shape", "agent:continue", "review:changes-requested"}


def out(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for k, v in kv.items():
        v = "" if v is None else str(v)
        if "\n" in v:
            d = "EOF_" + secrets.token_hex(8)
            lines.append(f"{k}<<{d}\n{v}\n{d}")
        else:
            lines.append(f"{k}={v}")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def note(msg):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    print(msg, file=sys.stderr)


def catalogue(project):
    cmd = [sys.executable, "-P", "-m", "squadhq.skills", "list", "--format", "json"]
    cfg = os.path.join(HOME, "squadhq.yml")
    if os.path.exists(cfg):
        cmd += ["--enabled-for", cfg]
        if project:
            cmd += ["--project", project]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        note(f"skills list failed: {r.stderr.strip()[:300]}")
        return []
    return json.loads(r.stdout)


def disabled_by_var(skill_id):
    raw = os.environ.get("SQUADHQ_DISABLED_AGENTS", "")
    return skill_id in {x.strip() for x in raw.split(",") if x.strip()}


def command_of(body, trigger):
    pattern = r"(?:" + "|".join(re.escape(t) for t in {trigger, "@squad", "@claude"}) + r")\s+([a-z0-9][a-z0-9-]*)"
    m = re.search(pattern, body or "", re.I)
    return m.group(1).lower() if m else ""


def model_for(tier):
    try:
        data = json.load(open(os.path.join(HOME, "config", "models.json")))
        tiers = data.get("tiers") or data
        return (tiers.get(tier) or {}).get("primary", "")
    except (OSError, ValueError, AttributeError):
        return ""


def match(args):
    skills = catalogue(args.project)
    value = args.value or ""
    if args.event == "command":
        value = command_of(value, os.environ.get("SQUADHQ_TRIGGER") or "@squad")
        wanted = f"command @squad {value}" if value else None
    elif args.event == "label":
        wanted = None if value in RESERVED_LABELS else f"label {value}"
    elif args.event == "schedule":
        wanted = f"schedule {value}"
    else:
        wanted = None
    found = []
    for s in skills:
        if s.get("legacy") or s.get("deterministic"):
            continue
        if args.event == "skill":
            hit = s["id"] == value
        else:
            hit = bool(wanted) and any(t == wanted or t.startswith(wanted + " (") for t in s.get("triggers", []))
        if hit:
            found.append(s)
    enabled = [s for s in found if s.get("enabled", s.get("default_enabled")) and not disabled_by_var(s["id"])]
    for s in found:
        if s not in enabled:
            note(f"skill `{s['id']}` matches but is disabled")
    if args.event == "schedule":
        ids = [s["id"] for s in enabled]
        out(skills=json.dumps(ids), count=len(ids), command=value)
        return
    s = enabled[0] if enabled else None
    out(skill=s["id"] if s else "", command=value,
        **{"tool-profile": s["tool_profile"] if s else "", "model-tier": s["model_tier"] if s else "",
           "role": (s.get("role") or s["id"]) if s else ""})
    if s:
        note(f"Skill matched: `{s['id']}` ({args.event} `{value}`)")


def gh_json(args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def render(args):
    skills = {s["id"]: s for s in catalogue(args.project)}
    s = skills.get(args.skill)
    if not s:
        raise SystemExit(f"unknown skill {args.skill}")
    ctx = {"trusted": {"skill": s["id"], "repo": args.repo, "number": args.number or None, "event": args.event,
                       "hub_repo": os.environ.get("SQUADHQ_HUB_REPO"),
                       "hub_path": os.path.relpath(HOME, os.environ.get("GITHUB_WORKSPACE", os.getcwd()))}}
    if args.repo and args.number:
        issue = gh_json(["api", f"repos/{args.repo}/issues/{args.number}"])
        if issue:
            ctx["issue"] = {"number": issue["number"], "title": issue.get("title", ""), "body": issue.get("body") or "",
                            "author": (issue.get("user") or {}).get("login", "unknown"),
                            "association": issue.get("author_association", "NONE"),
                            "labels": [lab["name"] for lab in issue.get("labels", [])]}
        comments = gh_json(["api", f"repos/{args.repo}/issues/{args.number}/comments?per_page=100"]) or []
        ctx["issue_comments"] = [{"id": c["id"], "body": c.get("body") or "",
                                  "author": (c.get("user") or {}).get("login", "unknown"),
                                  "association": c.get("author_association", "NONE")} for c in comments[-30:]]
        if issue and issue.get("pull_request"):
            files = gh_json(["api", f"repos/{args.repo}/pulls/{args.number}/files?per_page=100"]) or []
            ctx["pull_request_files"] = {"source": f"pr#{args.number}/files",
                                         "text": "\n".join(f"{f['status']}\t{f['filename']}" for f in files)}
    if args.project_list:
        try:
            ctx["trusted"]["projects"] = [p["repo"] for p in json.load(
                open(os.path.join(HOME, "config", "projects.json"))).get("projects", [])]
        except (OSError, ValueError):
            pass
    path = os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "skill-ctx.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ctx, f)
    r = subprocess.run([sys.executable, "-P", "-m", "squadhq.skills", "render-prompt", s["id"], "--context-file", path,
                        "--json"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"render-prompt failed: {r.stderr.strip()[:500]}")
    rendered = json.loads(r.stdout)
    prompt = rendered["system"] + "\n\n---\n\n" + rendered["user"]
    out(prompt=prompt, model=model_for(s["model_tier"]), **{"tool-profile": s["tool_profile"],
                                                           "role": s.get("role") or s["id"]})


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("match")
    m.add_argument("--event", required=True, choices=["label", "command", "schedule", "skill"])
    m.add_argument("--value", default="")
    m.add_argument("--project", default="")
    r = sub.add_parser("render")
    r.add_argument("--skill", required=True)
    r.add_argument("--repo", default="")
    r.add_argument("--number", default="")
    r.add_argument("--event", default="")
    r.add_argument("--project", default="")
    r.add_argument("--project-list", action="store_true")
    a = ap.parse_args()
    match(a) if a.cmd == "match" else render(a)


if __name__ == "__main__":
    main()
