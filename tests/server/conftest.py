from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from squadhq.interfaces import CheckRun, Comment, PullRequest, Ticket
from squadhq.server.auth import StaticToken, hash_token
from squadhq.server.integrations import ModuleResult, ScopedToken
from squadhq.server.sandbox import FakeContainerRunner
from squadhq.server.settings import ServerSettings
from squadhq.server.worker import Worker, WorkerDeps
from squadhq.store.sqlite import SQLiteStore

HEAD_SHA = "a" * 40
ADMIN, APPROVER, VIEWER = "sqh_admin-token", "sqh_approver-token", "sqh_viewer-token"
GH_SECRET = "gh-test-secret"
INLINE_CONFIG = {
    "org": {"timezone": "Europe/London", "working_hours": "07:00-20:00", "working_days": "mon-fri"},
    "hub": {"repo": "acme/agent-squad-hq"},
    "schedules": {"daily_brief": "weekdays 07:30"},
    "projects": [{"name": "Storefront", "repo": "acme/storefront", "packs": ["node-next"]},
                 {"name": "Billing", "repo": "acme/billing", "budget_usd": 25}],
    "budgets": {"monthly_usd": 100, "per_project_usd": {"default": 40, "acme/legacy": 5}},
}


_LEAKY_ENV = ("SQUADHQ_TOKEN_MINTER", "SQUADHQ_TRUSTED_ASSOCIATIONS", "SQUADHQ_ALLOWED_ASSOCIATIONS",
              "SQUADHQ_TRUSTED_ACTORS", "SQUADHQ_BOT_LOGIN", "SQUADHQ_BOT_ALIASES", "SQUADHQ_REQUIRED_CHECKS",
              "SQUADHQ_ALLOW_FORK_MERGES", "SQUADHQ_DB_URL", "SQUADHQ_CONFIG", "SQUADHQ_CONFIG_YAML",
              "SQUADHQ_HUB_REPO", "SQUADHQ_MODE", "SQUADHQ_API_DOCS", "EXPO_ACCESS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server tests never see env set by other suites or the developer's shell."""
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)


def gh_sign(body: bytes, secret: str = GH_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_settings(**kw: Any) -> ServerSettings:
    base = dict(
        db_url="sqlite://:memory:",
        hub_repo="acme/agent-squad-hq",
        inline_config=INLINE_CONFIG,
        static_tokens=[StaticToken("admin", "admin", hash_token(ADMIN)),
                       StaticToken("approver", "approver", hash_token(APPROVER)),
                       StaticToken("viewer", "viewer", hash_token(VIEWER))],
        webhook_secrets={"github": GH_SECRET, "linear": "lin-secret", "jira": "jira-secret",
                         "trello": "trello-secret"},
        trello_callback_url="https://hq.example.com/api/v1/webhooks/trello",
        log_format="text",
        retry_base_s=1.0,
        retry_max_s=60.0,
        github_token="test-token",
    )
    base.update(kw)
    return ServerSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> ServerSettings:
    return make_settings()


@pytest.fixture
def store() -> SQLiteStore:
    from squadhq.store.base import Project

    s = SQLiteStore(":memory:")
    s.migrate()
    s.upsert_project(Project("A", "acme/a"))  # webhook tests target acme/a (unregistered repos are dropped)
    return s


@pytest.fixture
def integrations() -> FakeIntegrations:
    """One fake shared by the API client and the worker rig: tests never reach a real code host."""
    return FakeIntegrations()


@pytest.fixture
def client(settings: ServerSettings, store: SQLiteStore, integrations: FakeIntegrations) -> Any:
    from fastapi.testclient import TestClient

    from squadhq.server.app import create_app

    app = create_app(settings, store=store, integrations=integrations)  # type: ignore[arg-type]
    return TestClient(app)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeTracker:
    name: str = "github"
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    tickets: list[Ticket] = field(default_factory=list)

    def list_tickets(self, project: str, *, state: str = "open", labels: Any = (), limit: int = 500) -> list[Ticket]:
        self.calls.append(("list_tickets", project, tuple(labels)))
        return list(self.tickets)

    def get_ticket(self, project: str, key: str) -> Ticket:  # pragma: no cover
        raise NotImplementedError

    def list_comments(self, project: str, key: str) -> list[Comment]:  # pragma: no cover
        return []

    def create_ticket(self, project: str, title: str, body: str, labels: Any = ()) -> Ticket:  # pragma: no cover
        raise NotImplementedError

    def comment(self, project: str, key: str, body: str) -> Comment:
        self.calls.append(("comment", project, key, body))
        return Comment(id="1", author="bot", association="BOT", body=body, created_at="")

    def add_labels(self, project: str, key: str, labels: Any) -> None:
        self.calls.append(("add_labels", project, key, list(labels)))

    def remove_labels(self, project: str, key: str, labels: Any) -> None:
        self.calls.append(("remove_labels", project, key, list(labels)))

    def close_ticket(self, project: str, key: str, reason: str = "completed") -> None:  # pragma: no cover
        self.calls.append(("close", project, key))


@dataclass
class FakeCodeHost:
    name: str = "github"
    pr_state: str = "open"
    draft: bool = False
    head_sha: str = HEAD_SHA
    fork: bool = False
    checks: list[CheckRun] = field(default_factory=lambda: [CheckRun("hub / ci-gate", "completed", "success")])
    merged: list[tuple[str, str, str]] = field(default_factory=list)
    merged_sha: list[str | None] = field(default_factory=list)
    checks_ref: list[str] = field(default_factory=list)

    def list_pull_requests(self, repo: str, *, state: str = "open", limit: int = 200) -> list[PullRequest]:  # pragma: no cover
        return []

    def get_pull_request(self, repo: str, number: str) -> PullRequest:
        return PullRequest(number=number, repo=repo, title="t", body="", state=self.pr_state,  # type: ignore[arg-type]
                           author="squadhq-ops[bot]", author_is_bot=True, head_ref="feat", base_ref="main",
                           labels=[], draft=self.draft, url="")

    def pull_request_head(self, repo: str, number: str) -> Any:
        from squadhq.codehost.github import PullHead

        return PullHead(sha=self.head_sha, repo="someone/fork" if self.fork else repo, fork=self.fork)

    def list_checks(self, repo: str, ref: str) -> list[CheckRun]:
        self.checks_ref.append(ref)
        return list(self.checks)

    def read_file(self, repo: str, path: str, ref: str | None = None) -> tuple[str, str]:  # pragma: no cover
        return "", ""

    def write_file(self, repo: str, path: str, content: str, message: str,
                   expected_version: str | None) -> str:  # pragma: no cover
        return ""

    def merge(self, repo: str, number: str, method: str = "squash", sha: str | None = None) -> None:
        self.merged.append((repo, number, method))
        self.merged_sha.append(sha)


@dataclass
class FakeIntegrations:
    trackers: dict[str, FakeTracker] = field(default_factory=dict)
    host: FakeCodeHost = field(default_factory=FakeCodeHost)

    def tracker(self, name: str) -> FakeTracker:
        return self.trackers.setdefault(name, FakeTracker(name))

    def code_host(self, name: str) -> FakeCodeHost:
        return self.host


@dataclass
class FakeModules:
    returncode: int = 0
    calls: list[tuple[str, list[str], dict[str, str]]] = field(default_factory=list)

    def run(self, module: str, args: list[str], *, env: Any, timeout_s: float) -> ModuleResult:
        self.calls.append((module, list(args), dict(env)))
        return ModuleResult(self.returncode, "ok", "boom" if self.returncode else "", 0.01)


@dataclass
class FakeDispatcher:
    calls: list[tuple[str, str, str, dict[str, str]]] = field(default_factory=list)
    error: Exception | None = None

    def dispatch(self, repo: str, workflow: str, ref: str, inputs: Any) -> None:
        if self.error:
            raise self.error
        self.calls.append((repo, workflow, ref, dict(inputs)))


class FakeTokens:
    def mint(self, repo: str, role: str) -> list[ScopedToken]:
        return [ScopedToken("GH_TOKEN", "ghs_scoped")]


@dataclass
class Rig:
    worker: Worker
    integrations: FakeIntegrations
    modules: FakeModules
    dispatcher: FakeDispatcher
    containers: FakeContainerRunner


def make_rig(store: SQLiteStore, settings: ServerSettings, integ: FakeIntegrations | None = None,
             **deps_kw: Any) -> Rig:
    integ = integ or FakeIntegrations()
    mods, disp, cont = FakeModules(), FakeDispatcher(), FakeContainerRunner()
    deps = WorkerDeps(integrations=integ, modules=mods, dispatcher=disp, containers=cont, tokens=FakeTokens(),
                      bot_logins={"squadhq-ops[bot]"}, **deps_kw)
    w = Worker(store, settings, deps, worker_id="w-test", rng=lambda: 0.5)
    return Rig(w, integ, mods, disp, cont)


@pytest.fixture
def rig(store: SQLiteStore, settings: ServerSettings, integrations: FakeIntegrations) -> Rig:
    return make_rig(store, settings, integrations)


def gh_payload(event: str, delivery: str = "d-1", **payload: Any) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": gh_sign(body),
                  "Content-Type": "application/json"}
