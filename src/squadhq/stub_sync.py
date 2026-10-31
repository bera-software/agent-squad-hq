#!/usr/bin/env python3
"""
Stub sync: keeps every project's .github/workflows/agents.yml equal to
onboarding/agents.yml.stub (rendered for the org and hub).

  python -m squadhq.stub_sync [--dry-run] [--repo owner/name]

onboard only SEEDS the stub when it is missing, so a stub change (e.g. a new
trigger type the router depends on) would never reach existing projects. This
is that sync. For each project in config/projects.json whose stub differs, it
writes the rendered stub to branch `squadhq-stub-sync` (created from, or reset
to, the default branch) and opens — or reuses — a PR. It never pushes to the
default branch: an approver merges, same as onboarding.

Exit code: 0 when every repo is in sync or got a PR, 1 when any repo errored
(the table in the step summary names which).

Template placeholders: __HUB_REPO__ (settings.hub_repo()), __HUB_REF__ (the
40-char commit SHA that SQUADHQ_STUB_REF — default "main" — resolves to, so
projects pin the hub by SHA) and __HUB_TAG__ (that ref's text, for the
`# vX.Y.Z` comment). Any other __PLACEHOLDER__ left in a rendered stub is an
error for that repo.

Server mode (--server or SQUADHQ_DEPLOYMENT=server) also syncs
onboarding/optional/squadhq-agent.yml.stub to
.github/workflows/squadhq-agent.yml.

Required env: GH_TOKEN (App token — writing under .github/workflows needs the
App's workflows permission, which onboarding already relies on).
"""
import argparse
import base64
import json
import os
import re
import sys

from squadhq import github, settings, untrusted
from squadhq.paths import hub_path

PATH = ".github/workflows/agents.yml"
SERVER_PATH = ".github/workflows/squadhq-agent.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


class StubError(RuntimeError):
    """A stub could not be rendered (unresolvable ref, leftover placeholder)."""
BRANCH = "squadhq-stub-sync"
TITLE = "chore: sync Agent Squad HQ agent stub"


def stub_path(server=False):
    if server:
        return hub_path("onboarding", "optional", "squadhq-agent.yml.stub")
    return hub_path("onboarding", "agents.yml.stub")


def server_mode(flag=False):
    # SQUADHQ_DEPLOYMENT=actions|server says how this hub is deployed. The old
    # SQUADHQ_MODE=server spelling is still honoured; SQUADHQ_MODE now belongs to
    # the server (byo-compute | worker).
    dep = os.environ.get("SQUADHQ_DEPLOYMENT", "").strip().lower()
    return bool(flag) or dep == "server" or os.environ.get("SQUADHQ_MODE", "").strip().lower() == "server"


def gh(args, input_text=None):
    """Seam: one gh call (tests replace this)."""
    return github.client().run(args, input_text=input_text)


def api(path, method="GET", fields=None):
    args = ["api", "-X", method, path]
    body = json.dumps(fields) if fields else None
    if body:
        args += ["--input", "-"]
    r = gh(args, body)
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        data = {}
    return r.returncode, data, r.stderr


def resolve_ref(hub_repo, ref):
    """The commit SHA ``ref`` (tag, branch or SHA) points at in the hub."""
    r = gh(["api", f"repos/{hub_repo}/commits/{ref}", "--jq", ".sha"])
    sha = r.stdout.strip()
    if r.returncode or not SHA_RE.match(sha):
        raise StubError(f"cannot resolve hub ref {ref!r} in {hub_repo} to a commit SHA: "
                        f"{(r.stderr or sha).strip()[:100]}")
    return sha


BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,99}$")


def follow_branch():
    """``SQUADHQ_STUB_FOLLOW``: a hub branch projects track directly instead of a pinned commit
    (opt-in, e.g. ``main`` for an owner's own projects). Stubs then never change on a hub
    release, so no sync PR is opened; every change merged to that branch reaches the projects,
    with their secrets, on their next run. Empty (the default) pins by commit SHA."""
    branch = (os.environ.get("SQUADHQ_STUB_FOLLOW") or "").strip()
    if branch and (not BRANCH_RE.match(branch) or ".." in branch or SHA_RE.match(branch)):
        raise StubError(f"SQUADHQ_STUB_FOLLOW must be a branch name, not {branch!r}")
    return branch


def placeholders(hub_repo=None, ref=None):
    """{placeholder: value} for the stubs. Resolves the hub ref once (or follows a branch)."""
    hub_repo = hub_repo or settings.hub_repo()
    branch = follow_branch()
    if branch:
        return {"__HUB_REPO__": hub_repo, "__HUB_REF__": branch, "__HUB_TAG__": branch}
    ref = ref or os.environ.get("SQUADHQ_STUB_REF") or "main"
    return {"__HUB_REPO__": hub_repo, "__HUB_REF__": resolve_ref(hub_repo, ref), "__HUB_TAG__": ref}


def render_text(text, subs, source="stub"):
    for k, v in subs.items():
        text = text.replace(k, v)
    leftover = sorted(set(PLACEHOLDER_RE.findall(text)))
    if leftover:
        raise StubError(f"{source}: unsubstituted placeholder(s) {', '.join(leftover)}")
    return text


def render(subs, server=False):
    path = stub_path(server)
    with open(path) as f:
        return render_text(f.read(), subs, os.path.basename(path))


def sync(repo, dry_run, subs=None, server=False):
    """-> (status, markdown row). status is "ok" or "error". With
    server=True the server-mode stub goes to SERVER_PATH instead."""
    target = SERVER_PATH if server else PATH
    branch = f"{BRANCH}-server" if server else BRANCH
    try:
        want = render(subs if subs is not None else placeholders(), server)
    except (StubError, OSError, RuntimeError) as e:
        return "error", f"| {repo} | error rendering {target} | {str(e)[:100]} |"
    code, repo_info, err = api(f"repos/{repo}")
    if code or "default_branch" not in repo_info:
        return "error", f"| {repo} | error | {err.strip()[:100] or 'repo not readable'} |"
    default = repo_info["default_branch"]
    code, cur, _ = api(f"repos/{repo}/contents/{target}?ref={default}")
    have = base64.b64decode(cur["content"]).decode() if code == 0 and cur.get("content") else None
    if have == want:
        return "ok", f"| {repo} | {target}: in sync | — |"
    if dry_run:
        return "ok", f"| {repo} | {target}: out of date | would open/update PR |"

    code, ref, err = api(f"repos/{repo}/git/ref/heads/{default}")
    base_sha = (ref.get("object") or {}).get("sha")
    if code or not base_sha:
        return "error", f"| {repo} | error reading {default} | {err.strip()[:100]} |"
    code, _, _ = api(f"repos/{repo}/git/ref/heads/{branch}")
    if code == 0:
        code, _, err = api(f"repos/{repo}/git/refs/heads/{branch}", "PATCH", {"sha": base_sha, "force": True})
    else:
        code, _, err = api(f"repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": base_sha})
    if code:
        return "error", f"| {repo} | error preparing branch | {err.strip()[:100]} |"
    fields = {"message": TITLE, "branch": branch, "content": base64.b64encode(want.encode()).decode()}
    if have is not None:
        fields["sha"] = cur["sha"]
    code, _, err = api(f"repos/{repo}/contents/{target}", "PUT", fields)
    if code:
        return "error", f"| {repo} | error writing stub | {err.strip()[:100]} |"
    # Reuse only OUR PR: a fork PR whose branch happens to be named
    # squadhq-stub-sync must never be reported (and merged) as the sync.
    r = gh(["pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "url,author"])
    try:
        prs = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except ValueError:
        prs = []
    url = next((p.get("url", "") for p in prs if isinstance(p, dict) and untrusted.by_bot(p)), "")
    if not url:
        r = gh(["pr", "create", "-R", repo, "-H", branch, "-B", default, "-t", TITLE,
                "-b", "Brings `.github/workflows/agents.yml` in line with the hub's "
                      "`onboarding/agents.yml.stub`. The stub only lists the events the hub's router needs; "
                      "all logic lives in the Agent Squad HQ hub. Merge to apply."])
        if r.returncode:
            return "error", f"| {repo} | error opening PR | {r.stderr.strip()[:100]} |"
        url = r.stdout.strip()
    return "ok", f"| {repo} | {target}: PR opened/updated | {url} |"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m squadhq.stub_sync")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--server", action="store_true", help="also sync the server-mode stub (or SQUADHQ_DEPLOYMENT=server)")
    ap.add_argument("--ref", help="hub tag/branch/SHA to pin (default: SQUADHQ_STUB_REF or main)")
    args = ap.parse_args(argv)
    server = server_mode(args.server)
    if args.repo:
        repos = [args.repo]
    else:
        with open(hub_path("config", "projects.json")) as f:
            repos = [p["repo"] for p in json.load(f)["projects"]]
    results = []
    try:
        subs = placeholders(ref=args.ref)
    except (StubError, RuntimeError) as e:
        subs = None
        results = [("error", f"| {r} | error | {str(e)[:100]} |") for r in repos]
    for r in repos if subs is not None else []:
        for srv in ((False, True) if server else (False,)):
            try:
                results.append(sync(r, args.dry_run, subs, srv))
            except Exception as e:  # one broken repo must not stop the others
                results.append(("error", f"| {r} | error | {e!r:.100} |"))
    failed = sum(1 for status, _ in results if status != "ok")
    text = "\n".join([f"## Stub sync{' (dry run)' if args.dry_run else ''}", "",
                      "| project | stub | action |", "|---|---|---|", *[row for _, row in results], "",
                      f"{len(results) - failed} ok / {failed} failed"])
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
