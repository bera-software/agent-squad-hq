"""StateStore contract for server mode (docs/adr/0008-pluggable-state-store.md).

Two implementations share one SQL code path (:mod:`squadhq.store.sql`):
SQLite (stdlib, the default) and Postgres (optional ``psycopg``). Everything
the API, worker and scheduler persist goes through this interface:

* a durable job queue (at-least-once, leases, retries, dead-letter, idempotency keys);
* key/value state with compare-and-set (``kv_cas``);
* leader-election locks with a TTL;
* approvals, runs, tickets, projects, briefs and API-token hashes;
* the append-only, hash-chained audit log.
"""
from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from squadhq.audit.chain import AuditEvent

JobStatus = Literal["queued", "running", "done", "dead"]
ApprovalStatus = Literal["pending", "approved", "rejected", "resolved"]
ApprovalKind = Literal["merge", "proposal", "deploy", "escalation"]

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None = None) -> str:
    """Sortable ISO-8601 UTC timestamp with a fixed width."""
    return (dt or utcnow()).astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    """Compare-and-set lost: the stored version is not the expected one."""


class NotFoundError(StoreError):
    pass


@dataclass
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: str
    created_at: str
    updated_at: str
    idempotency_key: str | None = None
    priority: int = 0
    locked_by: str | None = None
    locked_until: str | None = None
    last_error: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EnqueueResult:
    job_id: str
    created: bool


@dataclass
class Approval:
    id: str
    kind: str
    project: str
    subject: str  # e.g. "pr#12", "issue#40", "ENG-7"
    title: str
    url: str
    status: ApprovalStatus
    created_at: str
    updated_at: str
    data: dict[str, Any] = field(default_factory=dict)
    decided_by: str | None = None
    decided_at: str | None = None
    decision_comment: str = ""
    action_job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Run:
    id: str
    project: str
    role: str
    outcome: str
    started_at: str
    model: str = ""
    executor: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error_class: str = "none"
    job_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TicketRow:
    project: str
    key: str
    title: str
    state: str  # open | closed
    flow_state: str
    labels: list[str]
    url: str
    tracker: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Project:
    name: str
    repo: str
    tracker: str = "github"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TokenRecord:
    id: str
    name: str
    role: str
    token_hash: str
    created_at: str
    revoked: bool = False
    project: str = ""  # reporter tokens: the one project they may report for

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "role": self.role, "project": self.project or None,
                "created_at": self.created_at, "revoked": self.revoked}


@dataclass
class Brief:
    id: str
    created_at: str
    markdown: str
    project: str = ""


@dataclass
class Device:
    """A registered push target (Expo push token) owned by an API principal."""

    id: str
    expo_push_token: str
    platform: str
    name: str
    topics: list[str]
    owner: str
    created_at: str

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "platform": self.platform, "name": self.name, "topics": self.topics,
                "owner": self.owner, "created_at": self.created_at}


@dataclass
class IntakeRequest:
    """A human request ("build me ...") that becomes a tracker ticket."""

    id: str
    project: str
    title: str
    body: str
    source: str
    requested_by: str
    status: str  # queued | created | failed
    created_at: str
    updated_at: str
    tracker: str = "github"
    ticket_key: str | None = None
    ticket_url: str | None = None
    job_id: str | None = None
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "project": self.project, "status": self.status, "ticket_key": self.ticket_key,
                "ticket_url": self.ticket_url, "created_at": self.created_at, "source": self.source,
                "title": self.title}


class StateStore(abc.ABC):
    """Abstract store. All timestamps are UTC strings produced by :func:`ts`."""

    dialect: str = "abstract"

    # lifecycle ---------------------------------------------------------
    @abc.abstractmethod
    def migrate(self) -> int:
        """Apply pending schema migrations; return the resulting schema version."""

    @abc.abstractmethod
    def schema_version(self) -> int: ...

    @abc.abstractmethod
    def ping(self) -> bool: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    # queue -------------------------------------------------------------
    @abc.abstractmethod
    def enqueue(self, kind: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
                run_after: datetime | None = None, max_attempts: int = 5, priority: int = 0) -> EnqueueResult: ...

    @abc.abstractmethod
    def claim(self, worker_id: str, *, lease_s: float = 300.0, kinds: list[str] | None = None) -> Job | None: ...

    @abc.abstractmethod
    def extend_lease(self, job_id: str, worker_id: str, lease_s: float) -> bool: ...

    @abc.abstractmethod
    def complete(self, job_id: str, worker_id: str, result: dict[str, Any] | None = None) -> bool: ...

    @abc.abstractmethod
    def fail(self, job_id: str, worker_id: str, error: str, *, retry_at: datetime | None) -> bool:
        """Record a failed attempt; ``retry_at=None`` dead-letters the job."""

    @abc.abstractmethod
    def get_job(self, job_id: str) -> Job | None: ...

    @abc.abstractmethod
    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Job]: ...

    @abc.abstractmethod
    def retry_job(self, job_id: str) -> bool:
        """Move a dead-lettered job back to the queue with a fresh attempt budget."""

    @abc.abstractmethod
    def count_jobs(self) -> dict[str, int]: ...

    # kv + locks --------------------------------------------------------
    @abc.abstractmethod
    def kv_get(self, namespace: str, key: str) -> tuple[Any, int] | None: ...

    @abc.abstractmethod
    def kv_cas(self, namespace: str, key: str, value: Any, expected_version: int | None) -> int:
        """Write if the stored version equals ``expected_version`` (``None`` = must not exist).

        Returns the new version; raises :class:`ConflictError` otherwise.
        """

    @abc.abstractmethod
    def kv_put(self, namespace: str, key: str, value: Any) -> int: ...

    @abc.abstractmethod
    def acquire_lock(self, name: str, holder: str, ttl_s: float) -> bool: ...

    @abc.abstractmethod
    def release_lock(self, name: str, holder: str) -> None: ...

    # approvals ---------------------------------------------------------
    @abc.abstractmethod
    def upsert_approval(self, *, kind: str, project: str, subject: str, title: str, url: str,
                        data: dict[str, Any] | None = None) -> tuple[Approval, bool]:
        """Create a pending approval unless one is already pending for (kind, project, subject)."""

    @abc.abstractmethod
    def resolve_approvals(self, *, kind: str, project: str, subject: str, actor: str,
                          action: str = "approval.resolved_externally", data: dict[str, Any] | None = None) -> int:
        """Mark pending approvals as resolved outside the server (label removed)."""

    @abc.abstractmethod
    def get_approval(self, approval_id: str) -> Approval | None: ...

    @abc.abstractmethod
    def list_approvals(self, status: str | None = "pending", project: str | None = None,
                       limit: int = 200) -> list[Approval]: ...

    @abc.abstractmethod
    def decide_approval(self, approval_id: str, *, decision: Literal["approve", "reject"], actor: str,
                        comment: str, action_job: tuple[str, dict[str, Any]] | None) -> tuple[Approval, AuditEvent]:
        """Atomically: pending -> approved/rejected, append an audit event, enqueue the action job."""

    # runs / tickets / projects / briefs --------------------------------
    @abc.abstractmethod
    def record_run(self, run: Run) -> None: ...

    @abc.abstractmethod
    def list_runs(self, project: str | None = None, limit: int = 50) -> list[Run]: ...

    @abc.abstractmethod
    def cost_summary(self, month: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    def upsert_ticket(self, t: TicketRow) -> None: ...

    @abc.abstractmethod
    def list_tickets(self, project: str | None = None, state: str | None = None, limit: int = 500) -> list[TicketRow]: ...

    @abc.abstractmethod
    def upsert_project(self, p: Project) -> None: ...

    @abc.abstractmethod
    def list_projects(self) -> list[Project]: ...

    @abc.abstractmethod
    def add_brief(self, markdown: str, project: str = "") -> Brief: ...

    @abc.abstractmethod
    def latest_brief(self, project: str | None = None) -> Brief | None: ...

    # tokens ------------------------------------------------------------
    @abc.abstractmethod
    def add_token(self, name: str, role: str, token_hash: str, project: str = "") -> TokenRecord: ...

    @abc.abstractmethod
    def list_tokens(self, include_revoked: bool = False) -> list[TokenRecord]: ...

    @abc.abstractmethod
    def revoke_token(self, token_id: str, actor: str = "api") -> bool: ...

    # devices -----------------------------------------------------------
    @abc.abstractmethod
    def upsert_device(self, *, expo_push_token: str, platform: str, name: str, topics: list[str],
                      owner: str, allow_takeover: bool = False) -> tuple[Device, bool]:
        """Register (or re-register, keyed by push token) a device. Returns ``(device, created)``.

        Re-registering a push token owned by another ``owner`` raises
        :class:`ConflictError` unless ``allow_takeover``.
        """

    @abc.abstractmethod
    def get_device(self, device_id: str) -> Device | None: ...

    @abc.abstractmethod
    def delete_device(self, device_id: str) -> bool: ...

    @abc.abstractmethod
    def delete_devices_by_token(self, tokens: list[str]) -> int: ...

    @abc.abstractmethod
    def list_devices(self, topic: str | None = None) -> list[Device]: ...

    # intake requests ---------------------------------------------------
    @abc.abstractmethod
    def create_intake(self, *, project: str, title: str, body: str, source: str, requested_by: str,
                      tracker: str) -> IntakeRequest:
        """Atomically: record the request, enqueue its ``intake_ticket`` job, append an audit event."""

    @abc.abstractmethod
    def get_intake(self, intake_id: str) -> IntakeRequest | None: ...

    @abc.abstractmethod
    def update_intake(self, intake_id: str, *, status: str, ticket_key: str | None = None,
                      ticket_url: str | None = None, error: str = "") -> None: ...

    # audit -------------------------------------------------------------
    @abc.abstractmethod
    def append_audit(self, actor: str, action: str, subject: str, data: dict[str, Any] | None = None) -> AuditEvent: ...

    @abc.abstractmethod
    def iter_audit(self, since_seq: int = 0, since_ts: str | None = None,
                   limit: int | None = None) -> Iterator[AuditEvent]: ...
