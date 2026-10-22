#!/usr/bin/env python3
"""
Onboard/audit a project repo for the Agent Squad HQ hub.

  python -m squadhq.onboard audit <owner/repo> <ProjectName>
  python -m squadhq.onboard apply <owner/repo> <ProjectName>

audit: read-only readiness report (also runs automatically at the end of apply),
       posted as a hub issue and to the step summary.
apply: creates labels, opens a PR on the project with the stub + missing memory
       files, adds the project to config/projects.json (commit on the hub),
       then audits.

The onboarding PR is tailored to the project's stack packs (the registry
entry's ``packs``, else those detected in the clone): a starter ``CLAUDE.md``
pre-filled from the packs' commands, rules and review red lines (every gap
marked ``TODO(owner)``), ADR 0001 (Nygard format), and each pack's starter
architecture config (``arch.snippet``) and, when the repo has no linter
config, its default lint config (``ci.lint.snippet``). Nothing ever
overwrites an existing file; the PR body lists what the owner must finish.

Exit codes: 0 ok, 1 the repo is unreachable or a push failed, 2 bad arguments.
Uses gh's credential: GH_TOKEN in workflows (minted per run from the
squadhq-ops GitHub App, covering the repo + hub) or gh's stored login locally. Never touches secrets or repository settings.
"""
import argparse
import shlex
import datetime
import json
import os
import re
import subprocess
import sys
import shutil
import tempfile
import time

import yaml

from squadhq import github, settings, stub_sync
from squadhq.flow_reader import get_labels, static_label_names
from squadhq.paths import hub_path

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
NAME_RE = re.compile(r"^[\w .-]{1,64}$")
BRANCH = "squadhq-onboarding"
PUSH_ATTEMPTS = 3


class PushError(RuntimeError):
    pass


# ---------------------------------------------------------------- seams

def sh(args, check=True, cwd=None):
    r = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=300)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stderr}")
    return r.stdout


def run(args, cwd=None):
    """Like sh() but returns the CompletedProcess (for return-code checks)."""
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=300)


def gh(args, check=True):
    r = github.client().run(args)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}\n{r.stderr}")
    return r.stdout


def gh_ok(args):
    return github.client().run(args).returncode == 0


def hub_repo():
    try:
        return settings.hub_repo()
    except RuntimeError:
        return os.environ.get("GITHUB_REPOSITORY", "")


def projects_file():
    return hub_path("config", "projects.json")


def load_registry():
    with open(projects_file()) as f:
        return json.load(f)


def write_step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return  # local run — nothing to write to
    with open(path, "a") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def exists_file(repo, path):
    return gh_ok(["api", f"repos/{repo}/contents/{path}"])


def credential_source():
    """Which credential gh is using, in words — never the value."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return f"token from ${name}"
    return "gh's stored login (keyring)"


def access_failure(repo, stderr):
    """Why `gh api repos/<repo>` failed: authentication vs not found / no access."""
    err = (stderr or "").lower()
    cred = credential_source()
    if "401" in err or "bad credentials" in err or "authentication" in err or "gh auth login" in err:
        fix = "Re-mint the App token for this job." if cred.startswith("token") else "Run `gh auth login`."
        return f"authentication failed using {cred}: the credential is missing, expired or revoked. {fix}"
    if "404" in err or "not found" in err:
        app = settings.bot_login().removesuffix("[bot]")
        return (f"{repo} was not found, or {cred} has no access to it. Check the spelling, and that the "
                f"{app} GitHub App is installed on {repo}.")
    detail = (stderr or "").strip()[:200] or "no output from gh"
    return f"cannot read {repo} using {cred}: {detail}"


def default_branch(repo):
    return gh(["api", f"repos/{repo}", "-q", ".default_branch"]).strip()


def label_entries():
    # config/flows.yml is the single source of truth for the label registry.
    return static_label_names()


def push_with_retry(cmd, cwd=None, attempts=PUSH_ATTEMPTS):
    """`git push` that is CHECKED: on failure rebase onto the remote and retry,
    then raise PushError — nothing downstream (PR creation) may run against
    a branch that never reached the remote."""
    last = ""
    for i in range(attempts):
        r = run(cmd, cwd=cwd)
        if r.returncode == 0:
            return
        last = (r.stderr or r.stdout).strip()
        if i + 1 < attempts:
            run(["git", "pull", "--rebase"], cwd=cwd)
            time.sleep(2 * (i + 1))
    raise PushError(f"{' '.join(cmd)} failed after {attempts} attempts: {last[:300]}")


# ---------------------------------------------------------------- audit

SECRET_PURPOSE = {
    "ANTHROPIC_API_KEY": "hub loops / metered model API",
    "CLAUDE_CODE_OAUTH_TOKEN": "coding agents (subscription auth)",
}


def audit(repo, pname, hub=None):
    hub = hub or hub_repo()
    labels = [name for name, _color in label_entries()]
    checks = []

    def ck(name, ok, fix, info=False):
        # info=True: informational only, never a failure and not in the ratio.
        checks.append((name, ok, fix, info))
    r = github.client().run(["api", f"repos/{repo}", "--jq", ".default_branch"])
    if r.returncode != 0 or not r.stdout.strip():
        print(f"# Onboarding audit: {pname} ({repo})")
        print("FATAL: " + access_failure(repo, r.stderr))
        return 1
    db = r.stdout.strip()
    ck(f"Repo reachable ({credential_source()})", True, "")
    ck("Stub .github/workflows/agents.yml", exists_file(repo, ".github/workflows/agents.yml"),
       "run apply mode / merge the onboarding PR")
    ck("CLAUDE.md (project memory)", exists_file(repo, "CLAUDE.md"),
       "merge onboarding PR, then fill 'Project specifics'")
    ck("docs/adr/ present", exists_file(repo, "docs/adr/0001-record-architecture-decisions.md"),
       "merge onboarding PR")
    ck("docs/lessons.md", exists_file(repo, "docs/lessons.md"), "merge onboarding PR")
    try:
        have = {lbl["name"] for lbl in github.client().paginate(f"repos/{repo}/labels")}
        missing = [lbl for lbl in labels if lbl not in have]
        ck(f"Labels ({len(labels) - len(missing)}/{len(labels)})", not missing,
           f"apply mode creates them (missing: {', '.join(missing[:6])}{'...' if len(missing) > 6 else ''})")
    except github.GitHubError as e:
        ck("Labels (unreadable)", False, f"could not list labels: {e.stderr.strip()[:120]}")
    entry = next((p for p in load_registry()["projects"] if p["repo"] == repo), None)
    ck("Registered in projects.json", entry is not None, "apply mode adds it")
    packs = (entry or {}).get("packs") or []
    ck(f"Stack packs selected ({', '.join(packs) or 'none'})", bool(packs),
       "optional — set \"packs\" in config/projects.json (see packs/)", info=True)
    ck("Branch protection on default branch", gh_ok(["api", f"repos/{repo}/branches/{db}/protection"]),
       "MANUAL: Settings->Rules: require ONE check, '<stub job id> / ci-gate' (stock stub: "
       "'hub / ci-gate'); require a pull request; no direct pushes. Do NOT require the "
       "individual CI/review jobs - they legitimately SKIP on PRs they don't apply to, and a "
       "skipped check never satisfies a required one. ci-gate always runs.")
    secrets = github.client().json(["api", f"repos/{repo}/actions/secrets"], default=None)
    if isinstance(secrets, dict):
        names = {s["name"] for s in secrets.get("secrets", [])}
        for secret, purpose in SECRET_PURPOSE.items():
            ck(f"Secret {secret} — {purpose}", secret in names,
               "MANUAL: add in repo Settings->Secrets (or org-level). Only the credential for the "
               "provider/auth mode you configured is required", info=True)
    else:
        ck("Model credentials (unreadable with this token)", False,
           "MANUAL: verify the provider credential exists in repo or org secrets — the App needs "
           "'Actions secrets: read' to check this automatically", info=True)
    ck("Preview / staging deployments", False, "MANUAL: verify preview deploys + staging env", info=True)
    ck("graphify graph present", exists_file(repo, "graphify-out/graph.json"),
       "optional — see CLAUDE.md 'Codebase navigation'", info=True)

    scored = [c for c in checks if not c[3]]
    ok = sum(1 for _, o, _, _ in scored if o)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    lines = [f"# Onboarding audit: {pname} ({repo}) — {ok}/{len(scored)} ready", f"_{today}_", ""]
    for name, o, fix, info in checks:
        mark = ("✅" if o else "➖") if info else ("✅" if o else "❌")
        lines.append(f"- {mark} **{name}**" + (f" — {fix}" if not o and fix else ""))
    lines += ["", "Re-run: Actions → Onboard Project → mode=audit, or locally:",
              f"`python -m squadhq.onboard audit {shlex.quote(repo)} {shlex.quote(pname)}`.",
              "Start agents only at 100% or with explicit ❌-acceptance."]
    body = "\n".join(lines)
    print(body)
    write_step_summary(body)
    if hub:
        gh(["issue", "create", "-R", hub, "-t", f"Onboarding audit: {pname}", "-l", "escalation", "-b", body],
           check=False)
    return 0


# ---------------------------------------------------------------- apply

def seed(td, dst, src, subs=None):
    """Copy a hub template into the clone unless the file already exists.
    Fails loudly on an unsubstituted __PLACEHOLDER__ rather than shipping it."""
    target = os.path.join(td, dst)
    if os.path.exists(target):
        return False
    with open(src) as f:
        s = f.read()
    for k, v in (subs or {}).items():
        s = s.replace(k, v)
    leftover = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", s)))
    if leftover:
        raise RuntimeError(f"{src}: unsubstituted placeholder(s) {', '.join(leftover)} — "
                           f"add them to the seed() call's subs in squadhq/onboard.py")
    os.makedirs(os.path.dirname(target) or td, exist_ok=True)
    with open(target, "w") as f:
        f.write(s)
    return True


# ---------------------------------------------------------------- stack packs

def selected_packs(td, entry=None):
    """``(pack, root)`` pairs the onboarding PR is tailored to: the registry
    entry's ``packs`` when it lists any, otherwise the packs detected in the
    clone. Never raises: without packs the files are simply not pre-filled."""
    from squadhq import packs as P
    out = []
    try:
        if entry and entry.get("packs"):
            roots = entry.get("pack_roots") or {}
            for pid in entry["packs"]:
                pack = P.load_pack(pid)
                out.append((pack, str(roots.get(pid) or pack.root).rstrip("/") or "."))
        else:
            out = [(P.load_pack(d["pack"]), d["root"]) for d in P.detect(td)]
    except (P.PackError, OSError, ValueError, yaml.YAMLError) as e:
        print(f"onboard: stack packs unavailable ({e}); starter files are not pre-filled", file=sys.stderr)
        return []
    return out


def _under(root, rel):
    return rel if root in ("", ".") else f"{root}/{rel}"


def section_bullets(path, per_section=1, only=None):
    """``[(section, text)]``: the first ``per_section`` top-level bullets of
    each ``## `` section of a pack prompt file (continuation lines joined).
    ``only``: keep sections whose heading starts with it."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out, section, taken, cur = [], "", 0, None

    def flush():
        nonlocal cur
        if cur is not None:
            out.append((section, " ".join(cur)))
            cur = None
    for line in lines:
        if line.startswith("## "):
            flush()
            section, taken = line[3:].strip(), 0
        elif line.startswith("- ") and section and (only is None or section.startswith(only)):
            flush()
            if taken < per_section:
                cur, taken = [line[2:].strip()], taken + 1
        elif cur is not None and line.startswith("  ") and line.strip():
            cur.append(line.strip())
        else:
            flush()
    flush()
    return out


def _cmd(c):
    return f"`{c}`" if "`" not in c else c


def claude_md_subs(selected):
    """Placeholder values for the starter CLAUDE.md, from the selected packs."""
    if not selected:
        none = "- TODO(owner): no stack pack was detected; describe the stack and its commands here."
        return {"__PACKS__": none, "__LAYERING__": "- TODO(owner): no architecture check is configured.",
                "__COMMANDS__": "- TODO(owner): install, build, test, lint commands.",
                "__STANDARDS__": "- TODO(owner): coding standards.", "__DO_NOT__": ""}
    packs_l, layering, commands, standards, do_not = [], [], [], [], []
    for pack, root in selected:
        where = "" if root in ("", ".") else f" in `{root}/`"
        packs_l.append(f"- `{pack.id}` — {pack.name}{where}. TODO(owner): remove it if it does not apply.")
        c = pack.ci
        parts = [f"{label} {_cmd(cmd)}" for label, cmd in (("install", c.install), ("build", c.build),
                                                            ("test", c.test)) if cmd]
        if c.lint:
            parts.append(f"lint {_cmd(c.lint.local)}")
        if pack.arch:
            parts.append(f"arch {_cmd(pack.arch.local)}")
        commands.append(f"- `{pack.id}`{where}: " + " · ".join(parts) if parts else f"- `{pack.id}`{where}: none")
        a = pack.arch
        if a:
            layering.append(f"- `{pack.id}`: {a.tool}, rules in `{_under(root, a.config)}`"
                            + (f" (starter: `packs/{pack.id}/snippets/{a.snippet}`)" if a.snippet else "")
                            + ". The `arch` gate runs whenever that config exists.")
        else:
            layering.append(f"- `{pack.id}`: no architecture tool in this pack; the reviewer checks the rules above.")
        for sec, text in section_bullets(os.path.join(pack.dir, "implement.md"))[:6]:
            standards.append(f"- [{pack.id} · {sec}] {text}")
        for _sec, text in section_bullets(os.path.join(pack.dir, "review.md"), per_section=4,
                                          only="REQUEST CHANGES"):
            do_not.append(f"- [{pack.id}] Not allowed: {text}")
    return {"__PACKS__": "\n".join(packs_l), "__LAYERING__": "\n".join(layering),
            "__COMMANDS__": "\n".join(commands),
            "__STANDARDS__": "\n".join(standards) or "- TODO(owner): coding standards.",
            "__DO_NOT__": "\n".join(do_not)}


def seed_copy(td, dst, src):
    """Copy a pack snippet into the clone unless the target exists (no
    placeholder substitution: snippets are complete files)."""
    target = os.path.normpath(os.path.join(td, dst))
    if not target.startswith(os.path.normpath(td) + os.sep):
        raise RuntimeError(f"refusing to seed outside the clone: {dst}")
    if os.path.lexists(target) or not os.path.isfile(src):
        return False
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(src, target)
    return True


def seed_quality_files(td, selected):
    """Starter architecture and lint configs for the selected packs.
    -> ``(seeded, notes)``: ``[(path, checklist note)]`` for files added, and
    checklist notes for configs the owner must add by hand."""
    from squadhq.quality import gates as G
    seeded, notes = [], []
    for pack, root in selected:
        rdir = td if root in ("", ".") else os.path.join(td, root)
        a = pack.arch
        if a and a.snippet and not G.find_marker(rdir, a.config):
            req = f" Requires: {a.requires}." if a.requires else ""
            if a.snippet_path:
                dst = _under(root, a.snippet_path)
                if seed_copy(td, dst, os.path.join(pack.dir, "snippets", a.snippet)):
                    seeded.append((dst, f"architecture rules for the `arch` gate ({a.tool}, pack `{pack.id}`): "
                                        f"replace every TODO(owner) with this project's layout.{req} The gate "
                                        "runs whenever this file exists; delete it to opt out."))
            else:
                notes.append(f"Architecture test for the `arch` gate ({a.tool}, pack `{pack.id}`): not seeded. Copy "
                             f"`packs/{pack.id}/snippets/{a.snippet}` into the repo as a file matching "
                             f"`{_under(root, a.config)}`.{req}")
        lint = pack.ci.lint
        if lint and lint.snippet and lint.snippet_path and not any(G.find_marker(rdir, c) for c in lint.configs):
            dst = _under(root, lint.snippet_path)
            if seed_copy(td, dst, os.path.join(pack.dir, "snippets", lint.snippet)):
                seeded.append((dst, f"default linter config (pack `{pack.id}`): the `lint` gate runs with it once "
                                    "merged. Fix or relax its findings in this PR, or delete the file."))
    return seeded, notes


def pr_body(changed, seeded=(), notes=()):
    lines = ["Adds the Agent Squad HQ stub and starter project files. Nothing here overwrites an existing file.",
             "", "**Before merging (owner):**"]
    if "CLAUDE.md" in changed:
        lines.append("- [ ] `CLAUDE.md`: replace every `TODO(owner)`: architecture overview, layering rules, coding "
                     "standards, test policy, the do-not list and 'Project specifics'. Pre-filled lines come from "
                     "the detected stack packs; delete what does not apply.")
    if "adr-0001" in changed:
        lines.append("- [ ] `docs/adr/0001-record-architecture-decisions.md`: set the date, then record the "
                     "architecture baseline as ADR 0002.")
    for path, note in seeded:
        lines.append(f"- [ ] `{path}`: {note}")
    for note in notes:
        lines.append(f"- [ ] {note}")
    if len(lines) == 3:
        lines.append("- [ ] Review the stub.")
    lines += ["", "**After merging:** complete the MANUAL items from the onboarding audit issue in the hub."]
    return "\n".join(lines)


def seed_files(td, repo, pname, branch, hub, stub_subs=None, server=False, selected=None):
    """stub_subs: stub_sync.placeholders() for the hub (__HUB_REPO__,
    __HUB_REF__, __HUB_TAG__). server: also seed the server-mode stub.
    selected: ``(pack, root)`` pairs that pre-fill the starter CLAUDE.md."""
    tpl = lambda *p: hub_path("onboarding", *p)  # noqa: E731
    subs = dict(stub_subs or {"__HUB_REPO__": hub})
    claude_subs = {"__PROJECT__": pname, "__BRANCH__": branch, **claude_md_subs(selected or [])}
    plan = [
        (".github/workflows/agents.yml", tpl("agents.yml.stub"), subs, "stub"),
        ("CLAUDE.md", tpl("CLAUDE.md.template"), claude_subs, "CLAUDE.md"),
        ("docs/adr/0000-template.md", tpl("adr-0000-template.md"), None, "adr-template"),
        ("docs/adr/0001-record-architecture-decisions.md", tpl("adr-0001.md"), None, "adr-0001"),
        ("docs/lessons.md", tpl("lessons.md.template"), {"__PROJECT__": pname}, "lessons"),
    ]
    if server:
        plan.append((stub_sync.SERVER_PATH, tpl("optional", "squadhq-agent.yml.stub"), subs, "server-stub"))
    agents_tpl = hub_path("templates", "AGENTS.md.template")
    if os.path.exists(agents_tpl):
        plan.append(("AGENTS.md", agents_tpl, {"__PROJECT__": pname, "__BRANCH__": branch}, "AGENTS.md"))
    return [name for dst, src, subs, name in plan if seed(td, dst, src, subs)]


def register(repo, pname, db):
    """Add the project to config/projects.json and push the commit. -> True
    when newly registered. Raises PushError if the commit never reached the hub."""
    reg = load_registry()
    if any(p["repo"] == repo for p in reg["projects"]):
        return False
    reg["projects"].append({"name": pname, "repo": repo, "integration_branch": db, "frontend_url": "",
                            "staging_url": "", "keywords": [], "markets": [], "notes": ""})
    with open(projects_file(), "w") as f:
        json.dump(reg, f, indent=2)
        f.write("\n")
    name, email = github.bot_identity()
    root = hub_path()
    sh(["git", "-C", root, "add", "config/projects.json"], check=False)
    r = run(["git", "-C", root, "-c", f"user.name={name}", "-c", f"user.email={email}",
             "commit", "-m", f"chore: register project {pname}"])
    if r.returncode != 0:
        raise PushError(f"could not commit the registry change: {(r.stderr or r.stdout).strip()[:200]}")
    push_with_retry(["git", "-C", root, "push"])
    return True


def apply(repo, pname, hub=None, server=False, ref=None):
    hub = hub or hub_repo()
    # Resolve the pinned hub SHA before touching anything: an unresolvable ref
    # must fail the run, never seed a stub pointing at nothing.
    stub_subs = stub_sync.placeholders(hub, ref)
    # gh is authenticated but plain git is not; make git use gh's token too.
    run(["gh", "auth", "setup-git"])
    entries = label_entries()
    for name, color in entries:
        gh(["label", "create", name, "-R", repo, "--force", "--color", color], check=False)
    project_color = (get_labels("dynamic").get("project:<name>") or {}).get("color", "ededed")
    if hub:
        gh(["label", "create", f"project:{pname.lower()}", "-R", hub, "--force", "--color", project_color],
           check=False)
        # hub_only labels belong on the hub, never on a project repo.
        for name, meta in (get_labels("hub_only") or {}).items():
            gh(["label", "create", name, "-R", hub, "--force", "--color", (meta or {}).get("color", "ededed")],
               check=False)

    db = default_branch(repo)
    # __BRANCH__ names the integration branch; an existing registry entry wins
    # so a re-run never rewrites a deliberate non-default branch.
    entry = next((p for p in load_registry()["projects"] if p["repo"] == repo), None)
    integration_branch = (entry or {}).get("integration_branch") or db
    pr_url = ""
    with tempfile.TemporaryDirectory() as td:
        sh(["gh", "repo", "clone", repo, td, "--", "--depth", "1"])
        sh(["git", "checkout", "-b", BRANCH], cwd=td)
        selected = selected_packs(td, entry)
        changed = seed_files(td, repo, pname, integration_branch, hub, stub_subs, server, selected)
        seeded, notes = seed_quality_files(td, selected)
        changed += [path for path, _note in seeded]
        if changed:
            bot_name, bot_email = github.bot_identity()
            sh(["git", "add", "-A"], cwd=td)
            sh(["git", "-c", f"user.name={bot_name}", "-c", f"user.email={bot_email}",
                "commit", "-m", f"chore: Agent Squad HQ onboarding ({', '.join(changed)})"], cwd=td)
            push_with_retry(["git", "push", "-u", "origin", BRANCH, "--force"], cwd=td)
            pr_url = gh(["pr", "create", "-R", repo, "-H", BRANCH, "-B", db,
                         "-t", f"Agent Squad HQ onboarding: {pname}",
                         "-b", pr_body(changed, seeded, notes)], check=False).strip()

    registered = register(repo, pname, db)
    apply_lines = [f"## Onboarding apply: {pname} ({repo})", "",
                   f"- Labels ensured in {repo}: {', '.join(n for n, _ in entries)}",
                   f"- Label ensured in {hub or '(no hub)'}: project:{pname.lower()}",
                   f"- Files seeded: {', '.join(changed) if changed else 'none (already present)'}",
                   f"- PR opened: {pr_url}" if pr_url else "- PR opened: none (nothing needed seeding)",
                   f"- Registry: {'added ' + repo + ' to config/projects.json' if registered else 'already registered'}",
                   ""]
    write_step_summary("\n".join(apply_lines))
    return audit(repo, pname, hub)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m squadhq.onboard")
    ap.add_argument("mode", choices=("audit", "apply"))
    ap.add_argument("repo", help="owner/name")
    ap.add_argument("project", help="display name, e.g. 'My App'")
    ap.add_argument("--server", action="store_true",
                    help="also seed the server-mode stub (or SQUADHQ_DEPLOYMENT=server)")
    ap.add_argument("--ref", help="hub tag/branch/SHA to pin (default: SQUADHQ_STUB_REF or main)")
    args = ap.parse_args(argv)
    if not REPO_RE.match(args.repo):
        print(f"onboard: invalid repo {args.repo!r} — expected owner/name", file=sys.stderr)
        return 2
    if not NAME_RE.match(args.project):
        print(f"onboard: invalid project name {args.project!r} — letters, digits, space, . _ - (max 64)",
              file=sys.stderr)
        return 2
    try:
        if args.mode == "audit":
            return audit(args.repo, args.project)
        return apply(args.repo, args.project, server=stub_sync.server_mode(args.server), ref=args.ref)
    except stub_sync.StubError as e:
        print(f"onboard: {e}", file=sys.stderr)
        write_step_summary(f"## ⚠ onboard: {e}")
        return 1
    except PushError as e:
        msg = f"onboard: PUSH FAILED — {e}"
        print(msg, file=sys.stderr)
        write_step_summary(f"## ⚠ {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
