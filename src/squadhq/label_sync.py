#!/usr/bin/env python3
"""
Label sync: makes sure every project repository has the labels the router and
the agents use (config/flows.yml is the registry), the hub has its hub-only
labels, and each project has its ``project:<name>`` label on the hub.

Onboarding creates labels once (``squadhq onboard``), but a project added
another way (Agent Squad HQ Cloud, a hand-edited squadhq.yml) or a label added
to the registry later would otherwise never reach existing repositories. Only
MISSING labels are created; existing labels (and their colours and
descriptions) are left alone. Unlike onboarding, a label GitHub refuses is an
error row and a non-zero exit, never a silent skip.

  python -m squadhq.label_sync [--dry-run] [--repo owner/name]

Required env: GH_TOKEN with issues:write on the hub and the projects.
"""
import argparse
import json
import os
import sys

from squadhq import github, settings
from squadhq.flow_reader import get_labels, static_label_names
from squadhq.paths import hub_path


def gh(args):
    """Seam: one gh call (tests replace this)."""
    return github.client().run(args)


def existing(repo):
    """Label names in ``repo`` (raises on a listing GitHub refuses)."""
    r = gh(["api", f"repos/{repo}/labels", "--paginate", "--jq", ".[].name"])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "could not list labels").strip()[:160])
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def ensure(repo, wanted, dry_run=False):
    """Create the labels in ``wanted`` [(name, colour)] that ``repo`` lacks.
    -> (created names, [(name, error)])."""
    have = existing(repo)
    created, failed = [], []
    for name, color in wanted:
        if name in have:
            continue
        if dry_run:
            created.append(name)
            continue
        r = gh(["label", "create", name, "-R", repo, "--color", color or "ededed"])
        if r.returncode == 0 or "already exists" in (r.stderr or ""):
            created.append(name)
        else:
            failed.append((name, (r.stderr or "").strip()[:120]))
    return created, failed


def plan(projects, hub):
    """[(repo, [(name, colour)])]: the registry on every project, and on the hub its hub-only
    labels plus one ``project:<name>`` label per project."""
    registry = static_label_names()
    out = [(p["repo"], registry) for p in projects]
    if hub:
        project_color = (get_labels("dynamic").get("project:<name>") or {}).get("color", "ededed")
        hub_labels = [(name, (meta or {}).get("color", "ededed"))
                      for name, meta in (get_labels("hub_only") or {}).items()]
        hub_labels += [(f"project:{p['name'].lower()}", project_color) for p in projects]
        out.append((hub, hub_labels))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo", help="only this project repository")
    args = ap.parse_args(argv)
    with open(hub_path("config", "projects.json")) as f:
        projects = json.load(f)["projects"]
    if args.repo:
        projects = [p for p in projects if p["repo"] == args.repo]
    rows, errors = [], 0
    for repo, wanted in plan(projects, None if args.repo else settings.hub_repo()):
        try:
            created, failed = ensure(repo, wanted, args.dry_run)
        except Exception as e:  # one unreadable repo must not stop the others
            rows.append(f"| {repo} | error | {str(e)[:120]} |")
            errors += 1
            continue
        verb = "would create" if args.dry_run else "created"
        rows.append(f"| {repo} | {'ok' if not failed else 'error'} | "
                    + (f"{verb} {len(created)}: {', '.join(created[:12])}{' …' if len(created) > 12 else ''}"
                       if created else "nothing missing")
                    + (f"; failed: {', '.join(n for n, _ in failed)} ({failed[0][1]})" if failed else "") + " |")
        errors += bool(failed)
    body = "## Label sync\n\n| repo | result | labels |\n|---|---|---|\n" + "\n".join(rows)
    print(body)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(body + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
