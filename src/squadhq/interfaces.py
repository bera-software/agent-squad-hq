"""Contracts between the control plane and the outside world.

Adapters (LLM providers, issue trackers, code hosts, notifiers, coding-agent
executors) implement these protocols. The control plane depends only on this
module, never on a concrete adapter — pick one through the registry in the
matching subpackage (``squadhq.providers.get_provider(name)`` etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Errors shared by every adapter
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Base class. ``transient`` tells the caller whether a retry can help.

    Optional keyword context (all default to "unknown"): ``status`` (HTTP
    status, if any), ``body`` (truncated response body), ``retry_after``
    (seconds, from a ``Retry-After`` header), ``model_specific`` (the failure
    concerns this model, not the provider — a fallback model may succeed) and
    ``provider_wide`` (auth/billing/outage for the whole provider — only a
    fallback on a *different* provider can help).
    """

    transient = False

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        body: str = "",
        retry_after: float | None = None,
        model_specific: bool = False,
        provider_wide: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after = retry_after
        self.model_specific = model_specific
        self.provider_wide = provider_wide


class TransientError(AdapterError):
    """Rate limit, overload, timeout, 5xx — retry with backoff (same target)."""

    transient = True


class FatalError(AdapterError):
    """Bad request, auth failure, billing, 4xx — retrying will not help."""


class CapacityError(TransientError):
    """Subscription / quota exhausted. The capacity breaker should trip."""


# ---------------------------------------------------------------------------
# LLM providers (hub loops: single prompt -> single reply)
# ---------------------------------------------------------------------------


class Capability(str, Enum):
    WEB_SEARCH = "web_search"
    TOOLS = "tools"
    JSON_MODE = "json_mode"
    THINKING = "thinking"


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMReply:
    text: str
    usage: Usage
    stop_reason: str  # normalised: "end", "max_tokens", "tool_use", "refusal", "other"
    model: str
    provider: str
    raw_stop_reason: str = ""
    # Provider-native response body, kept for diagnosis (never compared).
    raw: Any = field(default=None, compare=False, repr=False)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        max_tokens: int,
        web_search: bool = False,
        json_mode: bool = False,
        timeout: float = 120.0,
    ) -> LLMReply: ...

    def capabilities(self, model: str) -> set[Capability]: ...

    def price(self, model: str, usage: Usage) -> Decimal | None:
        """USD cost of one call; ``None`` when the model is not in the price catalogue."""
        ...


# ---------------------------------------------------------------------------
# Coding-agent executors (multi-turn, tool-using, edits a checkout)
# ---------------------------------------------------------------------------


class ErrorClass(str, Enum):
    NONE = "none"
    TRANSIENT = "transient"
    INFRA = "infra"
    AGENT = "agent"
    POLICY = "policy"
    CAPACITY = "capacity"
    UNKNOWN = "unknown"


ToolProfile = Literal["read-only", "comment", "edit-test", "edit-test-push"]


@dataclass
class ExecutionResult:
    """Neutral result every executor emits (``execution-result.json``).

    The ledger, failure handler and capacity gate read only this — never an
    executor's native output format.
    """

    executor: str
    role: str
    model: str
    outcome: Literal["success", "failure", "skipped"]
    error_class: ErrorClass = ErrorClass.NONE
    error_message: str = ""
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    duration_s: float | None = None
    capacity_tripped: bool = False
    transcript_path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Pool account id the run used ("" = the single configured credential).
    # Set by the trusted bookkeeping side (normalize --account), never by the agent.
    account: str = ""


# ---------------------------------------------------------------------------
# Issue trackers (GitHub Issues, Jira, Linear, Trello, GitLab, Azure Boards)
# ---------------------------------------------------------------------------

AuthorAssociation = Literal["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE", "BOT"]


@dataclass
class Comment:
    id: str
    author: str
    association: AuthorAssociation
    body: str
    created_at: str  # ISO-8601 UTC


@dataclass
class Ticket:
    """A tracker item normalised to the shape the flow engine consumes.

    ``labels`` holds every label/tag/component the tracker exposes; for trackers
    with native workflow states (Jira, Linear) ``native_state`` carries the raw
    status and the adapter's status map derives the matching flow labels.
    """

    key: str  # "42" on GitHub, "ENG-42" on Jira/Linear, card id on Trello
    project: str  # repo "owner/name", Jira project key, Linear team key, Trello board id
    title: str
    body: str
    state: Literal["open", "closed"]
    labels: list[str]
    author: str
    association: AuthorAssociation
    url: str
    created_at: str
    updated_at: str
    native_state: str | None = None
    assignees: list[str] = field(default_factory=list)


@runtime_checkable
class IssueTracker(Protocol):
    name: str

    def list_tickets(
        self, project: str, *, state: Literal["open", "closed", "all"] = "open",
        labels: Iterable[str] = (), limit: int = 500,
    ) -> list[Ticket]: ...

    def get_ticket(self, project: str, key: str) -> Ticket: ...

    def list_comments(self, project: str, key: str) -> list[Comment]: ...

    def create_ticket(self, project: str, title: str, body: str, labels: Iterable[str] = ()) -> Ticket: ...

    def comment(self, project: str, key: str, body: str) -> Comment: ...

    def add_labels(self, project: str, key: str, labels: Iterable[str]) -> None: ...

    def remove_labels(self, project: str, key: str, labels: Iterable[str]) -> None: ...

    def close_ticket(self, project: str, key: str, reason: Literal["completed", "not_planned"] = "completed") -> None: ...


@dataclass
class TrackerEvent:
    """A tracker/code-host webhook delivery normalised for the flow engine.

    ``action`` is one of: ``ticket_opened``, ``ticket_updated``,
    ``ticket_closed``, ``ticket_reopened``, ``labels_changed``,
    ``state_changed``, ``comment_created``, ``pr_opened``, ``pr_updated``,
    ``pr_closed``, ``pr_merged``, ``check_completed``, ``ping``, ``other``.
    ``text`` (comment/ticket body) is untrusted — fence it before prompting.
    """

    source: str  # github | jira | linear | trello | gitlab
    action: str
    project: str
    key: str | None
    actor: str
    association: AuthorAssociation
    raw_type: str  # native event name, e.g. "issue_comment.created", "jira:issue_updated"
    delivery_id: str | None = None
    text: str | None = None
    labels_added: list[str] = field(default_factory=list)
    labels_removed: list[str] = field(default_factory=list)
    native_state: str | None = None
    previous_native_state: str | None = None
    ticket: Ticket | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Code hosts (GitHub, GitLab, Bitbucket)
# ---------------------------------------------------------------------------


@dataclass
class PullRequest:
    number: str
    repo: str
    title: str
    body: str
    state: Literal["open", "closed", "merged"]
    author: str
    author_is_bot: bool
    head_ref: str
    base_ref: str
    labels: list[str]
    draft: bool
    url: str
    changed_files: list[str] = field(default_factory=list)
    head_sha: str | None = None
    head_repo: str | None = None  # "owner/name" of the head; differs from ``repo`` for forks


@dataclass
class CheckRun:
    name: str
    status: Literal["queued", "in_progress", "completed"]
    conclusion: str | None  # success, failure, neutral, skipped, cancelled, timed_out, None


@runtime_checkable
class CodeHost(Protocol):
    name: str

    def list_pull_requests(self, repo: str, *, state: Literal["open", "closed", "all"] = "open",
                           limit: int = 200) -> list[PullRequest]: ...

    def get_pull_request(self, repo: str, number: str) -> PullRequest: ...

    def list_checks(self, repo: str, ref: str) -> list[CheckRun]: ...

    def read_file(self, repo: str, path: str, ref: str | None = None) -> tuple[str, str]:
        """Return ``(content, version)``; ``version`` is the blob sha / etag for CAS writes."""
        ...

    def write_file(self, repo: str, path: str, content: str, message: str,
                   expected_version: str | None) -> str:
        """Compare-and-set write. Raise ``TransientError`` on version conflict."""
        ...

    def merge(self, repo: str, number: str, method: Literal["squash", "merge", "rebase"] = "squash") -> None: ...


# ---------------------------------------------------------------------------
# Notifiers (GitHub issue, Slack, Teams, email, webhook, PagerDuty, push)
# ---------------------------------------------------------------------------

Severity = Literal["info", "warning", "critical"]


@dataclass
class Notification:
    title: str
    body_markdown: str
    severity: Severity = "info"
    kind: Literal["escalation", "digest", "approval", "incident", "report"] = "report"
    url: str | None = None
    dedupe_key: str | None = None
    project: str | None = None


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None: ...
