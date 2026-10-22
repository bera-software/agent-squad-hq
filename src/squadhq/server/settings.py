"""Server settings, read from ``SQUADHQ_*`` environment variables.

Secrets can also be supplied as files: for any variable ``SQUADHQ_X`` the
server reads ``SQUADHQ_X_FILE`` when set (Docker/Kubernetes secret mounts).
See ``deploy/.env.example`` for the full list.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from squadhq.server.auth import ROLES, StaticToken, parse_static_tokens

Mode = Literal["byo-compute", "worker"]
WEBHOOK_SOURCES = ("github", "jira", "linear", "trello", "gitlab")


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    file_var = env.get(name + "_FILE")
    if file_var:
        try:
            with open(file_var, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as e:
            raise ValueError(f"{name}_FILE points to an unreadable file: {e}") from e
    return env.get(name, default)


def _bool(v: str, default: bool = False) -> bool:
    if v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(v: str, default: int) -> int:
    return int(v) if v.strip() else default


def _float(v: str, default: float) -> float:
    return float(v) if v.strip() else default


@dataclass
class ServerSettings:
    db_url: str = "sqlite:///squadhq.db"
    mode: Mode = "byo-compute"
    hub_repo: str = ""
    config_path: str = ""
    inline_config: dict[str, Any] | None = None  # tests / embedding: a parsed squadhq.yml
    config_text: str = ""  # SQUADHQ_CONFIG_YAML: squadhq.yml content (no config mount needed)
    static_tokens: list[StaticToken] = field(default_factory=list)
    webhook_secrets: dict[str, str] = field(default_factory=dict)
    trello_callback_url: str = ""
    max_body_bytes: int = 1_048_576
    webhook_max_body_bytes: int = 5_242_880
    metrics_public: bool = False
    docs_enabled: bool = False  # /docs + /openapi.json; SQUADHQ_API_DOCS=true
    required_checks: tuple[str, ...] = ("hub / ci-gate",)  # merge gate; SQUADHQ_REQUIRED_CHECKS (comma list)
    allow_fork_merges: bool = False
    required_checks_app: str = "github-actions"  # app slug that must own the required checks (GitHub)
    required_checks_workflow: str = ".github/workflows/agents.yml"  # workflow that must produce them ("" = any)
    run_max_skew_days: int = 7  # POST /runs started_at window
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    otel_enabled: bool = False
    # worker
    worker_id: str = ""
    worker_poll_s: float = 2.0
    worker_lease_s: float = 900.0
    job_max_attempts: int = 5
    retry_base_s: float = 10.0
    retry_max_s: float = 900.0
    module_timeout_s: float = 1800.0
    # byo-compute dispatch
    github_api_url: str = "https://api.github.com"
    github_token: str = ""
    dispatch_workflow: str = "squadhq-agent.yml"
    dispatch_ref: str = "main"
    # worker-mode executor container
    executor_image: str = "ghcr.io/OWNER/agent-squad-hq-executor:latest"
    job_cpus: str = "2"
    job_memory: str = "4g"
    job_pids_limit: int = 512
    job_network: str = "squadhq-egress"
    job_timeout_s: float = 3600.0
    container_runtime: str = "docker"
    # per-job result dirs; must be the same absolute path on the docker host and in the worker
    sandbox_workdir: str = ""
    # scheduler
    scheduler_interval_s: float = 20.0
    # serve
    host: str = "0.0.0.0"  # noqa: S104 - container default; override with SQUADHQ_HOST
    port: int = 8080
    trusted_proxies: str = "127.0.0.1"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        e: Mapping[str, str] = os.environ if env is None else env
        mode = _env(e, "SQUADHQ_MODE", "byo-compute")
        if mode not in ("byo-compute", "worker"):
            raise ValueError("SQUADHQ_MODE must be 'byo-compute' or 'worker'")
        log_format = _env(e, "SQUADHQ_LOG_FORMAT", "json")
        if log_format not in ("json", "text"):
            raise ValueError("SQUADHQ_LOG_FORMAT must be 'json' or 'text'")
        secrets = {}
        for src in WEBHOOK_SOURCES:
            v = _env(e, f"SQUADHQ_WEBHOOK_SECRET_{src.upper()}")
            if v:
                secrets[src] = v
        hub_repo = _env(e, "SQUADHQ_HUB_REPO") or e.get("HQ_REPO", "") or e.get("HUB_REPO", "")
        home = e.get("SQUADHQ_HOME", "")
        default_cfg = os.path.join(home, "squadhq.yml") if home else "squadhq.yml"
        return cls(
            db_url=_env(e, "SQUADHQ_DB_URL", "sqlite:///squadhq.db"),
            mode=mode,  # type: ignore[arg-type]
            hub_repo=hub_repo,
            config_path=_env(e, "SQUADHQ_CONFIG", default_cfg),
            config_text=_env(e, "SQUADHQ_CONFIG_YAML"),
            static_tokens=parse_static_tokens(_env(e, "SQUADHQ_API_TOKENS")),
            webhook_secrets=secrets,
            trello_callback_url=_env(e, "SQUADHQ_TRELLO_CALLBACK_URL"),
            max_body_bytes=_int(_env(e, "SQUADHQ_MAX_BODY_BYTES"), 1_048_576),
            webhook_max_body_bytes=_int(_env(e, "SQUADHQ_WEBHOOK_MAX_BODY_BYTES"), 5_242_880),
            metrics_public=_bool(_env(e, "SQUADHQ_METRICS_PUBLIC")),
            docs_enabled=_bool(_env(e, "SQUADHQ_API_DOCS") or _env(e, "SQUADHQ_DOCS_ENABLED"), False),
            required_checks=tuple(c.strip() for c in _env(e, "SQUADHQ_REQUIRED_CHECKS", "hub / ci-gate").split(",")
                                  if c.strip()),
            allow_fork_merges=_bool(_env(e, "SQUADHQ_ALLOW_FORK_MERGES")),
            required_checks_app=_env(e, "SQUADHQ_REQUIRED_CHECKS_APP", "github-actions"),
            required_checks_workflow=_env(e, "SQUADHQ_REQUIRED_CHECKS_WORKFLOW", ".github/workflows/agents.yml"),
            run_max_skew_days=_int(_env(e, "SQUADHQ_RUN_MAX_SKEW_DAYS"), 7),
            log_level=_env(e, "SQUADHQ_LOG_LEVEL", "INFO").upper(),
            log_format=log_format,  # type: ignore[arg-type]
            otel_enabled=_bool(_env(e, "SQUADHQ_OTEL_ENABLED")),
            worker_id=_env(e, "SQUADHQ_WORKER_ID"),
            worker_poll_s=_float(_env(e, "SQUADHQ_WORKER_POLL_S"), 2.0),
            worker_lease_s=_float(_env(e, "SQUADHQ_WORKER_LEASE_S"), 900.0),
            job_max_attempts=_int(_env(e, "SQUADHQ_JOB_MAX_ATTEMPTS"), 5),
            retry_base_s=_float(_env(e, "SQUADHQ_RETRY_BASE_S"), 10.0),
            retry_max_s=_float(_env(e, "SQUADHQ_RETRY_MAX_S"), 900.0),
            module_timeout_s=_float(_env(e, "SQUADHQ_MODULE_TIMEOUT_S"), 1800.0),
            github_api_url=_env(e, "SQUADHQ_GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            github_token=_env(e, "SQUADHQ_GITHUB_TOKEN"),
            dispatch_workflow=_env(e, "SQUADHQ_DISPATCH_WORKFLOW", "squadhq-agent.yml"),
            dispatch_ref=_env(e, "SQUADHQ_DISPATCH_REF", "main"),
            executor_image=_env(e, "SQUADHQ_EXECUTOR_IMAGE", "ghcr.io/OWNER/agent-squad-hq-executor:latest"),
            job_cpus=_env(e, "SQUADHQ_JOB_CPUS", "2"),
            job_memory=_env(e, "SQUADHQ_JOB_MEMORY", "4g"),
            job_pids_limit=_int(_env(e, "SQUADHQ_JOB_PIDS_LIMIT"), 512),
            job_network=_env(e, "SQUADHQ_JOB_NETWORK", "squadhq-egress"),
            job_timeout_s=_float(_env(e, "SQUADHQ_JOB_TIMEOUT_S"), 3600.0),
            container_runtime=_env(e, "SQUADHQ_CONTAINER_RUNTIME", "docker"),
            sandbox_workdir=_env(e, "SQUADHQ_SANDBOX_WORKDIR"),
            scheduler_interval_s=_float(_env(e, "SQUADHQ_SCHEDULER_INTERVAL_S"), 20.0),
            host=_env(e, "SQUADHQ_HOST", "0.0.0.0"),  # noqa: S104
            port=_int(_env(e, "SQUADHQ_PORT"), 8080),
            trusted_proxies=_env(e, "SQUADHQ_TRUSTED_PROXIES", "127.0.0.1"),
        )

    def validate(self) -> list[str]:
        """Human-readable configuration problems (empty list = fine)."""
        problems = []
        for t in self.static_tokens:
            if t.role not in ROLES:
                problems.append(f"token {t.name!r} has unknown role {t.role!r}")
        if self.mode == "byo-compute" and not self.github_token:
            problems.append("byo-compute mode needs SQUADHQ_GITHUB_TOKEN to dispatch workflows")
        if not self.webhook_secrets:
            problems.append("no SQUADHQ_WEBHOOK_SECRET_* set: every webhook will be rejected")
        return problems
