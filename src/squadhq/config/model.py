"""Typed model of ``squadhq.yml`` (schema version 1).

Built by :func:`squadhq.config.loader.load`; every field is already validated
and defaulted, so consumers never need ``.get()`` chains.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Org:
    name: str
    timezone: str = "UTC"
    working_hours: str = "09:00-18:00"
    working_days: str = "mon-fri"
    approvers: list[str] = field(default_factory=list)
    operating_model: str = "team"
    dst: str = "daylight"


@dataclass
class Bot:
    app: str = "squadhq-ops"
    login: str = "squadhq-ops[bot]"
    client_id: str = ""


@dataclass
class Hub:
    repo: str
    ref: str = "main"
    bot: Bot = field(default_factory=Bot)  # ops App: routing, markers, merges
    agent_bot: Bot = field(default_factory=lambda: Bot(app="squadhq-agent", login="squadhq-agent[bot]"))
    single_app: bool = False  # True: agents run as the ops App; nothing merges unattended
    trigger: str = "@squad"
    # Token-broker mode (ADR-0011): App tokens come from this https URL via
    # GitHub OIDC instead of App private keys. "" = App keys.
    token_broker: str = ""
    token_broker_audience: str = ""  # OIDC audience; "" = the broker URL


@dataclass
class PoolAccount:
    id: str  # slug; shown in summaries, the ledger and the capacity state
    auth: str  # api | subscription
    secret: str  # secret NAME holding this account's credential


@dataclass
class ProviderConf:
    name: str
    auth: str = "api"
    base_url: str = ""
    region: str = ""
    secret: str = ""  # secret name holding the credential ("" = none needed)
    # Coding-agent account pool (anthropic only). Empty = the single credential above.
    pool: list[PoolAccount] = field(default_factory=list)
    pool_strategy: str = "ordered"  # ordered | round_robin


@dataclass
class ModelRef:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class Tier:
    name: str
    primary: ModelRef
    fallback: ModelRef | None = None


@dataclass
class Providers:
    default: str
    configs: dict[str, ProviderConf]
    routing: dict[str, Tier]
    jobs: dict[str, str]  # job -> tier (fully resolved: defaults + overrides)
    executor: str = "claude-code"
    fallback_on_capacity_trip: bool = False


@dataclass
class Budgets:
    monthly_usd: float = 100.0
    per_project_usd: dict[str, float] = field(default_factory=dict)
    per_run_usd: dict[str, float] = field(default_factory=dict)
    on_exceed: str = "pause"


@dataclass
class Trackers:
    default: str = "github"
    options: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Channel:
    kind: str
    target: str = ""

    def __str__(self) -> str:
        return self.kind + (f":{self.target}" if self.target else "")


@dataclass
class Notifications:
    escalations: list[Channel] = field(default_factory=lambda: [Channel("github_issue")])
    digest: list[Channel] = field(default_factory=lambda: [Channel("github_issue")])


@dataclass
class AgentConf:
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskBased:
    max_risk: str = "low"
    require_checks: list[str] = field(default_factory=lambda: ["sast", "sca", "secrets"])
    require_security_agent: bool = True


@dataclass
class Merge:
    mode: str = "approvers"
    risk_based: RiskBased = field(default_factory=RiskBased)
    always_human: list[str] = field(default_factory=lambda: ["auth", "payments", "migrations", "workflows", "infra",
                                                             "deps-major"])


@dataclass
class Release:
    strategy: str = "merge_is_prod"
    promote_branch: str = ""


@dataclass
class Policies:
    wip_limit: int = 5
    review_fix_rounds: int = 2
    allowed_associations: list[str] = field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"])
    merge: Merge = field(default_factory=Merge)
    release: Release = field(default_factory=Release)


@dataclass
class Security:
    gates: list[str] = field(default_factory=lambda: ["secrets", "sast", "sca", "license", "iac", "sbom"])
    show_agent_transcripts: bool = False


@dataclass
class Environment:
    name: str
    url: str = ""
    api_url: str = ""
    urls: dict[str, str] = field(default_factory=dict)
    deploy: str = "none"
    approval: str = "none"  # none | required


@dataclass
class Product:
    brief_file: str = ""
    markets: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Quality:
    """Per-project quality gates (see docs/concepts/quality-gates.md)."""
    arch: str = "auto"               # auto | required | off
    lint: str = "auto"               # auto | default | off
    changed_coverage: str = "report"  # enforce | report | off


@dataclass
class Project:
    name: str
    repo: str
    branch: str = "main"
    packs: list[str] = field(default_factory=list)
    pack_roots: dict[str, str] = field(default_factory=dict)
    environments: dict[str, Environment] = field(default_factory=dict)
    product: Product = field(default_factory=Product)
    agents: dict[str, AgentConf] = field(default_factory=dict)
    skills: dict[str, AgentConf] = field(default_factory=dict)
    merge_mode: str = ""  # "" = inherit policies.merge.mode
    budget_usd: float | None = None
    quality: Quality = field(default_factory=Quality)


@dataclass
class Config:
    version: int
    org: Org
    hub: Hub
    providers: Providers
    budgets: Budgets
    trackers: Trackers
    notifications: Notifications
    schedules: dict[str, str]
    agents: dict[str, AgentConf]
    policies: Policies
    security: Security
    projects: list[Project]
    skills: dict[str, AgentConf] = field(default_factory=dict)
    edition: str = ""

    def disabled_agents(self) -> list[str]:
        """Agent and skill names switched off org-wide (as written in squadhq.yml)."""
        return sorted({n for n, a in self.agents.items() if not a.enabled}
                      | {n for n, a in self.skills.items() if not a.enabled})

    def disabled_job_keys(self) -> list[str]:
        """Everything SQUADHQ_DISABLED_AGENTS must list so the runtime's checks
        (job keys, modes, schedule keys, skill ids) see the switch."""
        from .registry import AGENT_JOB_KEYS
        out: set[str] = set()
        for name in self.disabled_agents():
            out |= {name, name.replace("_", "-")}
            out |= set(AGENT_JOB_KEYS.get(name, ()))
        return sorted(out)

    def project(self, repo: str) -> Project | None:
        return next((p for p in self.projects if p.repo.lower() == repo.lower()), None)

    @property
    def owner(self) -> str:
        return self.hub.repo.split("/", 1)[0]
