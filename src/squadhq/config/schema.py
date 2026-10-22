"""JSON Schema for ``squadhq.yml`` (editor autocomplete only).

The authoritative validator is :mod:`squadhq.config.loader`; this schema is
derived from the same registry so enums never drift. Regenerate with::

    python -m squadhq.config.schema > schema/squadhq.schema.json

``tests/config/test_schema.py`` fails when the committed file is stale.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from squadhq.quality.gates import ARCH_MODES, CHANGED_COVERAGE_MODES, LINT_MODES

from . import registry as R

S = dict[str, Any]


def _enum(values: tuple[str, ...], desc: str = "", default: Any = None) -> S:
    s: S = {"type": "string", "enum": list(values)}
    if desc:
        s["description"] = desc
    if default is not None:
        s["default"] = default
    return s


def _str(desc: str = "", **kw: Any) -> S:
    s: S = {"type": "string"}
    if desc:
        s["description"] = desc
    s.update(kw)
    return s


def _obj(props: S, desc: str = "", required: list[str] | None = None, extra: bool | S = False) -> S:
    s: S = {"type": "object", "properties": props, "additionalProperties": extra}
    if desc:
        s["description"] = desc
    if required:
        s["required"] = required
    return s


def _list(items: S, desc: str = "") -> S:
    s: S = {"type": "array", "items": items, "uniqueItems": True}
    if desc:
        s["description"] = desc
    return s


_REPO = _str("owner/name", pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_BROKER_URL = r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$"
_URL = _str("Full http(s) URL", pattern=r"^https?://\S+$")
_MODEL = _str("provider/model-id, e.g. anthropic/claude-sonnet-5-5")
_SCHEDULE = _str(
    'Local-time schedule: "weekdays 07:30", "sun,wed 04:00", "every 30m during working_hours", "hourly", '
    '"hourly :15", "monthly 1st 05:00", "quarterly 1st 05:00", or raw five-field cron (taken as UTC).'
)
_AGENT = {
    "oneOf": [
        {"type": "boolean"},
        _obj({"enabled": {"type": "boolean", "default": True}}, "Agent switch plus agent-specific options",
             extra=True),
    ]
}
RESERVED = " RESERVED (server mode only): validated, not enforced in Actions mode."
_CHANNELS = _list(_str("github_issue[:owner/repo] | slack[:#channel] | email:addr | teams[:<url>] | "
                       "webhook[:<url>] | discord[:<url>] | pagerduty[:<routing key>] | expo_push. Actions workflows "
                       "post github_issue; other channels are rendered to SQUADHQ_NOTIFY_* for "
                       "`python -m squadhq.notify` and server mode.",
                       pattern=r"^(github_issue|slack|teams|webhook|discord|pagerduty|expo_push)(:.+)?$|^email:.+$"))


def build() -> S:
    provider_props: S = {
        name: _obj({
            "auth": _enum(R.PROVIDER_AUTH[name], default=R.PROVIDER_AUTH[name][0]),
            "base_url": _URL,
            "region": _str(),
            "secret": _str("Secret holding the credential", pattern=r"^[A-Z_][A-Z0-9_]*$"),
        }, f"{name} provider settings")
        for name in R.PROVIDERS
    }
    pool_entry = _obj({
        "id": _str("Account id (slug): shown in summaries and the capacity state",
                   pattern=r"^[a-z0-9][a-z0-9-]{0,33}$"),
        "auth": _enum(("api", "subscription"), default="subscription"),
        "secret": _str("Secret holding this account's credential. Actions mode passes only "
                       "ANTHROPIC_API_KEY[_1..5] / CLAUDE_CODE_OAUTH_TOKEN[_1..5]; default <that>_<n>",
                       pattern=r"^[A-Z][A-Z0-9_]*$"),
    }, "One coding-agent account", required=["id"])
    anth = provider_props["anthropic"]["properties"]
    anth["pool"] = {"type": "array", "items": pool_entry, "maxItems": 5,
                    "description": "Coding-agent account pool: when one account hits its limit the next job uses "
                                   "the next one; the fleet pauses (or falls back) only when all are tripped. "
                                   "Pooling personal consumer subscriptions may violate the provider's terms."}
    anth["pool_strategy"] = _enum(("ordered", "round_robin"), "ordered = first untripped account; round_robin = "
                                  "rotate across untripped accounts", default="ordered")
    tier = _obj({"primary": _MODEL, "fallback": _MODEL}, required=["primary"])
    env = _obj({
        "url": _URL, "api_url": _URL,
        "urls": {"type": "object", "additionalProperties": _URL, "description": "Extra named URLs (mcp, docs ...)"},
        "deploy": _enum(R.DEPLOY_TARGETS, default="none"),
        "approval": _enum(("none", "required"), "Human approval before deploying here", "none"),
    })
    project = _obj({
        "name": _str("Display name"),
        "repo": _REPO,
        "branch": _str("Integration branch", default="main"),
        "packs": _list(_str(pattern=r"^[a-z0-9][a-z0-9-]*$"), "Stack packs from packs/<id>"),
        "pack_roots": {"type": "object", "additionalProperties": _str(), "description": "pack id -> working dir"},
        "environments": {"type": "object", "additionalProperties": env},
        "product": _obj({"brief_file": _str(), "markets": _list(_str()), "keywords": _list(_str()),
                         "notes": _str()}),
        "agents": {"type": "object", "additionalProperties": _AGENT},
        "skills": {"type": "object", "additionalProperties": _AGENT, "description": "Per-project skill switches"},
        "merge_mode": _enum(R.MERGE_MODES, "Overrides policies.merge.mode for this project"),
        "budget_usd": {"type": "number", "minimum": 0, "description": "Per-project budget." + RESERVED},
        "quality": _obj({
            "arch": _enum(ARCH_MODES, "Architecture gate: auto = runs when the pack's arch config exists in the "
                          "repo; required = a missing config fails ci-gate; off = never", "auto"),
            "lint": _enum(LINT_MODES, "Lint/format gate: auto = runs when a linter config exists (or the linter "
                          "needs none); default = also use the pack's default config when the repo has none; "
                          "off = never", "auto"),
            "changed_coverage": _enum(CHANGED_COVERAGE_MODES, "Changed-lines coverage against the pack's "
                                      "min_changed: enforce = a required ci-gate check (when min_changed > 0 and "
                                      "a report exists); report = summary only; off = not computed", "report"),
        }, "Quality gates run by stack CI and enforced by ci-gate"),
    }, required=["name", "repo"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:agent-squad-hq:schema:squadhq.yml",
        "title": "squadhq.yml",
        "description": "Agent Squad HQ configuration (schema version 1). Generated by squadhq.config.schema.",
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "org", "hub"],
        "properties": {
            "version": {"const": 1},
            "org": _obj({
                "name": _str("Organisation display name"),
                "timezone": _str("IANA zone; all schedules are local time", default="UTC"),
                "working_hours": _str("HH:MM-HH:MM", pattern=r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", default="09:00-18:00"),
                "working_days": _str("daily | weekdays | weekends | mon-fri | sun,wed", default="mon-fri"),
                "approvers": _list(_str(pattern=r"^@?[A-Za-z0-9-]+(/[A-Za-z0-9._-]+)?$"),
                                   "Users/teams who approve merges. Rendered to SQUADHQ_APPROVERS and "
                                   "projects[].merge.approvers; who may TRIGGER jobs is "
                                   "policies.triggers.allowed_associations."),
                "operating_model": _enum(R.OPERATING_MODELS, "solo | team." + RESERVED, default="team"),
                "dst": _enum(R.DST_POLICIES, "Which UTC offset crons are rendered for", "daylight"),
            }, required=["name", "approvers"]),
            "hub": _obj({
                "repo": _REPO,
                "ref": _str(default="main"),
                "trigger": _str(default="@squad", pattern=r"^@[A-Za-z0-9_-]+$"),
                "bot": {"description": "OPS GitHub App: routing, protocol markers, policy-approved merges "
                                       "(SQUADHQ_APP_CLIENT_ID / SQUADHQ_APP_PRIVATE_KEY / SQUADHQ_BOT_LOGIN)",
                        "oneOf": [
                    _str("GitHub App slug"),
                    _obj({"app": _str(pattern=r"^[a-z0-9][a-z0-9-]{0,33}$", default="squadhq-ops"),
                          "login": _str(), "client_id": _str()}),
                ]},
                "agent_bot": {"description": "AGENT GitHub App the coding agents run as; cannot merge, approve "
                                             "its own pushes or edit workflows (SQUADHQ_AGENT_APP_CLIENT_ID / "
                                             "SQUADHQ_AGENT_APP_PRIVATE_KEY / SQUADHQ_AGENT_BOT_LOGIN). Default "
                                             "<bot.app>-agent",
                              "oneOf": [
                    _str("GitHub App slug"),
                    _obj({"app": _str(pattern=r"^[a-z0-9][a-z0-9-]{0,33}$"), "login": _str(), "client_id": _str()}),
                ]},
                "single_app": {"type": "boolean", "default": False,
                               "description": "Fallback: agents run as the ops App. Every auto, risk_based and "
                                              "trivial merge then needs an approver."},
                "token_broker": {"description": "Token-broker mode (ADR-0011): App installation tokens come from "
                                               "this broker in exchange for a GitHub Actions OIDC token, so no App "
                                               "private key is stored (SQUADHQ_TOKEN_BROKER / "
                                               "SQUADHQ_TOKEN_BROKER_AUDIENCE). Cannot be combined with single_app.",
                                 "oneOf": [
                    _str("Broker URL (https)", pattern=_BROKER_URL),
                    _obj({"url": _str("Broker URL (https)", pattern=_BROKER_URL),
                          "audience": _str("OIDC audience the broker verifies; default the broker URL")},
                         required=["url"]),
                ]},
            }, required=["repo"]),
            "providers": _obj({
                "default": _enum(R.PROVIDERS),
                **provider_props,
                "routing": _obj({"heavy": tier, "light": tier}, "Model tiers", required=["heavy", "light"],
                                extra=tier),
                "jobs": _obj({j: _str(default=t) for j, t in R.DEFAULT_JOB_TIERS.items()},
                             "job -> tier overrides", extra=_str()),
                "executor": _enum(R.EXECUTORS, "Coding-agent executor. Actions mode runs claude-code; the "
                                  "others are" + RESERVED, default="claude-code"),
                "fallback_on_capacity_trip": {"type": "boolean", "default": False},
            }),
            "budgets": _obj({
                "monthly_usd": {"type": "number", "minimum": 0, "default": 100},
                "per_project_usd": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0},
                                    "description": "Project name (or default) -> USD." + RESERVED},
                "per_run_usd": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0},
                                "description": "Job -> USD per run." + RESERVED},
                "on_exceed": _enum(R.ON_EXCEED, "Actions mode always pauses at monthly_usd; downgrade and "
                                   "notify are" + RESERVED, default="pause"),
            }),
            "trackers": _obj({
                "default": _enum(R.TRACKERS, default="github"),
                "jira": _obj({"site": _str(), "project": _str(), "status_map": _str()}, required=["site", "project"],
                             extra=True),
                "linear": _obj({"team": _str()}, required=["team"], extra=True),
                "trello": _obj({"board": _str()}, required=["board"], extra=True),
            }),
            "notifications": _obj({"escalations": _CHANNELS, "digest": _CHANNELS}),
            "schedules": _obj({k: {**_SCHEDULE, "default": spec} for k, (_wf, spec) in R.SCHEDULES.items()},
                              "Local-time schedules per workflow marker", extra=_SCHEDULE),
            "agents": _obj({a: _AGENT for a in R.KNOWN_AGENTS}, "Switch agents on/off (rendered to "
                           "SQUADHQ_DISABLED_AGENTS together with their job keys)", extra=_AGENT),
            "skills": {"type": "object", "additionalProperties": _AGENT,
                       "description": "Switch catalogue skills on/off by id (threat-modeler or threat_modeler)"},
            "edition": _enum(R.EDITIONS, "Skills above this tier are unavailable whatever the switches say"),
            "policies": _obj({
                "wip_limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 5,
                              "description": "Open agent PRs per project (projects.json policies.wip_limit)"},
                "review_fix_rounds": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2,
                                      "description": "Automated fix rounds before needs-human "
                                                     "(SQUADHQ_REVIEW_FIX_ROUNDS)"},
                "triggers": _obj({"allowed_associations": _list(_enum(R.ASSOCIATIONS))}),
                "merge": _obj({
                    "mode": _enum(R.MERGE_MODES, default="approvers"),
                    "risk_based": _obj({"max_risk": _enum(R.RISK_LEVELS, "Highest final risk that may "
                                                          "auto-merge", default="low"),
                                        "require_checks": _list(_enum(R.GATES)),
                                        "require_security_agent": {"type": "boolean", "default": True,
                                                                   "description": RESERVED.strip()}}),
                    "always_human": _list(_enum(R.ALWAYS_HUMAN_AREAS),
                                          "Areas that never auto-merge, by risk path category ('pii' has no "
                                          "detector yet)"),
                }),
                "release": _obj({"strategy": _enum(R.RELEASE_STRATEGIES, "merge_is_prod is the Actions "
                                                   "behaviour; the others are" + RESERVED, default="merge_is_prod"),
                                 "promote_branch": _str()}),
            }),
            "security": _obj({"gates": {**_list(_enum(R.GATES)), "default": list(R.DEFAULT_GATES),
                                        "description": "Rendered to SQUADHQ_GATES"},
                              "show_agent_transcripts": {"type": "boolean", "default": False}}),
            "projects": {"type": "array", "items": project},
        },
    }


def dumps() -> str:
    return json.dumps(build(), indent=2) + "\n"


if __name__ == "__main__":  # pragma: no cover
    sys.stdout.write(dumps())
