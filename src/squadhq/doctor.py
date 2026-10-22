"""``squadhq doctor``: re-run every installation check, any time.

Each check yields ✅ (ok), ❌ (broken, exit 1) or ⚠️ (works, but read the
note) with the exact fix. GitHub checks run only when ``gh`` is installed and
authenticated; otherwise they are reported as skipped with the fix.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timezone

from squadhq.config import loader
from squadhq.config import registry as R
from squadhq.config import render as RN
from squadhq.config.gh import Gh
from squadhq.config.github_ops import list_names, ruleset_requires_check, ruleset_review
from squadhq.config.model import Config
from squadhq.config.schedules import zone_offset

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"
ICON = {OK: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "➖"}
OAUTH_TOKEN_DAYS = 365  # `claude setup-token` tokens are valid for one year

# Stable check ids for `squadhq doctor --check <id>` (the config-doctor skill uses them).
CHECK_IDS: dict[str, str] = {
    "config": "squadhq.yml exists and validates (warnings included)",
    "rendered": "config/*.json and workflow crons match squadhq.yml",
    "schedule-markers": "every schedule has a `# squadhq:schedule:<key>` marker (and vice versa)",
    "dst": "daylight-saving caveat for the rendered cron offset",
    "models": "model ids in providers.routing are known for their provider",
    "schedules": "timezone offset the schedules were rendered for",
    "reserved": "settings that validate but are not enforced in Actions mode (server mode only)",
    "gh": "gh CLI installed and authenticated",
    "secrets": "required secrets present (org or hub repo)",
    "token-expiry": "CLAUDE_CODE_OAUTH_TOKEN age vs its one-year lifetime",
    "variables": "SQUADHQ_* variables present and equal to the rendered values",
    "app": "GitHub App installed with access to the hub and project repos",
    "rulesets": "project rulesets require `hub / ci-gate`, one approval and approval after the last push",
    "agent-app": "a separate agent GitHub App is configured and installed (single-app mode disables auto-merge)",
    "pool": "coding-agent account pool: accounts, secret slots, api/subscription mixing, pool secrets present",
    "token-broker": "token-broker mode: SQUADHQ_TOKEN_BROKER is an https URL that answers (ADR-0011)",
}
GITHUB_IDS = frozenset({"gh", "secrets", "token-expiry", "variables", "app", "rulesets", "agent-app", "token-broker"})
BROKER_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$")
SINGLE_APP_FIX = ("squadhq init --create-app --apply creates the agent App; then set hub.single_app: false "
                  "(or remove it) and SQUADHQ_AGENT_APP_CLIENT_ID / SQUADHQ_AGENT_APP_PRIVATE_KEY")


@dataclass
class Check:
    name: str
    status: str
    fix: str = ""
    id: str = ""

    def line(self) -> str:
        return f"{ICON[self.status]} {self.name}" + (f"\n      fix: {self.fix}" if self.fix and self.status != OK else "")


def _tag(checks: list[Check], check_id: str) -> list[Check]:
    for c in checks:
        if not c.id:
            c.id = check_id
    return checks


def _age_days(ts: str, now: datetime) -> int | None:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now - t).days


def check_config(path: str, root: str) -> tuple[Config | None, list[Check]]:
    if not os.path.isfile(path):
        return None, [Check(f"config {path} present", FAIL, "run `squadhq init` or copy squadhq.example.yml",
                            "config")]
    with open(path, encoding="utf-8") as f:
        cfg, issues = loader.load_text(f.read(), path, **loader.hub_context(root))
    errs = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    if errs:
        return None, [Check(f"config {path} valid ({len(errs)} error(s))", FAIL,
                            "; ".join(f"{i.path}: {i.message}" for i in errs[:5]) + " — run `squadhq validate`",
                            "config")]
    out = [Check(f"config {path} valid", OK)]
    for w in warns:
        out.append(Check(f"config {w.path}: {w.message}", WARN, w.hint))
    return cfg, _tag(out, "config")


def check_rendered(cfg: Config, root: str) -> list[Check]:
    try:
        rp = RN.plan(cfg, root)
    except ValueError as e:
        return [Check("schedules render", FAIL, str(e), "rendered")]
    stale = [f.path for f in rp.stale]
    out = [Check("rendered files fresh" if not stale else f"rendered files stale: {', '.join(stale)}",
                 OK if not stale else FAIL, "run `squadhq render` and commit the result", "rendered")]
    for w in rp.warnings:
        if w.startswith("reserved: "):
            out.append(Check(w, WARN, "remove the setting or run server mode", "reserved"))
        elif w.startswith("DST"):
            out.append(Check(w, WARN, "set org.dst: standard to run late instead of early, or accept the shift",
                             "dst"))
        else:
            out.append(Check(w, WARN, "add `# squadhq:schedule:<key>` to the workflow cron line",
                             "schedule-markers"))
    return out


def check_models(cfg: Config) -> list[Check]:
    out = []
    for where, ref in RN.model_refs(cfg):
        known = R.KNOWN_MODELS.get(ref.provider)
        if known is None:
            out.append(Check(f"model {ref} ({where})", WARN, f"cannot verify {ref.provider} model ids offline"))
        elif ref.model in known:
            out.append(Check(f"model {ref} ({where})", OK))
        else:
            out.append(Check(f"model {ref} ({where}) is not a known {ref.provider} model", FAIL,
                             f"use one of: {', '.join(known)} (or upgrade squadhq if the model is new)"))
    return _tag(out, "models")


def check_github(cfg: Config, gh: Gh, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    if not gh.available():
        return [Check("gh CLI installed", SKIP, "install https://cli.github.com to enable GitHub checks", "gh")]
    if not gh.run(["auth", "status"], timeout=20).ok:
        return [Check("gh authenticated", FAIL, "run `gh auth login` (scopes: repo, workflow, admin:org)", "gh")]
    out = [Check("gh authenticated", OK, id="gh")]
    owner, hub = cfg.owner, cfg.hub.repo

    secrets = list_names(gh, "secret", owner, hub)
    pool_names = {n for n, _ in RN.pool_secrets(cfg)}
    for name, purpose in RN.required_secrets(cfg):
        if name in pool_names:
            continue  # reported by the pool check (a missing slot only shrinks the pool)
        if name in secrets:
            out.append(Check(f"secret {name} — {purpose}", OK))
        else:
            fix = f"gh secret set {name} --org {owner} --visibility all"
            if name == "CLAUDE_CODE_OAUTH_TOKEN":
                fix = f"claude setup-token, then: {fix}"
            elif name == "SQUADHQ_APP_PRIVATE_KEY":
                fix = f"squadhq init --create-app, or: {fix} < <app>.private-key.pem"
            out.append(Check(f"secret {name} missing — {purpose}", FAIL, fix))
    _tag(out, "secrets")
    tok = secrets.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok and tok.get("updatedAt"):
        age = _age_days(str(tok["updatedAt"]), now)
        if age is not None:
            left = OAUTH_TOKEN_DAYS - age
            st = FAIL if left <= 0 else WARN if left <= 30 else OK
            out.append(Check(f"CLAUDE_CODE_OAUTH_TOKEN set {age} days ago (~{max(left, 0)} days until expiry)", st,
                             "regenerate with `claude setup-token` and re-set the secret", "token-expiry"))

    variables = list_names(gh, "variable", owner, hub)
    want = RN.repo_variables(cfg)
    for name, value in want.items():
        have = variables.get(name)
        if value == "" and have is None:
            out.append(Check(f"variable {name} (empty, not needed)", OK))
        elif have is None:
            out.append(Check(f"variable {name} missing", FAIL,
                             f"gh variable set {name} --org {owner} --visibility all --body '{value}'"))
        elif str(have.get("value", "")) != value:
            out.append(Check(f"variable {name} = {have.get('value')!r}, squadhq.yml says {value!r}", FAIL,
                             f"gh variable set {name} --org {owner} --visibility all --body '{value}'"))
        else:
            out.append(Check(f"variable {name}", OK))

    _tag(out, "variables")
    out.extend(check_pool(cfg, secrets))
    out.extend(_tag(check_app_installation(cfg, gh), "app"))
    out.extend(check_token_broker(cfg, variables))
    if not cfg.hub.single_app and not cfg.hub.token_broker:
        if "SQUADHQ_AGENT_APP_CLIENT_ID" not in variables:
            out.append(Check("variable SQUADHQ_AGENT_APP_CLIENT_ID missing: agents silently run as the ops App "
                             "and every unattended merge is disabled", FAIL, SINGLE_APP_FIX, "agent-app"))
        if "SQUADHQ_AGENT_APP_PRIVATE_KEY" not in secrets:
            out.append(Check("secret SQUADHQ_AGENT_APP_PRIVATE_KEY missing", FAIL, SINGLE_APP_FIX, "agent-app"))
        out.extend(_tag(check_app_installation(cfg, gh, cfg.hub.agent_bot.app), "agent-app"))
    fix = "squadhq init --apply (applies the 'squadhq' ruleset)"
    for p in cfg.projects:
        has = ruleset_requires_check(gh, p.repo)
        if has is None:
            out.append(Check(f"ruleset on {p.repo}", WARN, f"cannot read rules for {p.repo} with this login",
                             "rulesets"))
            continue
        if has:
            out.append(Check(f"ruleset on {p.repo} requires `{R.REQUIRED_CHECK}`", OK, id="rulesets"))
        else:
            out.append(Check(f"ruleset on {p.repo} does not require `{R.REQUIRED_CHECK}`", FAIL, fix, "rulesets"))
        rev = ruleset_review(gh, p.repo) or {}
        if rev.get("approvals", 0) >= 1 and rev.get("last_push"):
            out.append(Check(f"ruleset on {p.repo} requires an approval after the last push", OK, id="rulesets"))
        else:
            out.append(Check(f"ruleset on {p.repo} lets a PR merge without an independent approval "
                             f"(approvals={rev.get('approvals', 0)}, last-push approval="
                             f"{bool(rev.get('last_push'))})", FAIL, fix, "rulesets"))
    return out


def check_pool(cfg: Config, secrets: dict[str, dict[str, Any]] | None = None) -> list[Check]:
    """The account pool. Offline: accounts, Actions secret slots, auth mixing.
    With ``secrets`` (names present on GitHub): each pool secret and the hand-off key."""
    from squadhq.config.loader import pool_actions_slots

    anth = cfg.providers.configs.get("anthropic")
    if not anth or not anth.pool:
        return [] if secrets is not None else [Check("account pool: none (single credential)", OK, id="pool")]
    out: list[Check] = []
    owner = cfg.owner
    if secrets is None:
        fb = "metered API fallback" if cfg.providers.fallback_on_capacity_trip else "pause"
        out.append(Check(f"account pool: {len(anth.pool)} account(s), {anth.pool_strategy}; all tripped -> {fb}: "
                         + ", ".join(f"{a.id} ({a.auth}, {a.secret})" for a in anth.pool), OK))
        for a in anth.pool:
            if a.secret not in pool_actions_slots(a.auth):
                out.append(Check(f"pool account {a.id}: {a.secret} is not a declared workflow secret slot "
                                 "(Actions mode skips it)", WARN,
                                 f"rename the secret to one of {', '.join(pool_actions_slots(a.auth)[1:])}"))
        auths = {a.auth for a in anth.pool}
        if len(auths) > 1:
            out.append(Check("account pool mixes api and subscription accounts: a subscription limit moves work "
                             "onto metered API keys without a pause", WARN,
                             "keep one auth kind per pool, or list subscription accounts first and watch the ledger"))
        if "subscription" in auths:
            out.append(Check("account pool includes subscription accounts", WARN,
                             "rotating several API keys or organisation accounts is fine; pooling or rotating "
                             "personal consumer subscriptions for automated use may violate the provider's terms "
                             "— that is your responsibility"))
        return _tag(out, "pool")
    for name, purpose in RN.pool_secrets(cfg):
        if name in secrets:
            out.append(Check(f"secret {name} — {purpose}", OK))
        elif name == "SQUADHQ_HANDOFF_KEY":
            out.append(Check(f"secret {name} missing — with a pool the hand-off key falls back to the single "
                             "LLM credential, which may be unset", WARN,
                             f"openssl rand -hex 32 | gh secret set {name} --org {owner} --visibility all"))
        else:
            out.append(Check(f"secret {name} missing — {purpose} — a job that selects this account fails",
                             WARN, f"gh secret set {name} --org {owner} --visibility all"))
    return _tag(out, "pool")


def probe_url(url: str, timeout: float = 5.0) -> str:
    """Empty when an HTTP server answers at ``url`` (any status), else the reason."""
    try:
        req = urllib.request.Request(url, method="GET")  # noqa: S310 - BROKER_URL_RE allows https only
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310 - same URL
            return ""
    except urllib.error.HTTPError:
        return ""  # it answered
    except (urllib.error.URLError, OSError, ValueError) as e:
        return str(getattr(e, "reason", e))[:200]


PROBE: Callable[[str], str] = probe_url


def check_token_broker(cfg: Config, variables: dict[str, dict[str, Any]]) -> list[Check]:
    """Token-broker mode: no App key is needed, but the variable must be an https URL that answers."""
    have = str((variables.get("SQUADHQ_TOKEN_BROKER") or {}).get("value", ""))
    if not cfg.hub.token_broker:
        if have:
            return [Check(f"variable SQUADHQ_TOKEN_BROKER={have!r} is set but squadhq.yml has no hub.token_broker: "
                          "workflows already use the broker", WARN,
                          "add hub.token_broker to squadhq.yml, or delete the variable to use App keys",
                          "token-broker")]
        return []
    url = have or cfg.hub.token_broker
    if not BROKER_URL_RE.match(url):
        return [Check(f"SQUADHQ_TOKEN_BROKER {url!r} is not an https URL", FAIL,
                      f"gh variable set SQUADHQ_TOKEN_BROKER --org {cfg.owner} --visibility all "
                      f"--body '{cfg.hub.token_broker}'", "token-broker")]
    why = PROBE(url)
    if why:
        return [Check(f"token broker {url} did not answer ({why})", WARN,
                      "check the URL and that the broker is up; every token mint fails while it is down",
                      "token-broker")]
    return [Check(f"token broker {url} answers (no App private key needed)", OK, id="token-broker")]


def check_agent_config(cfg: Config) -> Check:
    if cfg.hub.token_broker:
        return Check(f"token-broker mode: ops App {cfg.hub.bot.app} and agent App {cfg.hub.agent_bot.app} tokens "
                     f"come from {cfg.hub.token_broker}", OK, id="agent-app")
    if cfg.hub.single_app:
        return Check("single-app mode: agents share the ops identity, so auto, risk_based and trivial merges are "
                     "disabled (an approver merges everything), sweeper implementer markers are ignored (the "
                     "engineer implements), session-context notes are not trusted, and the team thread is not "
                     "started automatically", WARN, SINGLE_APP_FIX, "agent-app")
    return Check(f"separate agent App {cfg.hub.agent_bot.app} (ops App {cfg.hub.bot.app})", OK, id="agent-app")


def check_app_installation(cfg: Config, gh: Gh, slug: str | None = None) -> list[Check]:
    slug = slug or cfg.hub.bot.app
    r = gh.run(["api", f"orgs/{cfg.owner}/installations", "--paginate"])
    if not r.ok:
        return [Check(f"GitHub App {slug} installation", WARN,
                      f"cannot list installations for {cfg.owner} (needs org admin); check "
                      f"https://github.com/organizations/{cfg.owner}/settings/installations")]
    data = r.json() or {}
    insts = data.get("installations", []) if isinstance(data, dict) else []
    inst = next((i for i in insts if isinstance(i, dict) and i.get("app_slug") == slug), None)
    if not inst:
        return [Check(f"GitHub App {slug} installed on {cfg.owner}", FAIL,
                      f"install it: https://github.com/apps/{slug}/installations/new")]
    out = [Check(f"GitHub App {slug} installed on {cfg.owner}", OK)]
    if inst.get("repository_selection") == "all":
        return out + [Check(f"App {slug} has access to all repositories", OK)]
    rr = gh.run(["api", f"user/installations/{inst.get('id')}/repositories", "--paginate"])
    names: set[str] = set()
    if rr.ok and isinstance(rr.json(), dict):
        names = {str(x.get("full_name", "")).lower() for x in rr.json().get("repositories", [])}
    for repo in [cfg.hub.repo] + [p.repo for p in cfg.projects]:
        if not rr.ok:
            out.append(Check(f"App access to {repo}", WARN, "cannot list installation repositories"))
        elif repo.lower() in names:
            out.append(Check(f"App access to {repo}", OK))
        else:
            out.append(Check(f"App {slug} cannot access {repo}", FAIL,
                             f"add {repo} under https://github.com/organizations/{cfg.owner}/settings/installations/"
                             f"{inst.get('id')}"))
    return out


def run(path: str, root: str, gh: Gh, *, offline: bool = False, only: str | None = None) -> list[Check]:
    """Run every check, or just the one ``only`` names (see :data:`CHECK_IDS`).
    An invalid config is always reported, since nothing else can run without it."""
    if only is not None and only not in CHECK_IDS:
        raise ValueError(f"unknown check {only!r}; one of: {', '.join(CHECK_IDS)}")
    cfg, checks = check_config(path, root)
    if cfg is None:
        return checks
    if only is None or only in ("rendered", "schedule-markers", "dst", "reserved"):
        checks += check_rendered(cfg, root)
    if only is None or only == "agent-app":
        checks.append(check_agent_config(cfg))
    if only is None or only == "models":
        checks += check_models(cfg)
    if only is None or only == "pool":
        checks += check_pool(cfg)
    if only is None or only == "schedules":
        off = zone_offset(cfg.org.timezone, cfg.org.dst)
        checks.append(Check(f"schedules rendered for {cfg.org.timezone} at {off.label()}", OK, id="schedules"))
    if (only is None and not offline) or only in GITHUB_IDS or (only == "pool" and not offline):
        checks += check_github(cfg, gh)
    if only is not None:
        checks = [c for c in checks if c.id == only]
        if not checks:
            checks = [Check(f"{only}: nothing to check ({CHECK_IDS[only]})", OK, id=only)]
    return checks


def report(checks: list[Check]) -> tuple[str, int]:
    lines = [c.line() for c in checks]
    bad = sum(1 for c in checks if c.status == FAIL)
    warn = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    lines.append(f"{len(checks) - bad - warn} ok, {warn} warning(s), {bad} problem(s)")
    return "\n".join(lines), 1 if bad else 0
