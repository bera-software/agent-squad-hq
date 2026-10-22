"""Webhook intake (verify -> dedupe -> enqueue) and event -> control-plane mapping.

Verification and normalisation are delegated to
:mod:`squadhq.trackers.webhooks` (``verify`` / ``normalize_event``). The
server fails closed: no configured secret, a missing module, a bad or
missing signature all reject the delivery.

Idempotency: the queue's idempotency key is ``webhook:<source>:<delivery-id>``;
the delivery id is the source's header (``X-GitHub-Delivery``,
``Linear-Delivery``, ``X-Atlassian-Webhook-Identifier``) or, when the source
has none (Trello), the SHA-256 of the signed body.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from squadhq.store.base import TicketRow, ts

DELIVERY_HEADERS = {
    "github": "X-GitHub-Delivery",
    "linear": "Linear-Delivery",
    "jira": "X-Atlassian-Webhook-Identifier",
    "gitlab": "X-Gitlab-Event-UUID",
    "trello": "",
}

# label -> approval kind. Legacy names are accepted.
APPROVAL_LABELS: dict[str, str] = {
    "needs-approver-merge": "merge",
    "proposal:pending": "proposal",
    "deploy:pending": "deploy",
    "needs-approver-deploy": "deploy",
    "escalation": "escalation",
    "needs-human": "escalation",
}

DEFAULT_ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_PR_ACTIONS = {"pr_opened", "pr_updated", "pr_closed", "pr_merged", "check_completed"}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return None


def verify(source: str, headers: Mapping[str, str], body: bytes, secret: str, callback_url: str = "") -> bool:
    if not secret:
        return False
    try:
        from squadhq.trackers.webhooks import verify as _verify
    except ImportError:  # fail closed
        return False
    try:
        return bool(_verify(source, headers, body, secret=secret, callback_url=callback_url or None))
    except Exception:
        return False


def delivery_id(source: str, headers: Mapping[str, str], body: bytes) -> str:
    name = DELIVERY_HEADERS.get(source, "")
    v = _header(headers, name) if name else None
    if v and len(v) <= 200:
        return v.strip()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _jsonable(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _jsonable(getattr(v, f.name)) for f in dataclasses.fields(v) if f.name != "raw"}
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
        return v.value
    return v


def _raw_subset(source: str, event_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the raw fields the worker needs (push SHAs, merge flag, default branch)."""
    out: dict[str, Any] = {}
    repo = data.get("repository") if isinstance(data.get("repository"), Mapping) else {}
    if source == "github":
        sender = data.get("sender")
        if isinstance(sender, Mapping):
            out["sender_type"] = str(sender.get("type") or "")  # "Bot" proves an App identity
        if event_type == "push":
            out.update({"ref": data.get("ref"), "before": data.get("before"), "after": data.get("after"),
                        "default_branch": (repo or {}).get("default_branch")})
        pr = data.get("pull_request")
        if isinstance(pr, Mapping):
            out["merged"] = bool(pr.get("merged"))
            out["number"] = pr.get("number")
            out["title"] = str(pr.get("title") or "")
            out["url"] = str(pr.get("html_url") or "")
            head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
            out["head_sha"] = str((head or {}).get("sha") or "")
            head_repo = (head or {}).get("repo")
            out["head_repo"] = str(head_repo.get("full_name") or "") if isinstance(head_repo, Mapping) else ""
            out["is_pr"] = True
        issue = data.get("issue")
        if isinstance(issue, Mapping) and issue.get("pull_request"):
            out["is_pr"] = True  # an issue_comment / issues event on a PR: same subject as the PR
    return out


def normalize(source: str, headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
    """Normalised, JSON-serialisable event (``TrackerEvent`` minus ``raw``) for the queue."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        from squadhq.trackers.webhooks import normalize_event
    except ImportError:  # pragma: no cover - trackers ship with the core
        ev: dict[str, Any] = {"source": source, "action": "other", "project": "", "key": None, "actor": "",
                              "association": "NONE", "raw_type": "", "labels_added": [], "labels_removed": []}
    else:
        ev = _jsonable(normalize_event(source, headers, data))
    event_type = _header(headers, "X-GitHub-Event") or ""
    ev["raw_subset"] = _raw_subset(source, event_type, data)
    ev.setdefault("raw_type", event_type)
    if event_type == "push" and source == "github":
        ev["action"] = "push"
        repo = data.get("repository")
        if isinstance(repo, Mapping) and not ev.get("project"):
            ev["project"] = str(repo.get("full_name") or "")
    return ev


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def registered_projects(projects: list[Any], hub_repo: str = "") -> set[str]:
    """Lower-cased identifiers an event's ``project`` may match (repo, name, tracker keys)."""
    out = {hub_repo.lower()} if hub_repo else set()
    for p in projects:
        out.update({str(p.repo).lower(), str(p.name).lower()})
        for k in ("tracker_project", "jira_project", "linear_team", "trello_board", "gitlab_project"):
            v = (p.data or {}).get(k)
            if v:
                out.add(str(v).lower())
    out.discard("")
    return out


def flow_state(labels: list[str], fallback: str) -> str:
    """Flow-engine state name for a label set (config/flows.yml), or ``fallback``."""
    try:
        from squadhq import flow_reader as _fr

        fr: Any = _fr  # legacy, untyped module
        combo, mods = fr.resolve_state(labels, fr.get_label_states("issue"))
    except Exception:
        return fallback
    if not combo:
        return fallback
    name = str(combo["state"]).split(".", 1)[-1]
    return name + "".join(f" +{m}" for m in mods)


def ticket_row(ev: Mapping[str, Any]) -> TicketRow | None:
    t = ev.get("ticket")
    if not isinstance(t, Mapping) or not t.get("key"):
        return None
    labels = [str(x) for x in t.get("labels") or []]
    state = str(t.get("state") or "open")
    return TicketRow(project=str(t.get("project") or ev.get("project") or ""), key=str(t["key"]),
                     title=str(t.get("title") or ""), state=state,
                     flow_state=flow_state(labels, str(t.get("native_state") or state)), labels=labels,
                     url=str(t.get("url") or ""), tracker=str(ev.get("source") or ""),
                     updated_at=str(t.get("updated_at") or ts()))


def is_pr(ev: Mapping[str, Any]) -> bool:
    raw = ev.get("raw_subset") if isinstance(ev.get("raw_subset"), Mapping) else {}
    return ev.get("action") in _PR_ACTIONS or bool((raw or {}).get("is_pr"))


def subject_for(ev: Mapping[str, Any]) -> str:
    """Canonical approval subject: ``pr#N`` for anything about a PR (incl. comments on it), else ``issue#N``."""
    key = str(ev.get("key") or "")
    if ev.get("source") in ("github", "gitlab"):
        return f"{'pr' if is_pr(ev) else 'issue'}#{key}"
    return key


_LABEL_EVENT_SUFFIXES = (".labeled", "addLabelToCard")


def is_label_added_event(ev: Mapping[str, Any]) -> bool:
    raw_type = str(ev.get("raw_type") or "")
    return ev.get("action") == "labels_changed" or raw_type.endswith(_LABEL_EVENT_SUFFIXES) \
        or (ev.get("source") in ("gitlab", "linear") and bool(ev.get("labels_added")))


def is_head_push(ev: Mapping[str, Any]) -> bool:
    """New commits on a PR head (``pull_request.synchronize`` / GitLab MR update with new commits)."""
    raw_type = str(ev.get("raw_type") or "")
    return raw_type in ("pull_request.synchronize", "merge_request.update.push") or \
        (raw_type == "merge_request.update" and bool((ev.get("raw_subset") or {}).get("new_commits")))


def approval_changes(ev: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(kinds requested, kinds no longer requested).

    Approvals are requested **only** by the event that adds the label, never
    re-derived from a ticket's current labels (a comment would otherwise
    re-create, and re-pin, an approval). ``merge`` approvals come only from a
    PR label event (``pull_request.labeled`` / a GitLab MR label change).
    """
    added = {str(x) for x in ev.get("labels_added") or []}
    removed = {str(x) for x in ev.get("labels_removed") or []}
    want: set[str] = set()
    if is_label_added_event(ev):
        want = {APPROVAL_LABELS[label] for label in added if label in APPROVAL_LABELS}
        if "merge" in want and not (ev.get("source") in ("github", "gitlab") and is_pr(ev)
                                    and str(ev.get("action")).startswith("pr_")):
            want.discard("merge")
    drop = {APPROVAL_LABELS[label] for label in removed if label in APPROVAL_LABELS} - want
    if ev.get("action") in ("ticket_closed", "pr_closed", "pr_merged"):
        t = ev.get("ticket")
        current = {str(x) for x in (t.get("labels") or [])} if isinstance(t, Mapping) else set()
        drop |= want | {APPROVAL_LABELS[label] for label in current if label in APPROVAL_LABELS}
        want = set()
    return sorted(want), sorted(drop)


def may_trigger(ev: Mapping[str, Any], allowed: frozenset[str], bot_logins: set[str],
                trusted_actors: frozenset[str] = frozenset()) -> bool:
    """Decided by code, never by a model (CONVENTIONS: untrusted input).

    GitHub/Linear payloads prove an author association. Jira, Trello and GitLab
    events always arrive with ``NONE``; for those only our bot and the
    explicitly configured ``SQUADHQ_TRUSTED_ACTORS`` (tracker account ids /
    usernames of approvers) may trigger agent work. Fail closed otherwise.
    """
    actor = str(ev.get("actor") or "")
    # Our bot is a GitHub App identity: match the full ``slug[bot]`` login, only on GitHub events, and only
    # when GitHub says the sender is a Bot (a user cannot register a login ending in "[bot]", but be explicit).
    raw = ev.get("raw_subset") if isinstance(ev.get("raw_subset"), Mapping) else {}
    if actor and ev.get("source") == "github" and actor in bot_logins and (raw or {}).get("sender_type") == "Bot":
        return True
    if actor and actor.lower() in trusted_actors:
        return True
    return str(ev.get("association") or "NONE") in allowed


def agent_roles(ev: Mapping[str, Any]) -> list[str]:
    return sorted(str(label).split(":", 1)[1] for label in ev.get("labels_added") or []
                  if str(label).startswith("agent:") and len(str(label)) > len("agent:"))
