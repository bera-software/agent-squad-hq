"""``squadhq render``: squadhq.yml -> the files the Actions runtime reads.

Outputs (shapes match what the runtime reads today; keys are only added):

* ``config/models.json``   ``tiers.*`` (resolve_model) and ``hub_loops.*`` (agent_runner)
* ``config/auth.json``     ``coding_agents_auth`` / ``fallback_on_capacity_trip`` (capacity_gate)
* ``config/projects.json`` ``projects[]`` (+ ``packs``, ``pack_roots``, ``environments``, ``merge``)
* ``.github/workflows/*.yml`` cron lines marked ``# squadhq:schedule:<key>`` (UTC)

plus the list of repository variables and secrets the workflows expect.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import registry as R
from .model import Config, ModelRef, Project, Tier
from .schedules import ScheduleError, zone_offset
from .schedules import render as render_schedule

GENERATED = "Rendered by `squadhq render` from squadhq.yml. Do not edit by hand: edit squadhq.yml and re-render."
MARKER_RE = re.compile(
    r"""^(?P<indent>\s*)-\s*cron:\s*(?P<q>["']?)(?P<expr>[^"'#\n]*?)(?P=q)\s*#\s*squadhq:schedule:(?P<key>[a-z0-9_]+)\b.*$"""
)


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# models.json
# --------------------------------------------------------------------------

def model_ref(m: ModelRef) -> str:
    """The ref the runtime resolves (``squadhq.providers.resolve``): a bare id
    for Anthropic (resolve's default provider, and what claude-code-action
    takes), ``provider/model`` for everything else, so a non-Anthropic route
    never silently resolves to Anthropic."""
    return m.model if m.provider == "anthropic" else f"{m.provider}/{m.model}"


def _tier_entry(t: Tier, jobs: list[str]) -> dict[str, Any]:
    e: dict[str, Any] = {"primary": model_ref(t.primary)}
    if t.fallback:
        e["fallback"] = model_ref(t.fallback)
    e["jobs"] = jobs
    e["primary_provider"] = t.primary.provider
    if t.fallback:
        e["fallback_provider"] = t.fallback.provider
    return e


def render_models(cfg: Config) -> dict[str, Any]:
    tiers: dict[str, Any] = {}
    loops: dict[str, Any] = {}
    for name, tier in cfg.providers.routing.items():
        code_jobs = [j for j, t in cfg.providers.jobs.items() if t == name and j not in R.HUB_LOOP_JOBS]
        loop_jobs = [j for j, t in cfg.providers.jobs.items() if t == name and j in R.HUB_LOOP_JOBS]
        tiers[name] = _tier_entry(tier, code_jobs)
        if loop_jobs:
            loops[name] = _tier_entry(tier, loop_jobs)
    return {
        "_generated": GENERATED,
        "tiers": tiers,
        "hub_loops": loops,
        "executor": cfg.providers.executor,
        "_notes": [
            "tiers.*.jobs are claude-code-action JOB values (squadhq.resolve_model); hub_loops.*.jobs are "
            "agent_runner modes. Both are generated from providers.routing + providers.jobs in squadhq.yml.",
            "primary/fallback are model refs: a bare id is Anthropic, anything else is provider/model "
            "(squadhq.providers.resolve). *_provider repeats the provider for readers.",
        ],
    }


# --------------------------------------------------------------------------
# auth.json
# --------------------------------------------------------------------------

def render_auth(cfg: Config) -> dict[str, Any]:
    anth = cfg.providers.configs.get("anthropic")
    providers: dict[str, Any] = {}
    for name, pc in cfg.providers.configs.items():
        e: dict[str, Any] = {"auth": pc.auth}
        if pc.secret:
            e["secret"] = pc.secret
        if pc.base_url:
            e["base_url"] = pc.base_url
        if pc.region:
            e["region"] = pc.region
        providers[name] = e
    out: dict[str, Any] = {
        "_generated": GENERATED,
        "coding_agents_auth": anth.auth if anth and anth.auth in ("api", "subscription") else "api",
        "fallback_on_capacity_trip": cfg.providers.fallback_on_capacity_trip,
        "default_provider": cfg.providers.default,
        "executor": cfg.providers.executor,
        "providers": providers,
    }
    if anth and anth.pool:
        # capacity_gate reads these: one breaker per account; the fleet pauses
        # (or fails over) only when every account is tripped.
        out["pool"] = [{"id": a.id, "auth": a.auth, "secret": a.secret} for a in anth.pool]
        out["pool_strategy"] = anth.pool_strategy
    out["_notes"] = [
            "coding_agents_auth: 'api' (default) uses the provider API key secret and is metered. 'subscription' "
            "uses a coding-agent subscription OAuth token instead; whether that is permitted for automated use, its "
            "capacity limits and its expiry are the user's responsibility under the provider's terms.",
            "fallback_on_capacity_trip: when true and the capacity breaker has tripped on subscription auth, jobs "
            "fall back to metered API auth instead of pausing. Default false: a paused job is cheaper than a "
            "surprise bill.",
            "Change auth in squadhq.yml (providers.anthropic.auth, providers.fallback_on_capacity_trip) and run "
            "`squadhq render`, never in workflow files, so every change is a reviewed commit.",
    ]
    if anth and anth.pool:
        out["_notes"].append(
            "pool: each job runs on one account (pool_strategy ordered = first untripped, round_robin = rotating); "
            "a capacity trip pauses only that account, and the fleet pauses (or, with fallback_on_capacity_trip, "
            "fails over to metered API) only when every account is tripped. Rotating several API keys or "
            "organisation accounts is fine; pooling or rotating personal consumer subscriptions for automated use "
            "may violate the provider's terms — that is the user's responsibility. With a pool, set "
            "SQUADHQ_HANDOFF_KEY: the hand-off key no longer derives from a single LLM credential.")
    return out


# --------------------------------------------------------------------------
# projects.json
# --------------------------------------------------------------------------

MERGE_MODE_VALUES = {"approvers": "approver", "risk_based": "risk_based", "auto": "auto"}


def _legacy_merge_mode(mode: str) -> str:
    # router.yml's self-merge job arms auto-merge for "auto"; for every other
    # value it asks squadhq.merge_policy, which reads the `merge` block below:
    # "approver" never auto-merges, "risk_based" merges within merge.risk_based.
    return MERGE_MODE_VALUES[mode]


def _project_entry(cfg: Config, p: Project) -> dict[str, Any]:
    mode = p.merge_mode or cfg.policies.merge.mode
    envs = p.environments
    prod = envs.get("production")
    stg = envs.get("staging")
    e: dict[str, Any] = {
        "name": p.name,
        "repo": p.repo,
        "merge_mode": _legacy_merge_mode(mode),
        "integration_branch": p.branch,
        "frontend_url": prod.url if prod else "",
        "staging_url": (stg.api_url or stg.url) if stg else "",
    }
    if stg and stg.url:
        e["staging_frontend_url"] = stg.url
    if stg:
        for k, u in sorted(stg.urls.items()):
            e[f"staging_{k}_url"] = u
    e["keywords"] = list(p.product.keywords)
    e["markets"] = list(p.product.markets)
    e["notes"] = p.product.notes
    e["packs"] = list(p.packs)
    e["pack_roots"] = dict(p.pack_roots)
    e["quality"] = {"arch": p.quality.arch, "lint": p.quality.lint, "changed_coverage": p.quality.changed_coverage}
    e["environments"] = {
        n: {k: v for k, v in (("url", env.url), ("api_url", env.api_url), ("urls", env.urls),
                              ("deploy", env.deploy), ("approval", env.approval)) if v}
        for n, env in envs.items()
    }
    e["merge"] = {
        "mode": mode,
        "approvers": list(cfg.org.approvers),
        "always_human": list(cfg.policies.merge.always_human),
    }
    if mode == "risk_based":
        rb = cfg.policies.merge.risk_based
        e["merge"]["risk_based"] = {"max_risk": rb.max_risk, "require_checks": list(rb.require_checks),
                                    "require_security_agent": rb.require_security_agent}
    if p.product.brief_file:
        e["brief_file"] = p.product.brief_file
    switches = {**p.agents, **p.skills}
    disabled = sorted(n for n, a in switches.items() if not a.enabled)
    enabled = sorted(n for n, a in switches.items() if a.enabled)
    if disabled or enabled:
        e["agents"] = {"enabled": enabled, "disabled": disabled}
    budget = p.budget_usd if p.budget_usd is not None else cfg.budgets.per_project_usd.get(
        p.name, cfg.budgets.per_project_usd.get("default"))
    if budget is not None:
        e["budget_usd"] = budget
    return e


def render_projects(cfg: Config) -> dict[str, Any]:
    return {
        "_generated": GENERATED,
        "_docs": {
            "merge_mode": "Per project, rendered from squadhq.yml (policies.merge.mode / projects[].merge_mode). "
                          "'approver': a human in the approver set merges; nothing auto-merges. 'risk_based': the "
                          "self-merge job merges a bot PR only when squadhq.merge_policy finds it within "
                          "merge.risk_based (max_risk, passing checks) and outside merge.always_human; otherwise "
                          "an approver merges. 'auto': the self-merge job merges as soon as the Reviewer applies "
                          "'ready-to-merge', leaving branch protection's required checks as the only gate. Never "
                          "use 'auto' unless the hub's default branch is protected and SAST, SCA and secret "
                          "scanning are required checks.",
            "integration_branch": "The branch the Engineer and Architect branch from and target. In a "
                                  "single-branch model it is also the production branch.",
            "packs": "Stack packs from packs/<id>/ that apply to this project (see packs/README.md). Order does "
                     "not matter.",
            "pack_roots": "Optional per-pack working directory, relative to the repo root, when a pack's code "
                          "does not live at the pack's default root.",
            "quality": "Quality gates (squadhq.yml projects[].quality): arch auto|required|off, lint "
                       "auto|default|off, changed_coverage enforce|report|off. Read by `squadhq.packs ci-plan`; "
                       "see docs/concepts/quality-gates.md.",
            "frontend_url": "Public URL of the product, used by the SEO and Growth agents. Optional.",
            "keywords": "Search terms target users would use; used by the SEO and Growth agents.",
            "markets": "Jurisdictions and audiences; used by the Legal and Growth agents.",
            "notes": "Plain-language positioning: what the product does, who it is for, what it must never "
                     "claim. Read by the reporting agents and the Product Owner.",
        },
        # Read by squadhq.stuck_sweeper.wip_limit() (falls back to flows.yml).
        "policies": {"wip_limit": cfg.policies.wip_limit, "review_fix_rounds": cfg.policies.review_fix_rounds},
        "projects": [_project_entry(cfg, p) for p in cfg.projects],
    }


# --------------------------------------------------------------------------
# schedules / workflows
# --------------------------------------------------------------------------

def render_schedules(cfg: Config, ref: date | None = None) -> dict[str, tuple[list[str], str]]:
    out: dict[str, tuple[list[str], str]] = {}
    for key, spec in cfg.schedules.items():
        out[key] = render_schedule(spec, cfg.org.timezone, cfg.org.dst, cfg.org.working_hours,
                                   cfg.org.working_days, ref)
    return out


def rewrite_workflow(text: str, schedules: dict[str, tuple[list[str], str]]) -> tuple[str, set[str]]:
    """Replace each run of consecutive marker lines for one key with the
    rendered crons. Returns ``(new_text, keys_seen)``; unknown keys are left
    untouched."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        m = MARKER_RE.match(lines[i].rstrip("\r\n"))
        if not m or m.group("key") not in schedules:
            if m:
                seen.add(m.group("key"))
            out.append(lines[i])
            i += 1
            continue
        key, indent = m.group("key"), m.group("indent")
        seen.add(key)
        j = i
        while j < len(lines):
            mj = MARKER_RE.match(lines[j].rstrip("\r\n"))
            if not mj or mj.group("key") != key:
                break
            j += 1
        nl = "\r\n" if lines[i].endswith("\r\n") else "\n"
        crons, comment = schedules[key]
        for c in crons:
            out.append(f'{indent}- cron: "{c}"  # squadhq:schedule:{key} ({comment}){nl}')
        i = j
    return "".join(out), seen


# --------------------------------------------------------------------------
# repository variables / secrets
# --------------------------------------------------------------------------

def repo_variables(cfg: Config) -> dict[str, str]:
    v = {
        "SQUADHQ_HUB_REPO": cfg.hub.repo,
        "SQUADHQ_HUB_REF": cfg.hub.ref,
        "SQUADHQ_ORG": cfg.owner,
        "SQUADHQ_BOT_LOGIN": cfg.hub.bot.login,
        "SQUADHQ_TRIGGER": cfg.hub.trigger,
        "SQUADHQ_DISABLED_AGENTS": ",".join(cfg.disabled_job_keys()),
        "SQUADHQ_MONTHLY_BUDGET_USD": _num(cfg.budgets.monthly_usd),
        "SQUADHQ_TRUSTED_ASSOCIATIONS": ",".join(cfg.policies.allowed_associations),
        "SQUADHQ_GATES": ",".join(cfg.security.gates),
        "SQUADHQ_DEBUG_TRANSCRIPTS": "true" if cfg.security.show_agent_transcripts else "false",
        "SQUADHQ_TIMEZONE": cfg.org.timezone,
        "SQUADHQ_WIP_LIMIT": str(cfg.policies.wip_limit),
        "SQUADHQ_REVIEW_FIX_ROUNDS": str(cfg.policies.review_fix_rounds),
        "SQUADHQ_MERGE_MODE": cfg.policies.merge.mode,
        "SQUADHQ_APPROVERS": ",".join(cfg.org.approvers),
        "SQUADHQ_NOTIFY_ESCALATIONS": ";".join(str(c) for c in cfg.notifications.escalations),
        "SQUADHQ_NOTIFY_DIGEST": ";".join(str(c) for c in cfg.notifications.digest),
    }
    if cfg.hub.bot.client_id:
        v["SQUADHQ_APP_CLIENT_ID"] = cfg.hub.bot.client_id
    if not cfg.hub.single_app:
        v["SQUADHQ_AGENT_BOT_LOGIN"] = cfg.hub.agent_bot.login
        if cfg.hub.agent_bot.client_id:
            v["SQUADHQ_AGENT_APP_CLIENT_ID"] = cfg.hub.agent_bot.client_id
    if cfg.hub.token_broker:
        v["SQUADHQ_TOKEN_BROKER"] = cfg.hub.token_broker
        if cfg.hub.token_broker_audience:
            v["SQUADHQ_TOKEN_BROKER_AUDIENCE"] = cfg.hub.token_broker_audience
    return v


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def required_secrets(cfg: Config) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not cfg.hub.token_broker:  # token-broker mode stores no App private key (ADR-0011)
        out.append(("SQUADHQ_APP_PRIVATE_KEY", "ops GitHub App private key (PEM): routing, state, merges"))
        if not cfg.hub.single_app:
            out.append(("SQUADHQ_AGENT_APP_PRIVATE_KEY", "agent GitHub App private key (PEM): coding agents"))
    used = {t.primary.provider for t in cfg.providers.routing.values()} | {
        t.fallback.provider for t in cfg.providers.routing.values() if t.fallback}
    for name, pc in cfg.providers.configs.items():
        if name not in used and name != cfg.providers.default:
            continue
        if pc.secret:
            out.append((pc.secret, f"{name} credential ({pc.auth})"))
    anth = cfg.providers.configs.get("anthropic")
    if anth and anth.auth == "subscription" and "anthropic" in used:
        # hub loops call the raw API; subscription auth only covers coding agents
        out.append(("ANTHROPIC_API_KEY", "anthropic API key for hub loops (subscription covers coding agents only)"))
    out.extend(pool_secrets(cfg))
    uniq: dict[str, str] = {}
    for n, purpose in out:
        uniq.setdefault(n, purpose)
    return list(uniq.items())


def pool_secrets(cfg: Config) -> list[tuple[str, str]]:
    """Pool account secrets plus the hand-off key a pool requires (empty without a pool)."""
    anth = cfg.providers.configs.get("anthropic")
    if not anth or not anth.pool:
        return []
    out = [(a.secret, f"pool account {a.id} ({a.auth}) for coding agents") for a in anth.pool]
    out.append(("SQUADHQ_HANDOFF_KEY", "hand-off key (required with an account pool): any random 32+ byte string"))
    return out


def pool_secret_commands(cfg: Config, scope: str = "org") -> list[str]:
    """``gh secret set`` lines for the pool (values are read from stdin, never put on the command line)."""
    where = f"--org {cfg.owner} --visibility all" if scope == "org" else f"-R {cfg.hub.repo}"
    out = []
    for name, _purpose in pool_secrets(cfg):
        if name == "SQUADHQ_HANDOFF_KEY":
            out.append(f"openssl rand -hex 32 | gh secret set {name} {where}")
        elif name.startswith("CLAUDE_CODE_OAUTH_TOKEN"):
            out.append(f"gh secret set {name} {where}   # paste the output of `claude setup-token` for this account")
        else:
            out.append(f"gh secret set {name} {where}   # paste this account's API key")
    return out


RESERVED = "reserved (server mode only): validated, not enforced in Actions mode"


def reserved_notes(cfg: Config) -> list[str]:
    """Settings that validate but that the Actions runtime does not enforce
    (yet). Only reported when set to something other than the default, so a
    plain config renders without noise. Shown by render and `doctor --check reserved`."""
    out: list[str] = []
    b = cfg.budgets
    if b.per_project_usd or any(p.budget_usd is not None for p in cfg.projects):
        out.append(f"budgets.per_project_usd / projects[].budget_usd: {RESERVED}; Actions mode enforces only "
                   "budgets.monthly_usd (SQUADHQ_MONTHLY_BUDGET_USD)")
    if b.per_run_usd:
        out.append(f"budgets.per_run_usd: {RESERVED}")
    if b.on_exceed != "pause":
        out.append(f"budgets.on_exceed: {b.on_exceed!r} is {RESERVED}; Actions mode always pauses at the cap")
    if cfg.policies.release.strategy != "merge_is_prod":
        out.append(f"policies.release.strategy: {cfg.policies.release.strategy!r} is {RESERVED}")
    if cfg.providers.executor not in R.ACTIONS_EXECUTORS:
        out.append(f"providers.executor: {cfg.providers.executor!r} is {RESERVED}; Actions mode runs claude-code")
    if cfg.org.operating_model != "team":
        out.append(f"org.operating_model: {RESERVED}")
    modes = {p.merge_mode or cfg.policies.merge.mode for p in cfg.projects} or {cfg.policies.merge.mode}
    rb = cfg.policies.merge.risk_based
    if "risk_based" in modes and rb.require_security_agent:
        out.append("policies.merge.risk_based.require_security_agent: reserved (server mode only); in Actions "
                   "mode the security gates in security.gates are the check")
    if "pii" in cfg.policies.merge.always_human:
        out.append("policies.merge.always_human: 'pii' has no path detector yet; it does not block a merge")
    for where, chans in (("escalations", cfg.notifications.escalations), ("digest", cfg.notifications.digest)):
        other = [str(c) for c in chans if c.kind != "github_issue"]
        if other:
            out.append(f"notifications.{where}: {', '.join(other)} rendered to SQUADHQ_NOTIFY_"
                       f"{where.upper()}; Actions workflows post github_issue only (send others with "
                       "`python -m squadhq.notify send`, or run server mode)")
    return out


def model_refs(cfg: Config) -> list[tuple[str, ModelRef]]:
    out: list[tuple[str, ModelRef]] = []
    for name, t in cfg.providers.routing.items():
        out.append((f"providers.routing.{name}.primary", t.primary))
        if t.fallback:
            out.append((f"providers.routing.{name}.fallback", t.fallback))
    return out


# --------------------------------------------------------------------------
# plan / apply / check
# --------------------------------------------------------------------------

@dataclass
class FileChange:
    path: str  # relative to root
    old: str | None
    new: str

    @property
    def changed(self) -> bool:
        return self.old != self.new

    def diff(self) -> str:
        return "".join(difflib.unified_diff((self.old or "").splitlines(keepends=True),
                                            self.new.splitlines(keepends=True),
                                            f"a/{self.path}", f"b/{self.path}"))


@dataclass
class RenderPlan:
    root: str
    files: list[FileChange] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    secrets: list[tuple[str, str]] = field(default_factory=list)
    schedules: dict[str, tuple[list[str], str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def stale(self) -> list[FileChange]:
        return [f for f in self.files if f.changed]


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def plan(cfg: Config, root: str, ref: date | None = None) -> RenderPlan:
    rp = RenderPlan(root=root)
    for rel, obj in (("config/models.json", render_models(cfg)), ("config/auth.json", render_auth(cfg)),
                     ("config/projects.json", render_projects(cfg))):
        rp.files.append(FileChange(rel, _read(os.path.join(root, rel)), dump_json(obj)))
    try:
        rp.schedules = render_schedules(cfg, ref)
    except ScheduleError as e:
        raise ValueError(f"schedules: {e.message} ({e.hint})") from e
    wf_dir = os.path.join(root, ".github", "workflows")
    seen: set[str] = set()
    if os.path.isdir(wf_dir):
        for name in sorted(os.listdir(wf_dir)):
            if not name.endswith((".yml", ".yaml")):
                continue
            rel = f".github/workflows/{name}"
            old = _read(os.path.join(root, rel)) or ""
            new, keys = rewrite_workflow(old, rp.schedules)
            seen |= keys
            if keys:
                rp.files.append(FileChange(rel, old, new))
    missing = sorted(set(rp.schedules) - seen)
    if missing:
        rp.warnings.append(
            f"{len(missing)} schedule(s) have no `# squadhq:schedule:<key>` marker in .github/workflows: "
            + ", ".join(missing))
    for key in sorted(seen - set(rp.schedules)):
        rp.warnings.append(f"workflow marker squadhq:schedule:{key} has no schedule in squadhq.yml (left as is)")
    rp.warnings.extend(f"reserved: {n}" for n in reserved_notes(cfg))
    if cfg.hub.single_app:
        rp.warnings.append("single-app mode: agents share the ops identity; auto, risk_based and trivial merges "
                           "need an approver. Create the agent App to enable them.")
    off = zone_offset(cfg.org.timezone, cfg.org.dst, ref)
    if off.has_dst:
        rp.warnings.append(f"DST: {cfg.org.timezone} rendered at {off.label()} (org.dst={cfg.org.dst}); "
                           f"scheduled jobs {off.caveat()}.")
    rp.variables = repo_variables(cfg)
    rp.secrets = required_secrets(cfg)
    return rp


def write(rp: RenderPlan) -> list[str]:
    written = []
    for f in rp.stale:
        full = os.path.join(rp.root, f.path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(f.new)
        written.append(f.path)
    return written


def app_commands(cfg: Config, scope: str = "org") -> list[str]:
    """``gh`` lines that wire both Apps (client ids are variables, keys are secrets).
    None in token-broker mode: the broker holds the keys."""
    if cfg.hub.token_broker:
        return []
    where = f"--org {cfg.owner} --visibility all" if scope == "org" else f"-R {cfg.hub.repo}"
    apps = [("SQUADHQ_APP", cfg.hub.bot)] + ([] if cfg.hub.single_app else [("SQUADHQ_AGENT_APP", cfg.hub.agent_bot)])
    out = []
    for prefix, bot in apps:
        body = shlex.quote(bot.client_id or f"<{bot.app} client id>")
        out.append(f"gh variable set {prefix}_CLIENT_ID {where} --body {body}")
        out.append(f"gh secret set {prefix}_PRIVATE_KEY {where} < {bot.app}.private-key.pem")
    return out


def variable_commands(variables: dict[str, str], repo: str) -> list[str]:
    import shlex
    return [f"gh variable set {k} -R {repo} --body {shlex.quote(v)}" for k, v in variables.items()]
