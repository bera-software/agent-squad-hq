"""``squadhq`` command line.

    squadhq init [--non-interactive ...] [--create-app] [--apply]
    squadhq validate [FILE] [--explain]
    squadhq render [--check | --dry-run] [--config FILE] [--root DIR]
    squadhq doctor [--offline] [--check ID] [--list-checks]
    squadhq onboard OWNER/REPO [--name NAME] [--apply]
    squadhq repo-settings --hub OWNER/HUB [--project OWNER/REPO ...] [--apply]
    squadhq drill [--repo OWNER/REPO] [--apply] [--timeout SECONDS]
    squadhq packs <list|validate|files|ci-plan|tools|risk-globs|detect> ...
    squadhq server ...        (delegates to squadhq.server; needs agent-squad-hq[server])
    squadhq skills ...        (delegates to squadhq.skills)
    squadhq evals ...         (delegates to squadhq.skills.evals)
    squadhq version

Everything that changes GitHub is a dry run (prints the exact ``gh``
commands) unless ``--apply`` is given.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from squadhq import __version__
from squadhq.config import loader
from squadhq.config import registry as R
from squadhq.config import render as RN
from squadhq.config.gh import Gh
from squadhq.paths import hub_root

# Seams for tests: replace these module attributes.
GH: Gh = Gh()
SLEEP: Callable[[float], None] = time.sleep
NOW: Callable[[], float] = time.monotonic
OPEN_BROWSER: Callable[[str], bool] = webbrowser.open
SUBPROCESS_RUN: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
ASK: Callable[[str, str], str] | None = None


def _out(msg: str = "") -> None:
    print(msg)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def default_config_path() -> str:
    env = os.environ.get("SQUADHQ_CONFIG")
    if env:
        return env
    if os.path.isfile("squadhq.yml"):
        return "squadhq.yml"
    return os.path.join(hub_root(), "squadhq.yml")


def _root_for(args: argparse.Namespace, config_path: str) -> str:
    if getattr(args, "root", None):
        return str(args.root)
    return os.path.dirname(os.path.abspath(config_path))


def _config_missing(path: str) -> bool:
    if os.path.isfile(path):
        return False
    _err(f"error: {path} not found\n    hint: run `squadhq init` or copy squadhq.example.yml")
    return True


def _load(args: argparse.Namespace) -> tuple[Any, str, str] | None:
    path = args.config or default_config_path()
    root = _root_for(args, path)
    try:
        cfg = loader.load(path, hub_root=root)
    except loader.ConfigError as e:
        for i in e.issues:
            _err(str(i))
        return None
    return cfg, path, root


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------

def cmd_validate(args: argparse.Namespace) -> int:
    path = args.file or args.config or default_config_path()
    if _config_missing(path):
        return 2
    root = _root_for(args, path)
    with open(path, encoding="utf-8") as f:
        cfg, issues = loader.load_text(f.read(), path, **loader.hub_context(root))
    for i in issues:
        _err(str(i))
    errors = sum(1 for i in issues if i.level == "error")
    warnings = len(issues) - errors
    if cfg is None:
        _err(f"{path}: INVALID ({errors} error(s), {warnings} warning(s))")
        return 1
    _out(f"{path}: valid ({warnings} warning(s)); {len(cfg.projects)} project(s), "
         f"{len(cfg.providers.routing)} tier(s), {len(cfg.disabled_agents())} agent(s) disabled")
    if args.explain:
        _out("\njobs -> tier -> model")
        for job, tier in sorted(cfg.providers.jobs.items()):
            t = cfg.providers.routing[tier]
            _out(f"  {job:16} {tier:6} {t.primary}" + (f" (fallback {t.fallback})" if t.fallback else ""))
        _out("\nschedules (UTC)")
        for key, (crons, comment) in RN.render_schedules(cfg).items():
            _out(f"  {key:22} {' | '.join(crons):28} # {comment}")
        anth = cfg.providers.configs.get("anthropic")
        if anth and anth.pool:
            fb = "metered API fallback" if cfg.providers.fallback_on_capacity_trip else "pause"
            _out(f"\naccount pool ({anth.pool_strategy}; all tripped -> {fb})")
            for a in anth.pool:
                _out(f"  {a.id:16} {a.auth:12} {a.secret}")
        else:
            _out(f"\naccount pool: none (single credential, {anth.auth if anth else 'api'})")
    return 0


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def cmd_render(args: argparse.Namespace) -> int:
    if _config_missing(args.config or default_config_path()):
        return 2
    loaded = _load(args)
    if loaded is None:
        return 1
    cfg, _path, root = loaded
    if not require_hub(root):
        return 2
    try:
        rp = RN.plan(cfg, root)
    except ValueError as e:
        _err(f"error: {e}")
        return 1
    for w in rp.warnings:
        _err(f"warning: {w}")
    if args.check:
        if rp.stale:
            _err("stale generated files (run `squadhq render` and commit):")
            for f in rp.stale:
                _err(f"  {f.path}")
                if args.diff:
                    _err(f.diff())
            return 1
        _out(f"generated files are up to date ({len(rp.files)} checked)")
        return 0
    if args.dry_run:
        for f in rp.files:
            _out(f"{'would update' if f.changed else 'unchanged   '} {f.path}")
            if f.changed and args.diff:
                _out(f.diff())
    else:
        for p in RN.write(rp):
            _out(f"wrote {p}")
        if not rp.stale:
            _out("all generated files already up to date")
    _out("\nschedules (UTC):")
    for key, (crons, comment) in rp.schedules.items():
        _out(f"  {key:22} {' | '.join(crons):28} # {comment}")
    _out(f"\nrepository variables (org-level, visibility all; or -R {cfg.hub.repo}):")
    for k, v in rp.variables.items():
        _out(f"  {k}={v}")
    _out("\nset them with:")
    for line in RN.variable_commands({k: v for k, v in rp.variables.items() if v}, cfg.hub.repo):
        _out(f"  {line}")
    _out("\nGitHub Apps (ops + agent):")
    if cfg.hub.token_broker:
        _out(f"  token-broker mode: tokens come from {cfg.hub.token_broker} (OIDC); no App key is stored here")
    for line in RN.app_commands(cfg):
        _out(f"  {line}")
    _out("\nrequired secrets:")
    for name, purpose in rp.secrets:
        _out(f"  {name:28} {purpose}")
    pool_lines = RN.pool_secret_commands(cfg)
    if pool_lines:
        _out("\naccount pool secrets (gh reads each value from stdin):")
        for line in pool_lines:
            _out(f"  {line}")
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    from squadhq import doctor
    path = args.config or default_config_path()
    if args.list_checks:
        for cid, desc in doctor.CHECK_IDS.items():
            _out(f"{cid:18} {desc}")
        return 0
    if os.path.isfile(path) and not require_hub(_root_for(args, path)):
        return 2
    try:
        checks = doctor.run(path, _root_for(args, path), GH, offline=args.offline, only=args.check)
    except ValueError as e:
        _err(f"error: {e}")
        return 2
    text, code = doctor.report(checks)
    _out(text)
    return code


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------

def _answers_from_flags(args: argparse.Namespace) -> Any:
    from squadhq.config.init_wizard import Answers, ProjectAnswer, detect_remote_packs, detect_timezone
    owner = args.owner or (args.hub_repo.split("/")[0] if args.hub_repo else "")
    if not owner:
        raise SystemExit("error: --owner (or --hub-repo owner/name) is required with --non-interactive")
    projects = []
    for repo in args.project or []:
        packs: list[str] = []
        roots: dict[str, str] = {}
        if args.detect_packs:
            packs, roots = detect_remote_packs(GH, repo)
        projects.append(ProjectAnswer(repo=repo, name=repo.split("/")[-1].replace("-", " ").title(),
                                      branch=args.branch, packs=packs, pack_roots=roots))
    for spec in args.packs or []:  # owner/repo=pack1,pack2
        repo, _, ids = spec.partition("=")
        for p in projects:
            if p.repo == repo:
                p.packs = [x for x in ids.split(",") if x]
    return Answers(org_name=args.org_name or owner, owner=owner, hub_repo=args.hub_repo or f"{owner}/agent-squad-hq",
                   timezone=args.timezone or detect_timezone(), working_hours=args.working_hours,
                   approvers=args.approver or [f"@{owner}"], operating_model=args.operating_model,
                   auth=args.auth, monthly_usd=args.budget, app_slug=args.app_slug or "",
                   agent_app_slug=args.agent_app_slug or "", single_app=args.single_app, projects=projects,
                   token_broker=args.token_broker or "")


def _resolve_hub_dir(args: argparse.Namespace) -> tuple[str, bool]:
    """``(hub_dir, needs_scaffold)``: an explicit --hub-dir, else the cwd when
    it already is a hub, else ./agent-squad-hq."""
    from squadhq.config import scaffold as SC
    if args.hub_dir:
        d = os.path.abspath(args.hub_dir)
    elif SC.is_hub(os.getcwd()):
        d = os.getcwd()
    else:
        d = os.path.abspath("agent-squad-hq")
    return d, not SC.is_hub(d)


def _check_packs(answers: Any) -> list[str]:
    from squadhq.config import scaffold as SC
    from squadhq.config.loader import _suggest
    known = SC.known_template_packs()
    if known is None:
        return []
    return [f"error: --packs {p.repo}: unknown pack {pk!r}\n    hint: {_suggest(pk, known)}"
            for p in answers.projects for pk in p.packs if pk not in known]


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):  # closed or replaced stdin
        return False


def cmd_init(args: argparse.Namespace) -> int:
    from squadhq.config import app_manifest as AM
    from squadhq.config import init_wizard as W
    from squadhq.config import scaffold as SC

    if args.non_interactive:
        answers = _answers_from_flags(args)
    else:
        # Before any gh lookup: the interview needs a terminal to prompt on.
        if ASK is None and not _stdin_is_tty():
            _err("error: stdin is not a terminal: use --non-interactive with --owner/--hub-repo/--project/... "
                 "(see `squadhq init --help`)")
            return 2
        answers = W.interview(GH, ASK or W._default_ask)
    if args.token_broker:
        if not loader.BROKER_URL_RE.match(args.token_broker) or answers.single_app:
            _err("error: --token-broker needs an https URL (https://host[/path]) and cannot be combined with "
                 "--single-app")
            return 2
        answers.token_broker = args.token_broker.rstrip("/")
        if args.create_app:
            _err("warning: --create-app ignored: in token-broker mode the broker owns the GitHub Apps")
            args.create_app = False
    if not answers.app_slug:
        answers.app_slug = W.default_slug(answers.owner)
    if not answers.single_app and not answers.agent_app_slug:
        answers.agent_app_slug = loader.default_agent_slug(answers.app_slug)
    if answers.single_app:
        _err("warning: single-app mode: the coding agents will run as the ops App. Their comments and pushes are "
             "then indistinguishable from protocol, so squadhq disables every unattended merge (auto, risk_based, "
             "trivial): an approver merges everything. Re-run without --single-app to create the agent App.")
    pack_errors = _check_packs(answers)
    if pack_errors:
        for e in pack_errors:
            _err(e)
        return 2

    hub_dir, needs_scaffold = _resolve_hub_dir(args)
    out_path = args.out or os.path.join(hub_dir, "squadhq.yml")
    write_local = args.apply or args.config_only  # --config-only: local files only, never GitHub
    if needs_scaffold:
        if write_local:
            copied = SC.scaffold(hub_dir)
            _out(f"scaffolded hub in {hub_dir} ({len(copied)} template items; git init, no remote)")
        else:
            _out(f"# would scaffold a new hub in {hub_dir} from the bundled template (git init, no remote)")

    pem_path, client_id, app_url = "", "", ""
    agent_pem, agent_client_id, agent_url = "", "", ""
    if args.create_app and args.apply and not args.config_only:
        is_org = W.owner_is_org(GH, answers.owner)
        tdir = hub_dir if os.path.isdir(os.path.join(hub_dir, "deploy", "github-app")) else SC.template_root()
        kinds = ["ops"] if answers.single_app else ["ops", "agent"]
        for kind in kinds:
            slug = answers.app_slug if kind == "ops" else answers.agent_app_slug
            _out(f"creating the {kind} GitHub App '{slug}' (approve it in the browser) ...")
            conv = AM.run_flow(answers.owner, is_org, slug, answers.hub_repo, open_browser=OPEN_BROWSER,
                               timeout=args.app_timeout, template_path=AM.manifest_template_path(kind, tdir))
            pem = AM.save_pem(args.pem_dir or hub_dir, conv.slug, conv.pem)
            _out(f"created {kind} App {conv.slug} (id {conv.id}); private key saved to {pem} (mode 0600, git-ignored)")
            if kind == "ops":
                pem_path, client_id, app_url, answers.app_slug = pem, conv.client_id, conv.html_url, conv.slug
            else:
                agent_pem, agent_client_id, agent_url = pem, conv.client_id, conv.html_url
                answers.agent_app_slug = conv.slug

    text = W.generate_yaml(answers)
    validate_root = hub_dir if SC.is_hub(hub_dir) else (SC.template_root() or hub_dir)
    errors = W.validate_generated(text, validate_root)
    if errors:
        for e in errors:
            _err(e)
        _err("refusing to write an invalid squadhq.yml; fix the answers above")
        return 1
    if os.path.exists(out_path) and not args.force:
        _out(f"{out_path} exists; keeping it (use --force to overwrite). Generated version:\n")
        _out(text)
    elif write_local:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        _out(f"wrote {out_path}")
    else:
        _out(f"# would write {out_path}:\n")
        _out(text)
    if write_local and SC.is_hub(hub_dir) and os.path.isfile(out_path):
        try:
            rp = RN.plan(loader.load(out_path, hub_root=hub_dir), hub_dir)
            for path in RN.write(rp):
                _out(f"rendered {path}")
        except (loader.ConfigError, ValueError) as e:
            _err(f"render failed: {e}")
            return 1
    if args.config_only:
        _out("\n--config-only: local files only; no GitHub changes. Next: `squadhq init --apply` or `squadhq doctor`.")
        return 0

    cfg, _issues = loader.load_text(text)
    variables = RN.repo_variables(cfg) if cfg else {}
    if args.create_app and not args.apply:
        apps = [answers.app_slug] + ([] if answers.single_app else [answers.agent_app_slug])
        _out(f"\nwould create GitHub App(s) {', '.join(repr(a) for a in apps)} via the manifest flow "
             f"(browser -> github.com/organizations/{answers.owner}/settings/apps/new -> localhost callback)")
    flows = os.path.join(hub_dir, "config", "flows.yml")
    if not os.path.isfile(flows):
        tr = SC.template_root()
        flows = os.path.join(tr, "config", "flows.yml") if tr else ""
    plan = W.setup_plan(answers, GH if GH.available() else None, scope=args.scope, pem_path=pem_path,
                        client_id=client_id, flows_path=flows or None, variables=variables,
                        agent_pem_path=agent_pem, agent_client_id=agent_client_id)
    if not pem_path and cfg:
        _out("\n# after creating the App(s), store their ids and keys:")
        for line in RN.app_commands(cfg, args.scope):
            _out(f"#   {line}")
    if args.apply:
        if not GH.authenticated():
            _err("gh is not installed or not authenticated: run `gh auth login`, or re-run without --apply "
                 "and execute the printed commands yourself")
            return 1
        failed = 0
        for o in plan.apply(GH):
            mark = "✅" if o.result.ok else "❌"
            failed += 0 if o.result.ok else 1
            _out(f"{mark} {o.step.description}" + ("" if o.result.ok else f": {o.result.stderr.strip()[:200]}"))
        _out(f"\n{len(plan.steps) - failed}/{len(plan.steps)} GitHub steps succeeded")
    else:
        _out(f"\n# dry run: {len(plan.steps)} GitHub step(s) (re-run with --apply to execute):")
        for st in plan.steps:
            _out(f"  {st.display()}")
    _out("\nnext steps:")
    if agent_url:
        _out(f"\ninstall the agent App too: {agent_url}/installations/new")
    for line in W.next_steps(answers, app_url):
        _out(f"  {line}")
    return 0


# --------------------------------------------------------------------------
# repo-settings
# --------------------------------------------------------------------------

def cmd_repo_settings(args: argparse.Namespace) -> int:
    """Admin repository settings only (hosted / token-broker installs): no squadhq.yml, no Apps,
    no secrets or variables. Dry run unless --apply; uses the caller's own ``gh`` login."""
    from squadhq.config import github_ops as ops

    bad = [r for r in [args.hub, *(args.project or [])] if not loader.REPO_RE.match(r)]
    if bad:
        _err(f"error: not owner/name: {', '.join(bad)}")
        return 2
    projects = list(dict.fromkeys(args.project or []))
    authed = GH.authenticated()
    if args.apply and not authed:
        _err("gh is not installed or not authenticated: run `gh auth login` (repo admin on the hub and projects)")
        return 1
    plan, notes = ops.repo_settings_plan(GH if authed else None, args.hub, projects)
    for n in notes:
        _out(f"• {n}")
    if not plan.steps:
        _out("nothing to change: every setting is already in place")
        return 0
    if not args.apply:
        if not authed:
            _out("# gh not authenticated: current settings were not read, so the plan lists every step")
        _out(f"# dry run: {len(plan.steps)} GitHub step(s) (re-run with --apply to execute):")
        for st in plan.steps:
            _out(f"  {st.description}\n    {st.display()}")
        return 0
    failed = 0
    for o in plan.apply(GH):
        if o.result.ok:
            _out(f"✅ {o.step.description}")
        elif o.step.kind == "ruleset" and ops.is_plan_limit(o.result):
            _out(f"⚠️  {o.step.description}: {ops.RULESET_PLAN_HINT}")
        else:
            failed += 1
            _out(f"❌ {o.step.description}: {o.result.stderr.strip()[:200]}")
    _out(f"\n{len(plan.steps) - failed}/{len(plan.steps)} GitHub steps succeeded or were skipped with a warning")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# onboard
# --------------------------------------------------------------------------

def cmd_onboard(args: argparse.Namespace) -> int:
    root = args.root or hub_root()
    if not require_hub(root):
        return 2
    name = args.name or args.repo.split("/")[-1].replace("-", " ").title()
    mode = "apply" if args.apply else "audit"
    cmd = [sys.executable, "-m", "squadhq.onboard", mode, args.repo, name]
    if not args.apply:
        _out(f"# audit only (read-only). `--apply` also creates labels, opens the onboarding PR and registers "
             f"the project.\n$ {' '.join(cmd)}")
    env = dict(os.environ, SQUADHQ_HOME=root)
    try:
        p = SUBPROCESS_RUN(cmd, cwd=root, env=env, text=True, timeout=args.timeout, check=False)
    except subprocess.TimeoutExpired:
        _err(f"onboard timed out after {args.timeout}s")
        return 124
    if args.apply and p.returncode == 0:
        _out("\nnext: add the project to squadhq.yml `projects:` (with packs) and run `squadhq render`.")
    return int(p.returncode)


# --------------------------------------------------------------------------
# drill
# --------------------------------------------------------------------------

def cmd_drill(args: argparse.Namespace) -> int:
    if _config_missing(args.config or default_config_path()):
        return 2
    loaded = _load(args)
    if loaded is None:
        return 1
    cfg = loaded[0]
    repo = args.repo or (cfg.projects[0].repo if cfg.projects else cfg.hub.repo)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"squadhq drill {stamp}"
    body = (f"{cfg.hub.trigger} This is the automated first-run drill from `squadhq drill`. "
            "Reply with a one-line acknowledgement. Do not change any code.")
    steps = [
        f"gh issue create -R {repo} --title '{title}' --body '<trigger + ack request>'",
        f"poll comments every {args.interval}s for up to {args.timeout}s for a reply from {cfg.hub.bot.login}",
        f"gh issue close <n> -R {repo} --comment 'drill finished'",
    ]
    if not args.apply:
        _out("# drill plan (dry run; --apply executes):")
        for line in steps:
            _out(f"  {line}")
        return 0
    r = GH.run(["issue", "create", "-R", repo, "--title", title, "--body", body])
    if not r.ok:
        _err(f"❌ could not create the drill issue: {r.stderr.strip()}")
        return 1
    url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    number = url.rstrip("/").split("/")[-1]
    _out(f"created {url}; waiting for {cfg.hub.bot.login} ...")
    # REST comments carry the unforgeable `<slug>[bot]` login; a human named
    # like the App never passes. Either App may answer (agents reply in role).
    bots = {cfg.hub.bot.login} | (set() if cfg.hub.single_app else {cfg.hub.agent_bot.login})
    deadline = NOW() + args.timeout
    ok = False
    while NOW() < deadline:
        v = GH.run(["api", f"repos/{repo}/issues/{number}/comments", "--paginate"])
        data = v.json() if v.ok else None
        comments = data if isinstance(data, list) else []
        if any(str((c.get("user") or {}).get("login", "")) in bots for c in comments if isinstance(c, dict)):
            ok = True
            break
        SLEEP(args.interval)
    GH.run(["issue", "close", number, "-R", repo, "--comment",
            "drill passed" if ok else "drill timed out (no reply)"])
    if ok:
        _out(f"✅ drill passed: {cfg.hub.bot.login} replied on {url}")
        return 0
    _out(f"❌ drill failed: no reply from {cfg.hub.bot.login} within {args.timeout}s\n"
         f"      fix: check the Actions tab of {repo} (agents.yml stub) and run `squadhq doctor`")
    return 1


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="squadhq", description="Agent Squad HQ: configure, render and check your squad.")
    ap.add_argument("--version", "-V", action="version", version=f"squadhq {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", "-c", default=argparse.SUPPRESS,
                        help="path to squadhq.yml (default: ./squadhq.yml, $SQUADHQ_CONFIG)")
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="hub root for generated files (default: the config file's directory)")
    ap.add_argument("--config", "-c", default=None, help="path to squadhq.yml (default: ./squadhq.yml, $SQUADHQ_CONFIG)")
    ap.add_argument("--root", default=None, help="hub root for generated files (default: the config file's directory)")
    _sub = ap.add_subparsers(dest="cmd", required=True)

    class _Sub:
        def add_parser(self, name: str, **kw: Any) -> argparse.ArgumentParser:
            return _sub.add_parser(name, parents=[common], **kw)

    sub = _Sub()

    sub.add_parser("version", help="print the version")

    v = sub.add_parser("validate", help="validate squadhq.yml")
    v.add_argument("file", nargs="?")
    v.add_argument("--explain", action="store_true", help="print resolved job tiers and UTC schedules")

    r = sub.add_parser("render", help="generate config/*.json and workflow crons from squadhq.yml")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="exit 1 if generated files are stale (CI)")
    g.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    r.add_argument("--diff", action="store_true", help="print unified diffs")

    d = sub.add_parser("doctor", help="check config, rendered files, secrets, App, rulesets")
    d.add_argument("--offline", action="store_true", help="skip GitHub checks")
    d.add_argument("--check", metavar="ID", help="run a single check by id (see --list-checks)")
    d.add_argument("--list-checks", action="store_true", help="print check ids and exit")

    i = sub.add_parser("init", help="create squadhq.yml and set up GitHub (dry run unless --apply)")
    i.add_argument("--non-interactive", action="store_true")
    i.add_argument("--apply", action="store_true",
                   help="write local files AND execute the GitHub steps (App, secrets, variables, labels, rulesets)")
    i.add_argument("--config-only", action="store_true",
                   help="write local files only (hub scaffold, squadhq.yml, rendered config); never touches GitHub. "
                        "Without --apply or --config-only, init is a dry run that writes nothing")
    i.add_argument("--force", action="store_true", help="overwrite an existing squadhq.yml")
    i.add_argument("--hub-dir", help="hub repository directory to create or reuse (default: the current directory "
                                     "if it is a hub, else ./agent-squad-hq)")
    i.add_argument("--out", help="where to write squadhq.yml (default: <hub-dir>/squadhq.yml)")
    i.add_argument("--owner")
    i.add_argument("--org-name")
    i.add_argument("--hub-repo")
    i.add_argument("--timezone")
    i.add_argument("--working-hours", default="09:00-18:00")
    i.add_argument("--approver", action="append", help="repeatable: @user or @org/team")
    i.add_argument("--operating-model", choices=R.OPERATING_MODELS, default="team")
    i.add_argument("--auth", choices=("api", "subscription"), default="api")
    i.add_argument("--budget", type=float, default=100.0)
    i.add_argument("--project", action="append", help="repeatable: owner/repo")
    i.add_argument("--packs", action="append", help="repeatable: owner/repo=pack1,pack2")
    i.add_argument("--branch", default="main")
    i.add_argument("--detect-packs", action="store_true", help="propose packs from repo contents via gh")
    i.add_argument("--app-slug", help="ops GitHub App slug (default <owner>-squad)")
    i.add_argument("--agent-app-slug", help="agent GitHub App slug (default <ops slug>-agent)")
    i.add_argument("--single-app", action="store_true",
                   help="one App only: agents share the ops identity and nothing merges unattended (not recommended)")
    i.add_argument("--token-broker", metavar="URL",
                   help="token-broker mode (ADR-0011): set SQUADHQ_TOKEN_BROKER instead of creating Apps; workflows "
                        "trade a GitHub OIDC token for App tokens at this https URL (pass --app-slug/--agent-app-slug "
                        "for the broker's Apps)")
    i.add_argument("--create-app", action="store_true",
                   help="create the ops and agent GitHub Apps via the manifest flow (one App with --single-app)")
    i.add_argument("--app-timeout", type=float, default=600.0)
    i.add_argument("--pem-dir", help="where to save the App private key (default: the hub dir, git-ignored)")
    i.add_argument("--scope", choices=("org", "repo"), default="org", help="where secrets/variables go")

    rs = sub.add_parser("repo-settings", help="apply only the admin repo settings (hub Actions access, merge "
                                              "settings, rulesets) with your gh login; dry run unless --apply")
    rs.add_argument("--hub", required=True, metavar="OWNER/HUB", help="the hub repository")
    rs.add_argument("--project", action="append", metavar="OWNER/REPO", help="repeatable: a project repository")
    rs.add_argument("--apply", action="store_true", help="execute the steps (default: print the plan)")

    o = sub.add_parser("onboard", help="onboard a project repo (audit unless --apply)")
    o.add_argument("repo")
    o.add_argument("--name")
    o.add_argument("--apply", action="store_true")
    o.add_argument("--timeout", type=float, default=900.0)

    dr = sub.add_parser("drill", help="first-run drill: open a test ticket and wait for the bot")
    dr.add_argument("--repo")
    dr.add_argument("--apply", action="store_true")
    dr.add_argument("--timeout", type=float, default=900.0)
    dr.add_argument("--interval", type=float, default=20.0)

    for name, help_ in (("server", "run the self-host server (squadhq.server)"),
                        ("skills", "skill catalogue commands (squadhq.skills)"),
                        ("evals", "skill evals (squadhq.skills.evals)")):
        sub.add_parser(name, help=help_, add_help=False).add_argument("rest", nargs=argparse.REMAINDER)

    sub.add_parser("packs", help="stack pack queries: list, validate, files, ci-plan, tools, risk-globs, detect",
                   add_help=False).add_argument("rest", nargs=argparse.REMAINDER)
    return ap


# subcommand -> (module, function, install hint). Imported lazily: these
# packages are optional (server extra) or owned elsewhere. The skills/evals
# ``__main__`` modules call sys.exit at import, so delegate to their ``cli``.
DELEGATES: dict[str, tuple[str, str, str]] = {
    "server": ("squadhq.server.__main__", "main", "install agent-squad-hq[server]"),
    "skills": ("squadhq.skills.cli", "main", "squadhq.skills is not available in this install"),
    "evals": ("squadhq.skills.evals.cli", "main", "squadhq.skills.evals is not available in this install"),
}


def delegate(cmd: str, rest: list[str]) -> int:
    import importlib
    module, func, hint = DELEGATES[cmd]
    try:
        fn = getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError) as e:
        _err(f"squadhq {cmd}: {hint} ({e})")
        return 2
    # skills/evals: usage lines name the CLI command, not ``python -m``.
    code = fn(rest) if cmd == "server" else fn(rest, prog=f"squadhq {cmd}")
    return int(code or 0)


# Commands that only make sense inside a hub checkout (config/flows.yml present).
_HUB_DELEGATES = {"skills": ("--root",), "evals": ("--hub",), "packs": ()}


def require_hub(root: str) -> bool:
    from squadhq.config.scaffold import NOT_A_HUB, is_hub
    if is_hub(root):
        return True
    _err(f"{NOT_A_HUB} (looked in {root})")
    return False


def _wants_help(rest: list[str]) -> bool:
    return any(a in ("-h", "--help") for a in rest)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in ("--version", "-V"):
        _out(f"squadhq {__version__}")
        return 0
    if raw and (raw[0] in DELEGATES or raw[0] == "packs"):  # pass everything (incl. -h) straight through
        cmd, rest = raw[0], raw[1:]
        explicit = any(a.split("=")[0] in _HUB_DELEGATES.get(cmd, ()) for a in rest)
        if cmd in _HUB_DELEGATES and not explicit and not _wants_help(rest) and rest and not require_hub(hub_root()):
            return 2
        if cmd == "packs":
            from squadhq import packs
            return packs.main(rest)
        return delegate(cmd, rest)
    args = build_parser().parse_args(raw)
    if args.cmd == "version":
        _out(f"squadhq {__version__}")
        return 0
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "validate": cmd_validate, "render": cmd_render, "doctor": cmd_doctor, "init": cmd_init,
        "onboard": cmd_onboard, "drill": cmd_drill, "repo-settings": cmd_repo_settings,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
