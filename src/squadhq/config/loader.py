"""Parse and validate ``squadhq.yml`` into :class:`squadhq.config.model.Config`.

Every problem is reported as an :class:`Issue` with a dotted path
(``projects[0].environments.staging.url``), a reason and, where possible, a
hint. Errors make the file unusable; warnings (unknown agent/job names,
packs not found locally) never block a render.
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from typing import Any

import yaml

from squadhq.quality.gates import ARCH_MODES, CHANGED_COVERAGE_MODES, LINT_MODES

from . import registry as R
from .model import (
    AgentConf,
    Bot,
    Budgets,
    Channel,
    Config,
    Environment,
    Hub,
    Merge,
    ModelRef,
    Notifications,
    Org,
    Policies,
    PoolAccount,
    Product,
    Project,
    ProviderConf,
    Quality,
    Providers,
    Release,
    RiskBased,
    Security,
    Tier,
    Trackers,
)
from .schedules import ScheduleError, parse_window, zone_offset
from .schedules import parse as parse_schedule
from .schedules import render as render_schedule

REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,33}$")
# Only `<slug>[bot]` is unforgeable: a human may register the bare slug.
BOT_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,33}\[bot\]$")
PACK_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
URL_RE = re.compile(r"^https?://[^\s]+$")
# Token broker (ADR-0011): https only, no credentials, query or fragment.
BROKER_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$")
APPROVER_RE = re.compile(r"^@?[A-Za-z0-9-]+(?:/[A-Za-z0-9._-]+)?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PROVIDER_META_KEYS = {"default", "routing", "jobs", "executor", "fallback_on_capacity_trip"}


@dataclass(frozen=True)
class Issue:
    path: str
    message: str
    hint: str = ""
    level: str = "error"  # error | warning

    def __str__(self) -> str:
        tag = "error" if self.level == "error" else "warning"
        s = f"{tag}: {self.path or '<root>'}: {self.message}"
        if self.hint:
            s += f"\n    hint: {self.hint}"
        return s


class ConfigError(Exception):
    def __init__(self, issues: list[Issue], source: str = "squadhq.yml") -> None:
        self.issues = issues
        self.source = source
        errs = [i for i in issues if i.level == "error"]
        super().__init__(f"{source}: {len(errs)} error(s)\n" + "\n".join(str(i) for i in errs))


def _suggest(word: str, options: Any) -> str:
    m = difflib.get_close_matches(str(word), [str(o) for o in options], n=1, cutoff=0.6)
    return f"did you mean {m[0]!r}?" if m else f"allowed: {', '.join(sorted(str(o) for o in options))}"


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


class _V:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    # -- reporting -------------------------------------------------------
    def err(self, path: str, msg: str, hint: str = "") -> None:
        self.issues.append(Issue(path, msg, hint, "error"))

    def warn(self, path: str, msg: str, hint: str = "") -> None:
        self.issues.append(Issue(path, msg, hint, "warning"))

    # -- primitives --------------------------------------------------------
    def mapping(self, val: Any, path: str, allowed: set[str] | None = None, *,
                required: tuple[str, ...] = ()) -> dict[str, Any]:
        if val is None:
            val = {}
        if not isinstance(val, dict):
            self.err(path, f"expected a mapping, got {type(val).__name__}", "use `key: value` lines indented under it")
            return {}
        for k in val:
            if not isinstance(k, str):
                self.err(_join(path, str(k)), "keys must be strings", "quote the key")
            elif allowed is not None and k not in allowed:
                self.err(_join(path, k), "unknown key", _suggest(k, allowed))
        for k in required:
            if k not in val or val[k] is None:
                self.err(_join(path, k), "is required")
        return {k: v for k, v in val.items() if isinstance(k, str)}

    def string(self, d: dict[str, Any], key: str, path: str, default: str = "", *,
               choices: tuple[str, ...] | None = None, pattern: re.Pattern[str] | None = None,
               hint: str = "") -> str:
        p = _join(path, key)
        if key not in d or d[key] is None:
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            self.err(p, f"expected a string, got {type(v).__name__}",
                     "YAML reads yes/no/on/off as booleans; quote the value" if isinstance(v, bool) else hint)
            return default
        if not isinstance(v, str):
            self.err(p, f"expected a string, got {type(v).__name__} {v!r}",
                     'quote the value (YAML reads 07:30 as a base-60 number), e.g. "07:30"')
            return default
        if choices is not None and v not in choices:
            self.err(p, f"invalid value {v!r}", _suggest(v, choices))
            return default
        if pattern is not None and not pattern.match(v):
            self.err(p, f"invalid value {v!r}", hint)
            return default
        return v

    def boolean(self, d: dict[str, Any], key: str, path: str, default: bool) -> bool:
        if key not in d or d[key] is None:
            return default
        v = d[key]
        if not isinstance(v, bool):
            self.err(_join(path, key), f"expected true/false, got {v!r}")
            return default
        return v

    def integer(self, d: dict[str, Any], key: str, path: str, default: int, lo: int, hi: int) -> int:
        if key not in d or d[key] is None:
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, int):
            self.err(_join(path, key), f"expected an integer, got {v!r}")
            return default
        if not lo <= v <= hi:
            self.err(_join(path, key), f"{v} is out of range", f"must be between {lo} and {hi}")
            return default
        return v

    def number(self, v: Any, path: str, default: float | None = None) -> float | None:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.err(path, f"expected a number, got {v!r}")
            return default
        if v < 0:
            self.err(path, f"{v} must not be negative")
            return default
        return float(v)

    def str_list(self, d: dict[str, Any], key: str, path: str, default: list[str] | None = None, *,
                 choices: tuple[str, ...] | None = None, pattern: re.Pattern[str] | None = None,
                 hint: str = "") -> list[str]:
        p = _join(path, key)
        if key not in d or d[key] is None:
            return list(default or [])
        v = d[key]
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            self.err(p, f"expected a list, got {type(v).__name__}", "use [a, b] or one `- item` per line")
            return list(default or [])
        out: list[str] = []
        for i, item in enumerate(v):
            ip = _join(p, i)
            if not isinstance(item, str):
                self.err(ip, f"expected a string, got {item!r}", "quote the value")
                continue
            if choices is not None and item not in choices:
                self.err(ip, f"invalid value {item!r}", _suggest(item, choices))
                continue
            if pattern is not None and not pattern.match(item):
                self.err(ip, f"invalid value {item!r}", hint)
                continue
            if item in out:
                self.warn(ip, f"duplicate {item!r}")
                continue
            out.append(item)
        return out


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def _org(v: _V, raw: Any) -> Org:
    d = v.mapping(raw, "org", {"name", "timezone", "working_hours", "working_days", "approvers",
                               "operating_model", "dst"}, required=("name",))
    o = Org(name=v.string(d, "name", "org", "unnamed"))
    o.timezone = v.string(d, "timezone", "org", "UTC")
    try:
        zone_offset(o.timezone)
    except ScheduleError as e:
        v.err("org.timezone", e.message, e.hint)
        o.timezone = "UTC"
    o.working_hours = v.string(d, "working_hours", "org", o.working_hours)
    try:
        parse_window(o.working_hours)
    except ScheduleError as e:
        v.err("org.working_hours", e.message, e.hint)
        o.working_hours = "09:00-18:00"
    o.working_days = v.string(d, "working_days", "org", o.working_days)
    from .schedules import parse_days
    try:
        parse_days(o.working_days)
    except ScheduleError as e:
        v.err("org.working_days", e.message, e.hint)
        o.working_days = "mon-fri"
    o.approvers = v.str_list(d, "approvers", "org", pattern=APPROVER_RE,
                             hint='a GitHub login ("@alice") or team ("@acme/leads")')
    if "approvers" in d and not o.approvers:
        v.err("org.approvers", "must list at least one approver", 'e.g. approvers: ["@acme/leads"]')
    elif "approvers" not in d:
        v.err("org.approvers", "is required", 'who may approve merges, e.g. approvers: ["@acme/leads"]')
    o.operating_model = v.string(d, "operating_model", "org", "team", choices=R.OPERATING_MODELS)
    o.dst = v.string(d, "dst", "org", "daylight", choices=R.DST_POLICIES)
    return o


def default_agent_slug(ops_slug: str) -> str:
    """``squadhq-ops`` -> ``squadhq-agent``; ``acme-ops`` -> ``acme-agent``;
    anything else gets ``-agent`` appended (within GitHub's 34-char limit)."""
    base = ops_slug[: -len("-ops")] if ops_slug.endswith("-ops") else ops_slug
    return f"{base[:28].rstrip('-')}-agent"


def _hub(v: _V, raw: Any, org_name: str) -> Hub:
    d = v.mapping(raw, "hub", {"repo", "ref", "bot", "agent_bot", "single_app", "trigger", "token_broker"},
                  required=("repo",))
    repo = v.string(d, "repo", "hub", "", pattern=REPO_RE, hint='owner/name, e.g. "acme/agent-squad-hq"')
    h = Hub(repo=repo or f"{org_name.lower()}/agent-squad-hq")
    h.ref = v.string(d, "ref", "hub", "main")
    h.trigger = v.string(d, "trigger", "hub", "@squad", pattern=re.compile(r"^@[A-Za-z0-9_-]+$"),
                         hint='an @-phrase such as "@squad"')
    braw = d.get("bot")
    if isinstance(braw, str):
        braw = {"app": braw}
    b = v.mapping(braw, "hub.bot", {"app", "login", "client_id"})
    app = v.string(b, "app", "hub.bot", "squadhq-ops", pattern=SLUG_RE,
                   hint="the GitHub App slug: lowercase letters, digits and dashes")
    h.bot = Bot(app=app, login=v.string(b, "login", "hub.bot", f"{app}[bot]", pattern=BOT_LOGIN_RE,
                                        hint='the full App login ending in "[bot]", e.g. "acme-squad[bot]"'),
                client_id=v.string(b, "client_id", "hub.bot", ""))
    araw = d.get("agent_bot")
    if isinstance(araw, str):
        araw = {"app": araw}
    a = v.mapping(araw, "hub.agent_bot", {"app", "login", "client_id"})
    agent_app = v.string(a, "app", "hub.agent_bot", default_agent_slug(app),
                         pattern=SLUG_RE, hint="the agent GitHub App slug: lowercase letters, digits and dashes")
    h.agent_bot = Bot(app=agent_app, login=v.string(a, "login", "hub.agent_bot", f"{agent_app}[bot]",
                                                    pattern=BOT_LOGIN_RE,
                                                    hint='the full App login ending in "[bot]"'),
                      client_id=v.string(a, "client_id", "hub.agent_bot", ""))
    h.single_app = v.boolean(d, "single_app", "hub", False)
    if h.single_app and "agent_bot" in d:
        v.err("hub.agent_bot", "set together with single_app: true",
              "remove hub.agent_bot, or set single_app: false to use a separate agent App")
    elif not h.single_app and h.agent_bot.app == h.bot.app:
        v.err("hub.agent_bot.app", "the agent App must differ from the ops App (hub.bot.app)",
              "use two Apps, or set hub.single_app: true (no unattended merges)")
    if h.single_app:
        v.warn("hub.single_app", "agents share the ops identity: auto, risk_based and trivial merges are disabled",
               "create the agent App (squadhq init --create-app) and set single_app: false")
    _token_broker(v, d.get("token_broker"), h)
    return h


def _token_broker(v: _V, raw: Any, h: Hub) -> None:
    """``hub.token_broker``: an https URL, or ``{url, audience}``."""
    if raw is None:
        return
    if isinstance(raw, str):
        raw = {"url": raw}
    d = v.mapping(raw, "hub.token_broker", {"url", "audience"}, required=("url",))
    hint = "an https URL without credentials, query or fragment, e.g. https://api.squadhq.example"
    h.token_broker = v.string(d, "url", "hub.token_broker", "", pattern=BROKER_URL_RE, hint=hint).rstrip("/")
    h.token_broker_audience = v.string(d, "audience", "hub.token_broker", "",
                                       hint="the audience the broker verifies; default the broker URL")
    if h.token_broker and h.single_app:
        v.err("hub.single_app", "set together with hub.token_broker",
              "the broker mints agent tokens from its own agent App: remove single_app")


def _model_ref(v: _V, raw: Any, path: str, providers: set[str], default_provider: str) -> ModelRef | None:
    if not isinstance(raw, str) or not raw.strip():
        v.err(path, f"expected 'provider/model', got {raw!r}", 'e.g. "anthropic/claude-sonnet-5-5"')
        return None
    if "/" in raw:
        prov, model = raw.split("/", 1)
    else:
        prov, model = default_provider, raw
    if prov not in providers:
        v.err(path, f"provider {prov!r} is not configured under providers",
              f"add `providers.{prov}: {{auth: api}}` or use one of: {', '.join(sorted(providers))}")
    if not model:
        v.err(path, "model id is empty")
        return None
    return ModelRef(prov, model)


def _providers(v: _V, raw: Any, skill_jobs: dict[str, str] | None = None) -> Providers:
    allowed = _PROVIDER_META_KEYS | set(R.PROVIDERS)
    if isinstance(raw, dict):
        for bad, why in R.UNSUPPORTED_PROVIDERS.items():
            if bad in raw:
                v.err(f"providers.{bad}", "unsupported provider", why)
        raw = {k: x for k, x in raw.items() if k not in R.UNSUPPORTED_PROVIDERS}
    d = v.mapping(raw, "providers", allowed)
    configs: dict[str, ProviderConf] = {}
    for name in R.PROVIDERS:
        if name not in d:
            continue
        p = f"providers.{name}"
        auths = R.PROVIDER_AUTH[name]
        keys = {"auth", "base_url", "region", "secret"} | ({"pool", "pool_strategy"} if name == "anthropic" else set())
        pd = v.mapping(d[name], p, keys)
        auth = v.string(pd, "auth", p, auths[0], choices=auths)
        secrets = R.PROVIDER_SECRETS.get(name, {})
        configs[name] = ProviderConf(
            name=name, auth=auth,
            base_url=v.string(pd, "base_url", p, "", pattern=URL_RE, hint="http(s)://host:port"),
            region=v.string(pd, "region", p, ""),
            secret=v.string(pd, "secret", p, secrets.get(auth, ""),
                            pattern=re.compile(r"^[A-Z_][A-Z0-9_]*$"), hint="UPPER_SNAKE secret name"),
        )
        if configs[name].secret and auth == "api" and configs[name].secret not in R.HUB_LOOP_SECRETS:
            v.warn(f"{p}.secret", f"{configs[name].secret} is not passed to hub loops in Actions mode (the "
                   "workflows declare their secrets statically); it works in server mode only",
                   "use " + R.PROVIDER_SECRETS.get(name, {}).get("api", "the provider's default secret name"))
        if name == "anthropic":
            configs[name].pool = _pool(v, pd.get("pool"), f"{p}.pool")
            configs[name].pool_strategy = v.string(pd, "pool_strategy", p, "ordered", choices=POOL_STRATEGIES)
            if "pool_strategy" in pd and not configs[name].pool:
                v.warn(f"{p}.pool_strategy", "has no effect without providers.anthropic.pool")
    if not configs:
        configs["anthropic"] = ProviderConf("anthropic", "api", secret=R.PROVIDER_SECRETS["anthropic"]["api"])
        if d:
            v.err("providers", "no provider configured", "add e.g. `anthropic: {auth: api}`")
    default = v.string(d, "default", "providers", next(iter(configs)))
    if default not in configs:
        v.err("providers.default", f"{default!r} is not configured", _suggest(default, configs))
        default = next(iter(configs))

    routing: dict[str, Tier] = {}
    rd = v.mapping(d.get("routing"), "providers.routing")
    for tier_name in ("heavy", "light"):
        if tier_name not in rd:
            if "routing" in d:
                v.err(f"providers.routing.{tier_name}", "is required",
                      "both heavy and light tiers must define a primary model")
    for tier_name, traw in rd.items():
        tp = f"providers.routing.{tier_name}"
        td = v.mapping(traw, tp, {"primary", "fallback"}, required=("primary",))
        prim = _model_ref(v, td.get("primary"), f"{tp}.primary", set(configs), default) if "primary" in td else None
        fb = _model_ref(v, td["fallback"], f"{tp}.fallback", set(configs), default) if td.get("fallback") else None
        if prim:
            routing[tier_name] = Tier(tier_name, prim, fb)
    if not rd:
        routing = {
            "heavy": Tier("heavy", ModelRef("anthropic", "claude-sonnet-5-5"), ModelRef("anthropic", "claude-sonnet-5")),
            "light": Tier("light", ModelRef("anthropic", "claude-haiku-5-5"), ModelRef("anthropic", "claude-sonnet-5-5")),
        }

    # Defaults: core jobs, then every skill job (tier from its skill.yml model_tier).
    jobs = dict(R.DEFAULT_JOB_TIERS)
    for job, tier in (skill_jobs or {}).items():
        jobs.setdefault(job, tier)
    known_jobs = dict(jobs)
    jd = v.mapping(d.get("jobs"), "providers.jobs")
    for job, tier in jd.items():
        jp = f"providers.jobs.{job}"
        if job not in known_jobs:
            v.warn(jp, "unknown job name (the runtime will never look it up)", _suggest(job, known_jobs))
        if not isinstance(tier, str) or tier not in routing:
            v.err(jp, f"tier {tier!r} is not defined under providers.routing", _suggest(str(tier), routing))
            continue
        jobs[job] = tier
    for job, tier in jobs.items():
        if tier not in routing and "routing" in d:
            v.err(f"providers.jobs.{job}", f"default tier {tier!r} missing from providers.routing",
                  f"define providers.routing.{tier} or map {job} to another tier")
    executor = v.string(d, "executor", "providers", "claude-code", choices=R.EXECUTORS)
    if executor == "claude-code":
        for tname, t in routing.items():
            coding = sorted(j for j, tt in jobs.items() if tt == tname and j not in R.HUB_LOOP_JOBS)
            for ref, where in ((t.primary, "primary"), (t.fallback, "fallback")):
                if ref is not None and ref.provider != "anthropic" and coding:
                    v.warn(f"providers.routing.{tname}.{where}",
                           f"{ref} serves coding jobs ({', '.join(coding[:4])}{'...' if len(coding) > 4 else ''}) "
                           "but executor claude-code runs Anthropic models only",
                           "map those jobs to an Anthropic tier, or use a non-Anthropic model only for hub loops")
                    break
    return Providers(
        default=default, configs=configs, routing=routing, jobs=jobs,
        executor=executor,
        fallback_on_capacity_trip=v.boolean(d, "fallback_on_capacity_trip", "providers", False),
    )


POOL_MAX = 5  # GitHub reusable workflows declare their secrets statically: five slots per auth mode
POOL_STRATEGIES = ("ordered", "round_robin")
POOL_SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_POOL_BASE = {"api": "ANTHROPIC_API_KEY", "subscription": "CLAUDE_CODE_OAUTH_TOKEN"}


def pool_actions_slots(auth: str) -> tuple[str, ...]:
    """Secret names the Actions workflows declare (and can pass to the agent) for ``auth``."""
    base = _POOL_BASE[auth]
    return (base, *(f"{base}_{n}" for n in range(1, POOL_MAX + 1)))


def _pool(v: _V, raw: Any, path: str) -> list[PoolAccount]:
    """``providers.anthropic.pool``: up to five ``{id, auth, secret}`` accounts."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        v.err(path, f"expected a list, got {type(raw).__name__}",
              "one `- {id: main, auth: subscription}` entry per account")
        return []
    if len(raw) > POOL_MAX:
        v.err(path, f"{len(raw)} accounts; at most {POOL_MAX} are supported",
              "GitHub reusable workflows must declare every secret slot statically")
    out: list[PoolAccount] = []
    seen_ids: set[str] = set()
    seen_secrets: set[str] = set()
    for i, e in enumerate(raw[:POOL_MAX]):
        ep = _join(path, i)
        ed = v.mapping(e, ep, {"id", "auth", "secret"}, required=("id",))
        aid = v.string(ed, "id", ep, "", pattern=SLUG_RE, hint="a slug: lowercase letters, digits and dashes")
        auth = v.string(ed, "auth", ep, "subscription", choices=("api", "subscription"))
        secret = v.string(ed, "secret", ep, f"{_POOL_BASE[auth]}_{i + 1}", pattern=POOL_SECRET_RE,
                          hint="UPPER_SNAKE secret name, e.g. CLAUDE_CODE_OAUTH_TOKEN_2")
        if not aid:
            continue
        if aid in seen_ids:
            v.err(_join(ep, "id"), f"duplicate pool account id {aid!r}", "every account needs its own id")
            continue
        if secret in seen_secrets:
            v.err(_join(ep, "secret"), f"secret {secret} is already used by another pool account",
                  "one credential per account")
            continue
        if secret not in pool_actions_slots(auth):
            v.warn(_join(ep, "secret"), f"{secret} is not a declared workflow secret slot: Actions mode skips this "
                   "account (server mode reads it from the environment)",
                   f"use {_POOL_BASE[auth]}_1..{POOL_MAX} for {auth} accounts")
        seen_ids.add(aid)
        seen_secrets.add(secret)
        out.append(PoolAccount(aid, auth, secret))
    return out


def _budgets(v: _V, raw: Any, jobs: dict[str, str]) -> Budgets:
    d = v.mapping(raw, "budgets", {"monthly_usd", "per_project_usd", "per_run_usd", "on_exceed"})
    b = Budgets()
    if d.get("monthly_usd") is not None:
        b.monthly_usd = v.number(d["monthly_usd"], "budgets.monthly_usd", b.monthly_usd) or 0.0
    for key, target in (("per_project_usd", b.per_project_usd), ("per_run_usd", b.per_run_usd)):
        md = v.mapping(d.get(key), f"budgets.{key}")
        for k, val in md.items():
            n = v.number(val, f"budgets.{key}.{k}")
            if n is not None:
                target[k] = n
            if key == "per_run_usd" and k not in jobs and k != "default":
                v.warn(f"budgets.per_run_usd.{k}", "unknown job name", _suggest(k, jobs))
    for k, n in b.per_project_usd.items():
        if n > b.monthly_usd:
            v.warn(f"budgets.per_project_usd.{k}", f"{n} exceeds monthly_usd {b.monthly_usd}")
    b.on_exceed = v.string(d, "on_exceed", "budgets", "pause", choices=R.ON_EXCEED)
    return b


def _trackers(v: _V, raw: Any) -> Trackers:
    d = v.mapping(raw, "trackers", {"default", *R.TRACKERS})
    t = Trackers(default=v.string(d, "default", "trackers", "github", choices=R.TRACKERS))
    required = {"jira": ("site", "project"), "linear": ("team",), "trello": ("board",)}
    for name in R.TRACKERS:
        if name == "github" or name not in d:
            continue
        t.options[name] = v.mapping(d[name], f"trackers.{name}", required=required[name])
    if t.default != "github" and t.default not in t.options:
        v.err("trackers.default", f"{t.default!r} has no settings block", f"add `trackers.{t.default}: {{...}}`")
    return t


def _channels(v: _V, raw: Any, path: str) -> list[Channel]:
    if raw is None:
        return [Channel("github_issue")]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        v.err(path, "expected a list of channels", 'e.g. [github_issue, "slack:#eng-alerts"]')
        return [Channel("github_issue")]
    out: list[Channel] = []
    for i, item in enumerate(raw):
        ip = _join(path, i)
        if not isinstance(item, str):
            v.err(ip, f"expected a string, got {item!r}")
            continue
        kind, _, target = item.partition(":")
        if kind not in R.NOTIFY_KINDS:
            v.err(ip, f"unknown channel kind {kind!r}", _suggest(kind, R.NOTIFY_KINDS))
            continue
        if kind in R.NOTIFY_NEEDS_TARGET and not target:
            v.err(ip, f"{kind} needs a target", f'"{kind}:leads@example.com"')
            continue
        if kind == "email" and not EMAIL_RE.match(target):
            v.err(ip, f"invalid email address {target!r}")
            continue
        out.append(Channel(kind, target))
    return out


def _agents(v: _V, raw: Any, path: str, known: bool = True) -> dict[str, AgentConf]:
    """``agents:`` / ``skills:`` switches. Agent names are checked against the
    registry (warning); skill ids are checked by ``squadhq skills validate``."""
    d = v.mapping(raw, path)
    out: dict[str, AgentConf] = {}
    for name, val in d.items():
        p = f"{path}.{name}"
        if known and name not in R.KNOWN_AGENTS:
            v.warn(p, "unknown agent", _suggest(name, R.KNOWN_AGENTS))
        if isinstance(val, bool):
            out[name] = AgentConf(enabled=val)
            continue
        ad = v.mapping(val, p)
        enabled = v.boolean(ad, "enabled", p, True)
        out[name] = AgentConf(enabled=enabled, options={k: x for k, x in ad.items() if k != "enabled"})
    return out


def _policies(v: _V, raw: Any) -> Policies:
    d = v.mapping(raw, "policies", {"wip_limit", "review_fix_rounds", "triggers", "merge", "release"})
    p = Policies(
        wip_limit=v.integer(d, "wip_limit", "policies", 5, 1, 100),
        review_fix_rounds=v.integer(d, "review_fix_rounds", "policies", 2, 0, 10),
    )
    td = v.mapping(d.get("triggers"), "policies.triggers", {"allowed_associations"})
    p.allowed_associations = v.str_list(td, "allowed_associations", "policies.triggers",
                                        p.allowed_associations, choices=R.ASSOCIATIONS)
    if "NONE" in p.allowed_associations or "FIRST_TIMER" in p.allowed_associations:
        v.warn("policies.triggers.allowed_associations", "lets anyone on the internet trigger paid LLM jobs",
               "keep to OWNER, MEMBER, COLLABORATOR unless the hub runs on a strict budget")
    md = v.mapping(d.get("merge"), "policies.merge", {"mode", "risk_based", "always_human"})
    m = Merge(mode=v.string(md, "mode", "policies.merge", "approvers", choices=R.MERGE_MODES))
    rd = v.mapping(md.get("risk_based"), "policies.merge.risk_based",
                   {"max_risk", "require_checks", "require_security_agent"})
    m.risk_based = RiskBased(
        max_risk=v.string(rd, "max_risk", "policies.merge.risk_based", "low", choices=R.RISK_LEVELS),
        require_checks=v.str_list(rd, "require_checks", "policies.merge.risk_based",
                                  RiskBased().require_checks, choices=R.GATES),
        require_security_agent=v.boolean(rd, "require_security_agent", "policies.merge.risk_based", True),
    )
    m.always_human = v.str_list(md, "always_human", "policies.merge", m.always_human, choices=R.ALWAYS_HUMAN_AREAS)
    if m.mode == "auto":
        v.warn("policies.merge.mode", "auto merges every reviewer-approved PR with no human click",
               "only use with a ruleset requiring `hub / ci-gate`; `squadhq doctor` checks it")
    p.merge = m
    reld = v.mapping(d.get("release"), "policies.release", {"strategy", "promote_branch"})
    p.release = Release(strategy=v.string(reld, "strategy", "policies.release", "merge_is_prod",
                                          choices=R.RELEASE_STRATEGIES),
                        promote_branch=v.string(reld, "promote_branch", "policies.release", ""))
    if p.release.strategy == "promote_branch" and not p.release.promote_branch:
        v.err("policies.release.promote_branch", "is required when strategy is promote_branch",
              'e.g. promote_branch: "production"')
    return p


def _security(v: _V, raw: Any) -> Security:
    d = v.mapping(raw, "security", {"gates", "show_agent_transcripts"})
    return Security(gates=v.str_list(d, "gates", "security", list(R.DEFAULT_GATES), choices=R.GATES),
                    show_agent_transcripts=v.boolean(d, "show_agent_transcripts", "security", False))


def _environment(v: _V, name: str, raw: Any, path: str) -> Environment:
    d = v.mapping(raw, path, {"url", "api_url", "urls", "deploy", "approval"})
    e = Environment(name=name)
    e.url = v.string(d, "url", path, "", pattern=URL_RE, hint="a full URL, e.g. https://staging.acme.com")
    e.api_url = v.string(d, "api_url", path, "", pattern=URL_RE, hint="a full URL")
    ud = v.mapping(d.get("urls"), f"{path}.urls")
    for k, u in ud.items():
        if not isinstance(u, str) or not URL_RE.match(u):
            v.err(f"{path}.urls.{k}", f"invalid URL {u!r}", "a full URL")
        elif not re.match(r"^[a-z][a-z0-9_]*$", k):
            v.err(f"{path}.urls.{k}", "name must be snake_case")
        else:
            e.urls[k] = u
    e.deploy = v.string(d, "deploy", path, "none", choices=R.DEPLOY_TARGETS)
    e.approval = v.string(d, "approval", path, "none", choices=("none", "required"))
    return e


def _projects(v: _V, raw: Any, known_packs: set[str] | None) -> list[Project]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        v.err("projects", "expected a list of projects", "start each project with `- name: ...`")
        return []
    out: list[Project] = []
    seen_names: set[str] = set()
    seen_repos: set[str] = set()
    for i, item in enumerate(raw):
        path = f"projects[{i}]"
        d = v.mapping(item, path, {"name", "repo", "branch", "packs", "pack_roots", "environments", "product",
                                   "agents", "skills", "merge_mode", "budget_usd", "quality"},
                      required=("name", "repo"))
        pr = Project(name=v.string(d, "name", path, f"project{i}"),
                     repo=v.string(d, "repo", path, "", pattern=REPO_RE, hint='owner/name, e.g. "acme/storefront"'))
        if pr.name.lower() in seen_names:
            v.err(f"{path}.name", f"duplicate project name {pr.name!r}")
        if pr.repo and pr.repo.lower() in seen_repos:
            v.err(f"{path}.repo", f"duplicate repo {pr.repo!r}")
        seen_names.add(pr.name.lower())
        seen_repos.add(pr.repo.lower())
        pr.branch = v.string(d, "branch", path, "main")
        pr.packs = v.str_list(d, "packs", path, pattern=PACK_RE, hint="pack ids are lowercase-dashed, e.g. node-next")
        if known_packs is not None:
            rp = d.get("packs")
            raw_packs: list[Any] = rp if isinstance(rp, list) else list(pr.packs)
            for pk in pr.packs:
                j = raw_packs.index(pk) if pk in raw_packs else 0
                if pk not in known_packs:
                    v.err(f"{path}.packs[{j}]", f"unknown pack {pk!r} (not found under packs/)",
                           _suggest(pk, known_packs) if known_packs else "no packs/ directory found in the hub")
        rd = v.mapping(d.get("pack_roots"), f"{path}.pack_roots")
        for pk, root in rd.items():
            rp = f"{path}.pack_roots.{pk}"
            if pk not in pr.packs:
                v.err(rp, f"pack {pk!r} is not listed in {path}.packs", "add it to packs or remove this root")
            elif not isinstance(root, str) or root.startswith("/") or ".." in root.split("/"):
                v.err(rp, f"invalid root {root!r}", "a relative directory inside the repo, e.g. frontend")
            else:
                pr.pack_roots[pk] = root.rstrip("/") or "."
        ed = v.mapping(d.get("environments"), f"{path}.environments")
        for en, eraw in ed.items():
            pr.environments[en] = _environment(v, en, eraw, f"{path}.environments.{en}")
        pd = v.mapping(d.get("product"), f"{path}.product", {"brief_file", "markets", "keywords", "notes"})
        pr.product = Product(brief_file=v.string(pd, "brief_file", f"{path}.product", ""),
                             markets=v.str_list(pd, "markets", f"{path}.product"),
                             keywords=v.str_list(pd, "keywords", f"{path}.product"),
                             notes=v.string(pd, "notes", f"{path}.product", ""))
        pr.agents = _agents(v, d.get("agents"), f"{path}.agents")
        pr.skills = _agents(v, d.get("skills"), f"{path}.skills", known=False)
        pr.merge_mode = v.string(d, "merge_mode", path, "", choices=R.MERGE_MODES)
        if d.get("budget_usd") is not None:
            pr.budget_usd = v.number(d["budget_usd"], f"{path}.budget_usd")
        qp = f"{path}.quality"
        qd = v.mapping(d.get("quality"), qp, {"arch", "lint", "changed_coverage"})
        pr.quality = Quality(arch=v.string(qd, "arch", qp, "auto", choices=ARCH_MODES),
                             lint=v.string(qd, "lint", qp, "auto", choices=LINT_MODES),
                             changed_coverage=v.string(qd, "changed_coverage", qp, "report",
                                                       choices=CHANGED_COVERAGE_MODES))
        out.append(pr)
    return out


def _schedules(v: _V, raw: Any, org: Org) -> dict[str, str]:
    d = v.mapping(raw, "schedules")
    out = {k: spec for k, (_wf, spec) in R.SCHEDULES.items()}
    for key, spec in d.items():
        p = f"schedules.{key}"
        if key in R.SCHEDULE_ALIASES:
            canonical = R.SCHEDULE_ALIASES[key]
            v.warn(p, f"deprecated schedule key; use {canonical!r}", f"rename it to schedules.{canonical}")
            if canonical in d:
                continue  # the canonical key wins
            key = canonical
        elif key not in R.SCHEDULES:
            v.warn(p, "unknown schedule key (no workflow carries this marker)", _suggest(key, R.SCHEDULES))
        if not isinstance(spec, str):
            v.err(p, f"expected a string, got {spec!r}", 'quote it, e.g. "weekdays 07:30"')
            continue
        try:
            parse_schedule(spec, org.working_hours, org.working_days)
        except ScheduleError as e:
            v.err(p, e.message, e.hint)
            continue
        out[key] = spec
    for key, spec in out.items():
        try:
            render_schedule(spec, org.timezone, org.dst, org.working_hours, org.working_days)
        except ScheduleError as e:
            default = "" if key in d else f" (default {spec!r})"
            v.err(f"schedules.{key}", f"{e.message} for {org.timezone}{default}", e.hint)
    return out


def known_pack_ids(hub_root: str) -> set[str] | None:
    pdir = os.path.join(hub_root, "packs")
    if not os.path.isdir(pdir):
        return None
    return {n for n in os.listdir(pdir) if os.path.isfile(os.path.join(pdir, n, "pack.yml"))}


def hub_context(hub_root: str) -> dict[str, Any]:
    """Keyword arguments for :func:`parse` / :func:`load_text` derived from a
    hub checkout: the pack ids under packs/ and the skill jobs under skills/."""
    from .skill_jobs import skill_job_tiers
    return {"known_packs": known_pack_ids(hub_root), "skill_jobs": skill_job_tiers(hub_root)}


def parse(data: Any, *, known_packs: set[str] | None = None,
          skill_jobs: dict[str, str] | None = None) -> tuple[Config | None, list[Issue]]:
    """Validate an already-loaded YAML document. Returns ``(config, issues)``;
    ``config`` is None when there is at least one error."""
    v = _V()
    top = {"version", "org", "hub", "providers", "budgets", "trackers", "notifications", "schedules",
           "agents", "skills", "edition", "policies", "security", "projects"}
    d = v.mapping(data, "", top, required=("version", "org", "hub"))
    if d.get("version") is not None and d.get("version") != 1:
        v.err("version", f"unsupported schema version {d.get('version')!r}", "this CLI understands version: 1")
    org = _org(v, d.get("org"))
    hub = _hub(v, d.get("hub"), org.name)
    providers = _providers(v, d.get("providers"), skill_jobs)
    nd = v.mapping(d.get("notifications"), "notifications", {"escalations", "digest"})
    cfg = Config(
        version=1, org=org, hub=hub, providers=providers,
        budgets=_budgets(v, d.get("budgets"), providers.jobs),
        trackers=_trackers(v, d.get("trackers")),
        notifications=Notifications(escalations=_channels(v, nd.get("escalations"), "notifications.escalations"),
                                    digest=_channels(v, nd.get("digest"), "notifications.digest")),
        schedules=_schedules(v, d.get("schedules"), org),
        agents=_agents(v, d.get("agents"), "agents"),
        skills=_agents(v, d.get("skills"), "skills", known=False),
        edition=v.string(d, "edition", "", "", choices=R.EDITIONS),
        policies=_policies(v, d.get("policies")),
        security=_security(v, d.get("security")),
        projects=_projects(v, d.get("projects"), known_packs),
    )
    for name in cfg.budgets.per_project_usd:
        if name != "default" and not any(p.name == name for p in cfg.projects):
            v.warn(f"budgets.per_project_usd.{name}", "no project with this name", _suggest(name, [p.name for p in cfg.projects]))
    if any(i.level == "error" for i in v.issues):
        return None, v.issues
    return cfg, v.issues


def load_text(text: str, source: str = "squadhq.yml", *, known_packs: set[str] | None = None,
              skill_jobs: dict[str, str] | None = None) -> tuple[Config | None, list[Issue]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "unknown position"
        return None, [Issue("", f"YAML syntax error at {where}: {getattr(e, 'problem', e)}",
                            "check indentation (spaces, not tabs) and quoting")]
    return parse(data, known_packs=known_packs, skill_jobs=skill_jobs)


def load(path: str, *, hub_root: str | None = None) -> Config:
    """Load and validate; raise :class:`ConfigError` on any error."""
    if not os.path.isfile(path):
        raise ConfigError([Issue("", f"{path} not found", "run `squadhq init` or copy squadhq.example.yml")], path)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    ctx = hub_context(hub_root) if hub_root else {}
    cfg, issues = load_text(text, path, **ctx)
    if cfg is None:
        raise ConfigError(issues, path)
    return cfg
