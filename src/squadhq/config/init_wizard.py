"""``squadhq init``: interview (or flags) -> squadhq.yml -> GitHub setup plan.

Nothing touching GitHub runs without ``--apply``; without it the full plan
(every ``gh`` command) is printed instead.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from squadhq.paths import hub_root

from . import github_ops as ops
from .gh import Gh
from . import registry as R
from .loader import default_agent_slug, load_text

Ask = Callable[[str, str], str]


@dataclass
class ProjectAnswer:
    repo: str
    name: str = ""
    branch: str = "main"
    packs: list[str] = field(default_factory=list)
    pack_roots: dict[str, str] = field(default_factory=dict)


@dataclass
class PoolAnswer:
    """One coding-agent account of ``providers.anthropic.pool``."""
    id: str
    auth: str = "api"  # api | subscription
    secret: str = ""  # empty = the loader's default for its position


@dataclass
class HubLoopAnswer:
    """Model routing for the hub's scheduled loops (brief, rollup, growth, ...), on any provider.

    They get their own tiers (``hub-heavy``, ``hub-light``) so coding agents keep Claude models,
    and fall back to Claude so a provider outage does not stop them."""
    provider: str  # a registry provider id, e.g. openai, gemini, azure_openai, bedrock, openrouter
    heavy: str  # model id (Azure: deployment name)
    light: str
    base_url: str = ""
    region: str = ""


@dataclass
class Answers:
    org_name: str
    owner: str
    hub_repo: str
    timezone: str = "UTC"
    working_hours: str = "09:00-18:00"
    approvers: list[str] = field(default_factory=list)
    operating_model: str = "team"
    auth: str = "api"
    monthly_usd: float = 100.0
    app_slug: str = ""
    agent_app_slug: str = ""
    single_app: bool = False
    projects: list[ProjectAnswer] = field(default_factory=list)
    token_broker: str = ""  # https URL: tokens via OIDC from a broker, no App keys (ADR-0011)
    pool: list[PoolAnswer] = field(default_factory=list)  # empty = the single `auth` credential
    pool_strategy: str = "ordered"  # ordered | round_robin
    heavy_model: str = "claude-sonnet-5-5"  # coding agents' tiers (Claude Code)
    light_model: str = "claude-haiku-5-5"
    hub_loops: HubLoopAnswer | None = None  # None: hub loops share the coding tiers


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect_timezone(env: dict[str, str] | None = None, localtime: str = "/etc/localtime") -> str:
    env = dict(os.environ) if env is None else env
    tz = env.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath(localtime)
    except OSError:  # pragma: no cover - realpath rarely raises
        return "UTC"
    marker = "zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1]
    return "UTC"


def detect_owner(gh: Gh) -> str:
    r = gh.run(["repo", "view", "--json", "owner", "--jq", ".owner.login"], timeout=20)
    return r.stdout.strip() if r.ok else ""


def detect_login(gh: Gh) -> str:
    r = gh.run(["api", "user", "--jq", ".login"], timeout=20)
    return r.stdout.strip() if r.ok else ""


def owner_is_org(gh: Gh, owner: str) -> bool:
    r = gh.run(["api", f"users/{owner}", "--jq", ".type"], timeout=20)
    return not r.ok or r.stdout.strip() != "User"


def default_branch(gh: Gh, repo: str) -> str:
    r = gh.run(["api", f"repos/{repo}", "--jq", ".default_branch"], timeout=20)
    return r.stdout.strip() if r.ok and r.stdout.strip() else "main"


def _list_dir(gh: Gh, repo: str, path: str = "") -> list[dict[str, Any]]:
    r = gh.run(["api", f"repos/{repo}/contents/{path}".rstrip("/")], timeout=30)
    data = r.json() if r.ok else None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def detect_remote_packs(gh: Gh, repo: str, root: str | None = None) -> tuple[list[str], dict[str, str]]:
    """Propose packs by listing the repo root and its top-level directories
    through the contents API (no clone)."""
    from squadhq import packs as P
    packs, _errs = P.load_all(root)
    if not packs:
        return [], {}
    top = _list_dir(gh, repo)
    candidates: list[tuple[str, list[str]]] = [(".", [str(d.get("name")) for d in top if d.get("type") == "file"])]
    for d in top:
        name = str(d.get("name", ""))
        if d.get("type") == "dir" and not name.startswith(".") and name not in ("node_modules", "vendor", "docs"):
            candidates.append((name, [str(x.get("name")) for x in _list_dir(gh, repo, name) if x.get("type") == "file"]))
    chosen: list[str] = []
    roots: dict[str, str] = {}
    for pack in packs.values():
        for rel, files in candidates:
            if any(fnmatch.fnmatch(f, pat) for pat in pack.detect_files for f in files):
                chosen.append(pack.id)
                if rel != pack.root:
                    roots[pack.id] = rel
                break
    return chosen, roots


# --------------------------------------------------------------------------
# YAML generation (commented, round-trips through the validator)
# --------------------------------------------------------------------------

def default_slug(owner: str) -> str:
    """``<owner>-squad``, lowercased and trimmed to GitHub's 34-char App slug limit."""
    base = re.sub(r"[^a-z0-9-]+", "-", owner.lower()).strip("-") or "acme"
    return f"{base[:28].rstrip('-')}-squad"


def _q(s: str) -> str:
    return json.dumps(s)


def _flow(items: list[str]) -> str:
    return "[" + ", ".join(_q(i) for i in items) + "]"


def generate_yaml(a: Answers) -> str:
    lines = [
        "# yaml-language-server: $schema=./schema/squadhq.schema.json",
        "# Generated by `squadhq init`. See squadhq.example.yml for every option.",
        "version: 1",
        "",
        "org:",
        f"  name: {_q(a.org_name)}",
        f"  timezone: {a.timezone}",
        f"  working_hours: {_q(a.working_hours)}",
        f"  approvers: {_flow(a.approvers)}",
        f"  operating_model: {a.operating_model}",
        "",
        "hub:",
        f"  repo: {a.hub_repo}",
        "  ref: main",
    ]
    ops_slug = a.app_slug or default_slug(a.owner)
    lines += ["  bot:", f"    app: {ops_slug}   # OPS GitHub App: routing, state, merges; login is <app>[bot]"]
    if a.single_app:
        lines += ["  single_app: true   # agents share the ops identity: every merge needs an approver"]
    else:
        lines += ["  agent_bot:", f"    app: {a.agent_app_slug or default_agent_slug(ops_slug)}   "
                  "# AGENT GitHub App the coding agents run as"]
    if a.token_broker:
        lines += [f"  token_broker: {a.token_broker}   # App tokens via OIDC from this broker; no App keys here"]
    lines += [
        "",
        "providers:",
        "  default: anthropic",
        "  anthropic:",
        f"    auth: {a.auth}               # api | subscription",
    ]
    if a.pool:
        lines.append("    pool:                        # coding agents: one account per job (docs: account pool)")
        for acct in a.pool:
            secret = f", secret: {acct.secret}" if acct.secret else ""
            lines.append(f"      - {{ id: {acct.id}, auth: {acct.auth}{secret} }}")
        lines.append(f"    pool_strategy: {a.pool_strategy}   # ordered | round_robin")
    hl = a.hub_loops
    if hl and hl.provider != "anthropic":
        extra = "".join(f", {k}: {_q(val)}" for k, val in (("base_url", hl.base_url), ("region", hl.region)) if val)
        lines.append(f"  {hl.provider}: {{ auth: api{extra} }}   # hub loops (scheduled jobs) only")
    heavy_fb = "claude-sonnet-5" if a.heavy_model != "claude-sonnet-5" else "claude-sonnet-5-5"
    light_fb = "claude-sonnet-5-5" if a.light_model != "claude-sonnet-5-5" else "claude-haiku-5-5"
    lines += [
        "  routing:",
        f"    heavy: {{ primary: anthropic/{a.heavy_model}, fallback: anthropic/{heavy_fb} }}",
        f"    light: {{ primary: anthropic/{a.light_model}, fallback: anthropic/{light_fb} }}",
    ]
    if hl:
        lines += [
            f"    hub-heavy: {{ primary: {hl.provider}/{hl.heavy}, fallback: anthropic/{a.heavy_model} }}",
            f"    hub-light: {{ primary: {hl.provider}/{hl.light}, fallback: anthropic/{a.light_model} }}",
            "  jobs:                          # hub loops on their own tiers; coding agents stay on Claude",
        ]
        lines += [f"    {job}: hub-{tier}" for job, tier in R.HUB_LOOP_JOBS.items()]
    lines += [
        "",
        "budgets:",
        f"  monthly_usd: {int(a.monthly_usd) if float(a.monthly_usd).is_integer() else a.monthly_usd}",
        "",
        "policies:",
        "  merge: { mode: approvers }     # approvers | risk_based | auto",
        "",
        "projects:" if a.projects else "projects: []",
    ]
    for p in a.projects:
        lines += [f"  - name: {_q(p.name or p.repo.split('/')[-1])}", f"    repo: {p.repo}", f"    branch: {p.branch}",
                  f"    packs: {_flow(p.packs)}"]
        if p.pack_roots:
            lines.append("    pack_roots: {" + ", ".join(f"{k}: {_q(v)}" for k, v in p.pack_roots.items()) + "}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# interview
# --------------------------------------------------------------------------

def _default_ask(question: str, default: str) -> str:  # pragma: no cover - interactive
    v = input(f"{question} [{default}]: ").strip()
    return v or default


def interview(gh: Gh, ask: Ask = _default_ask, *, defaults: Answers | None = None,
              root: str | None = None) -> Answers:
    owner_guess = (defaults.owner if defaults else "") or detect_owner(gh)
    login = detect_login(gh)
    owner = ask("GitHub organisation (or user) that owns your repos", owner_guess or login or "acme")
    org_name = ask("Company / team display name", (defaults.org_name if defaults else "") or owner)
    hub_repo = ask("Hub repository (owner/name)", (defaults.hub_repo if defaults else "") or f"{owner}/agent-squad-hq")
    tz = ask("Timezone (IANA)", (defaults.timezone if defaults else "") or detect_timezone())
    hours = ask("Working hours", (defaults.working_hours if defaults else "") or "09:00-18:00")
    approvers = [x.strip() for x in ask("Approvers (comma-separated @user or @org/team)",
                                       ",".join(defaults.approvers) if defaults and defaults.approvers
                                       else (f"@{login}" if login else f"@{owner}")).split(",") if x.strip()]
    model = "solo" if len(approvers) == 1 and "/" not in approvers[0] else "team"
    auth = ask("Anthropic auth: api (API key) or subscription (Claude Max token)",
               (defaults.auth if defaults else "") or "api")
    slug = ask("Ops GitHub App slug (routing and merges; login is <slug>[bot])",
               (defaults.app_slug if defaults and defaults.app_slug else "") or default_slug(owner))
    two = ask("Separate agent App for the coding agents? (yes = recommended; no = single-app mode, "
              "nothing merges unattended)", "no" if defaults and defaults.single_app else "yes")
    single = two.strip().lower() in ("n", "no", "false", "0")
    agent_slug = "" if single else ask("Agent GitHub App slug",
                                       (defaults.agent_app_slug if defaults and defaults.agent_app_slug else "")
                                       or default_agent_slug(slug))
    budget = ask("Monthly LLM budget in USD", str(int(defaults.monthly_usd) if defaults else 100))
    repos = [x.strip() for x in ask("Project repos (comma-separated owner/name)",
                                    ",".join(p.repo for p in defaults.projects) if defaults else "").split(",")
             if x.strip()]
    projects = []
    for repo in repos:
        packs, roots = detect_remote_packs(gh, repo, root)
        chosen = [x.strip() for x in ask(f"Packs for {repo}", ",".join(packs)).split(",") if x.strip()]
        projects.append(ProjectAnswer(repo=repo, name=repo.split("/")[-1].replace("-", " ").title(),
                                      branch=default_branch(gh, repo), packs=chosen,
                                      pack_roots={k: v for k, v in roots.items() if k in chosen}))
    try:
        budget_f = float(budget)
    except ValueError:
        budget_f = 100.0
    return Answers(org_name=org_name, owner=owner, hub_repo=hub_repo, timezone=tz, working_hours=hours,
                   approvers=approvers, operating_model=model, auth=auth, monthly_usd=budget_f,
                   app_slug=slug, agent_app_slug=agent_slug, single_app=single, projects=projects)


# --------------------------------------------------------------------------
# GitHub setup plan
# --------------------------------------------------------------------------

def setup_plan(a: Answers, gh: Gh | None, *, scope: str = "org", pem_path: str = "",
               client_id: str = "", flows_path: str | None = None,
               variables: dict[str, str] | None = None,
               agent_pem_path: str = "", agent_client_id: str = "") -> ops.Plan:
    plan = ops.Plan()
    vars_ = dict(variables or {})
    for prefix, pem, cid in (("SQUADHQ_APP", pem_path, client_id),
                             ("SQUADHQ_AGENT_APP", agent_pem_path, agent_client_id)):
        if pem:
            ops.plan_secret_from_file(plan, f"{prefix}_PRIVATE_KEY", pem, a.owner, scope, a.hub_repo)
        if cid:
            vars_[f"{prefix}_CLIENT_ID"] = cid
    ops.plan_variables(plan, vars_, a.owner, scope, a.hub_repo)
    try:
        project_labels, hub_labels = ops.label_registry(flows_path)
    except OSError:
        project_labels, hub_labels = [], []
    ops.plan_labels(plan, a.hub_repo, project_labels + hub_labels)
    for p in a.projects:
        ops.plan_labels(plan, p.repo, project_labels)
        ops.plan_ruleset(plan, gh, p.repo)
    return plan


def next_steps(a: Answers, app_html_url: str = "") -> list[str]:
    steps = []
    if a.token_broker:
        steps.append(f"Install the broker's GitHub Apps on the hub and project repos (token broker {a.token_broker})")
    if app_html_url:
        steps.append(f"Install the App on the hub and project repos: {app_html_url}/installations/new")
    steps.append("Add the model credential secret(s) (never paste them into squadhq.yml), e.g.:")
    if a.auth == "subscription":
        steps.append(f"  claude setup-token | gh secret set CLAUDE_CODE_OAUTH_TOKEN --org {a.owner} --visibility all")
    steps.append(f"  gh secret set ANTHROPIC_API_KEY --org {a.owner} --visibility all")
    steps.append("squadhq render && git add squadhq.yml config .github && git commit -m 'chore: squadhq config'")
    for p in a.projects:
        steps.append(f"squadhq onboard {p.repo} --apply")
    steps.append("squadhq doctor")
    if a.projects:
        steps.append(f"squadhq drill --repo {a.projects[0].repo} --apply")
    return steps


def validate_generated(text: str, root: str | None = None) -> list[str]:
    from .loader import known_pack_ids
    _cfg, issues = load_text(text, "squadhq.yml", known_packs=known_pack_ids(root or hub_root()))
    return [str(i) for i in issues if i.level == "error"]


__all__ = ["Answers", "ProjectAnswer", "generate_yaml", "interview", "setup_plan", "next_steps",
           "detect_timezone", "detect_remote_packs"]
