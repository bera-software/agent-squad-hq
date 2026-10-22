"""Webhook signature verification and payload normalisation.

The server's ``/api/v1/webhooks/<source>`` handlers call, in order::

    if not verify(source, request.headers, raw_body, secret=..., callback_url=...):
        return 401
    event = normalize_event(source, request.headers, raw_body)

Verification helpers (all constant-time, all return ``False`` on a missing
or malformed header, never raise):

* :func:`verify_github` — ``X-Hub-Signature-256: sha256=<hex HMAC-SHA256(body)>``
* :func:`verify_jira` — ``X-Hub-Signature: sha256=<hex>`` (Jira Cloud admin
  webhooks created with a secret); bare hex accepted. Connect-app JWT is not
  handled here.
* :func:`verify_linear` — ``Linear-Signature: <hex HMAC-SHA256(body)>`` plus
  an optional ``webhookTimestamp`` replay window.
* :func:`verify_trello` — ``X-Trello-Webhook: base64(HMAC-SHA1(body + callbackURL))``
  keyed with the Trello app secret.
* :func:`verify_gitlab` — ``X-Gitlab-Token`` equals the configured secret.

``normalize_event`` never raises on an unknown or malformed event: it
returns ``action="other"``.

Association (who may trigger agent work) is only claimed when the payload
proves it: GitHub's ``author_association``; Linear ``MEMBER`` only for a
``user`` actor whose payload says ``guest: false`` (Linear guests are
outside collaborators) or who is listed in ``SQUADHQ_TRUSTED_ACTORS``;
Trello ``MEMBER`` only for an ``idMemberCreator``/username listed in
``SQUADHQ_TRUSTED_ACTORS`` (Trello payloads do not say whether the creator
belongs to the board). Everything else is ``NONE``. Text fields (``text``, ticket bodies) are
untrusted. Actor association is what the payload proves; when it proves
nothing (Jira/Trello/GitLab comments) it is ``NONE`` and the caller should
resolve trust through the tracker (e.g. ``GitLabTracker.association_for``)
or the configured approver list.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from typing import Any

from squadhq.interfaces import AuthorAssociation, TrackerEvent
from squadhq.trackers import status_maps as sm
from squadhq.trackers._common import association

SOURCES = ("github", "jira", "linear", "trello", "gitlab")

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return None


def _hmac_hex(secret: str, body: bytes, algo: Any = hashlib.sha256) -> str:
    return hmac.new(secret.encode(), body, algo).hexdigest()


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def verify_github(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    return _eq(signature_header.split("=", 1)[1].strip().lower(), _hmac_hex(secret, body))


def verify_jira(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header:
        return False
    sig = signature_header.strip()
    if "=" in sig:
        method, _, sig = sig.partition("=")
        if method.lower() != "sha256":
            return False
    return _eq(sig.lower(), _hmac_hex(secret, body))


def verify_linear(secret: str, body: bytes, signature_header: str | None, *, timestamp_ms: int | None = None,
                  now_ms: int | None = None, tolerance_s: int = 60) -> bool:
    if not secret or not signature_header:
        return False
    if not _eq(signature_header.strip().lower(), _hmac_hex(secret, body)):
        return False
    if timestamp_ms is not None:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if abs(now - int(timestamp_ms)) > tolerance_s * 1000:
            return False
    return True


def trello_signature(secret: str, body: bytes, callback_url: str) -> str:
    """The ``X-Trello-Webhook`` value Trello would send for this body."""
    digest = hmac.new(secret.encode(), body + callback_url.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_trello(secret: str, body: bytes, callback_url: str, signature_header: str | None) -> bool:
    if not secret or not signature_header or not callback_url:
        return False
    return _eq(signature_header.strip(), trello_signature(secret, body, callback_url))


def verify_gitlab(secret: str, token_header: str | None) -> bool:
    if not secret or token_header is None:
        return False
    return _eq(token_header, secret)


def verify(source: str, headers: Mapping[str, str], body: bytes, *, secret: str,
           callback_url: str | None = None) -> bool:
    """Dispatch to the right verifier. ``callback_url`` is required for Trello."""
    src = source.lower()
    if src == "github":
        return verify_github(secret, body, header(headers, "X-Hub-Signature-256"))
    if src == "jira":
        return verify_jira(secret, body, header(headers, "X-Hub-Signature"))
    if src == "linear":
        ts: int | None = None
        try:
            parsed = json.loads(body or b"{}")
            raw_ts = parsed.get("webhookTimestamp") if isinstance(parsed, dict) else None
            ts = int(raw_ts) if raw_ts is not None else None
        except (ValueError, TypeError):
            ts = None
        return verify_linear(secret, body, header(headers, "Linear-Signature"), timestamp_ms=ts)
    if src == "trello":
        return verify_trello(secret, body, callback_url or "", header(headers, "X-Trello-Webhook"))
    if src == "gitlab":
        return verify_gitlab(secret, header(headers, "X-Gitlab-Token"))
    return False


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _load(body: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(body, Mapping):
        return dict(body)
    try:
        data = json.loads(body or b"{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _other(source: str, raw_type: str, data: dict[str, Any], delivery: str | None) -> TrackerEvent:
    return TrackerEvent(source=source, action="other", project="", key=None, actor="", association="NONE",
                        raw_type=raw_type, delivery_id=delivery, raw=data)


def _github(headers: Mapping[str, str], data: dict[str, Any]) -> TrackerEvent:
    from squadhq.trackers.github import ticket_from_issue

    event = header(headers, "X-GitHub-Event") or ""
    act = str(data.get("action") or "")
    delivery = header(headers, "X-GitHub-Delivery")
    repo = str((data.get("repository") or {}).get("full_name") or "")
    sender = data.get("sender") or {}
    actor = str(sender.get("login") or "")
    raw_type = f"{event}.{act}" if act else event
    base: dict[str, Any] = {"source": "github", "project": repo, "actor": actor, "raw_type": raw_type,
            "delivery_id": delivery, "raw": data}

    def assoc_for(obj: Mapping[str, Any]) -> AuthorAssociation:
        if sender.get("type") == "Bot":
            return "BOT"
        user = obj.get("user") or {}
        if user.get("login") == actor:
            return association(obj.get("author_association"))
        return "NONE"

    if event == "ping":
        return TrackerEvent(action="ping", key=None, association="NONE", **base)
    if event == "issues":
        issue = data.get("issue") or {}
        mapping = {"opened": "ticket_opened", "closed": "ticket_closed", "reopened": "ticket_reopened",
                   "labeled": "labels_changed", "unlabeled": "labels_changed"}
        label = str((data.get("label") or {}).get("name") or "")
        return TrackerEvent(
            action=mapping.get(act, "ticket_updated"), key=str(issue.get("number", "")) or None,
            association=assoc_for(issue), text=str(issue.get("body") or "") if act == "opened" else None,
            labels_added=[label] if act == "labeled" and label else [],
            labels_removed=[label] if act == "unlabeled" and label else [],
            ticket=ticket_from_issue(repo, issue) if issue else None, **base)
    if event == "issue_comment":
        issue = data.get("issue") or {}
        comment = data.get("comment") or {}
        action = "comment_created" if act == "created" else "other"
        assoc: AuthorAssociation = ("BOT" if (comment.get("user") or {}).get("type") == "Bot"
                                    else association(comment.get("author_association")))
        return TrackerEvent(action=action, key=str(issue.get("number", "")) or None, association=assoc,
                            text=str(comment.get("body") or ""),
                            ticket=ticket_from_issue(repo, issue) if issue else None, **base)
    if event == "pull_request":
        pr = data.get("pull_request") or {}
        if act in ("opened", "reopened", "ready_for_review"):
            action = "pr_opened"
        elif act == "closed":
            action = "pr_merged" if pr.get("merged") else "pr_closed"
        else:
            action = "pr_updated"
        label = str((data.get("label") or {}).get("name") or "")
        return TrackerEvent(action=action, key=str(pr.get("number") or data.get("number") or "") or None,
                            association=assoc_for(pr), text=str(pr.get("body") or "") if act == "opened" else None,
                            labels_added=[label] if act == "labeled" and label else [],
                            labels_removed=[label] if act == "unlabeled" and label else [], **base)
    if event == "check_run":
        run = data.get("check_run") or {}
        prs = run.get("pull_requests") or []
        return TrackerEvent(action="check_completed" if act == "completed" else "other",
                            key=str(prs[0].get("number")) if prs else None, association="NONE",
                            native_state=str(run.get("conclusion") or "") or None, **base)
    return TrackerEvent(action="other", key=None, association="NONE", **base)


def _jira_user(u: Mapping[str, Any] | None) -> tuple[str, AuthorAssociation]:
    from squadhq.trackers.jira import _user

    return _user(u)


def _jira(headers: Mapping[str, str], data: dict[str, Any]) -> TrackerEvent:
    from squadhq.trackers.jira import JiraTracker

    ev = str(data.get("webhookEvent") or "")
    delivery = header(headers, "X-Atlassian-Webhook-Identifier")
    issue = data.get("issue") or {}
    self_url = str(issue.get("self") or "")
    base_url = self_url.split("/rest/", 1)[0] if "/rest/" in self_url else ""
    ticket = JiraTracker(base_url=base_url or "https://jira.invalid")._ticket(issue) if issue else None
    key = str(issue.get("key") or "") or None
    project = ticket.project if ticket else ""
    actor, assoc = _jira_user(data.get("user"))
    common: dict[str, Any] = {"source": "jira", "project": project, "key": key, "raw_type": ev, "delivery_id": delivery,
              "ticket": ticket, "raw": data}
    if ev == "jira:issue_created":
        return TrackerEvent(action="ticket_opened", actor=actor, association=assoc,
                            text=ticket.body if ticket else None, native_state=ticket.native_state if ticket else None,
                            **common)
    if ev == "comment_created":
        comment = data.get("comment") or {}
        from squadhq.trackers.adf import adf_to_markdown

        c_actor, c_assoc = _jira_user(comment.get("author") or data.get("user"))
        return TrackerEvent(action="comment_created", actor=c_actor, association=c_assoc,
                            text=adf_to_markdown(comment.get("body")), **common)
    if ev == "jira:issue_updated":
        added: list[str] = []
        removed: list[str] = []
        new_state = prev_state = None
        for item in (data.get("changelog") or {}).get("items") or []:
            fld = str(item.get("field") or "").lower()
            if fld == "status":
                prev_state, new_state = item.get("fromString"), item.get("toString")
            elif fld == "labels":
                before = set(str(item.get("fromString") or "").split())
                after = set(str(item.get("toString") or "").split())
                added += sorted(after - before)
                removed += sorted(before - after)
        if new_state is not None:
            old_l = set(sm.native_to_labels("jira", prev_state))
            new_l = set(sm.native_to_labels("jira", new_state))
            added += sorted(new_l - old_l)
            removed += sorted(old_l - new_l)
            action = "ticket_closed" if ticket and ticket.state == "closed" else "state_changed"
        elif added or removed:
            action = "labels_changed"
        else:
            action = "ticket_updated"
        return TrackerEvent(action=action, actor=actor, association=assoc, labels_added=added,
                            labels_removed=removed, native_state=new_state, previous_native_state=prev_state,
                            **common)
    return TrackerEvent(action="other", actor=actor, association=assoc, **common)


def _trusted_actor(*ids: Any) -> bool:
    raw = os.environ.get("SQUADHQ_TRUSTED_ACTORS", "")
    trusted = {v.strip().lower() for v in raw.split(",") if v.strip()}
    return any(str(i).strip().lower() in trusted for i in ids if i)


def _linear_assoc(user: Mapping[str, Any]) -> AuthorAssociation:
    if not user:
        return "NONE"
    if str(user.get("type", "user")) != "user":
        return "BOT"  # OauthClient / Integration
    if user.get("guest") is False or _trusted_actor(user.get("id"), user.get("email"), user.get("name")):
        return "MEMBER"
    return "NONE"  # guest, or membership not proven by the payload


def _linear(headers: Mapping[str, str], data: dict[str, Any]) -> TrackerEvent:
    kind = str(data.get("type") or header(headers, "Linear-Event") or "")
    act = str(data.get("action") or "")
    delivery = header(headers, "Linear-Delivery")
    obj = data.get("data") or {}
    actor_obj = data.get("actor") or {}
    actor = str(actor_obj.get("email") or actor_obj.get("name") or "")
    assoc: AuthorAssociation = _linear_assoc(actor_obj)
    raw_type = f"{kind}.{act}"
    if kind == "Issue":
        ident = str(obj.get("identifier") or "") or None
        team = str((obj.get("team") or {}).get("key") or (ident.split("-")[0] if ident else ""))
        state = obj.get("state") or {}
        label_names = {str(lb.get("id")): str(lb.get("name")) for lb in obj.get("labels") or []}
        common: dict[str, Any] = {"source": "linear", "project": team, "key": ident, "actor": actor, "association": assoc,
                  "raw_type": raw_type, "delivery_id": delivery, "raw": data,
                  "native_state": str(state.get("name") or "") or None}
        if act == "create":
            return TrackerEvent(action="ticket_opened", text=str(obj.get("description") or ""), **common)
        if act == "update":
            prev = data.get("updatedFrom") or {}
            added: list[str] = []
            removed: list[str] = []
            if "labelIds" in prev:
                before = set(prev.get("labelIds") or [])
                after = set(obj.get("labelIds") or label_names.keys())
                added = sorted(label_names.get(i, i) for i in after - before)
                removed = sorted(label_names.get(i, i) for i in before - after)
            if "stateId" in prev:
                added += sm.native_to_labels("linear", common["native_state"])
                closed = state.get("type") in ("completed", "canceled")
                return TrackerEvent(action="ticket_closed" if closed else "state_changed",
                                    labels_added=added, labels_removed=removed, **common)
            if added or removed:
                return TrackerEvent(action="labels_changed", labels_added=added, labels_removed=removed, **common)
            return TrackerEvent(action="ticket_updated", **common)
        return TrackerEvent(action="other", **common)
    if kind == "Comment":
        issue = obj.get("issue") or {}
        ident = str(issue.get("identifier") or "") or None
        user = obj.get("user") or actor_obj
        c_actor = str(user.get("email") or user.get("name") or actor)
        return TrackerEvent(source="linear", action="comment_created" if act == "create" else "other",
                            project=ident.split("-")[0] if ident else "", key=ident or str(obj.get("issueId") or "") or None,
                            actor=c_actor, association=_linear_assoc(obj["user"]) if obj.get("user") else assoc,
                            raw_type=raw_type, delivery_id=delivery, text=str(obj.get("body") or ""), raw=data)
    return _other("linear", raw_type, data, delivery)


def _trello(headers: Mapping[str, str], data: dict[str, Any]) -> TrackerEvent:
    action_obj = data.get("action") or {}
    kind = str(action_obj.get("type") or "")
    d = action_obj.get("data") or {}
    card = d.get("card") or {}
    board = str((d.get("board") or {}).get("id") or (data.get("model") or {}).get("id") or "")
    mc = action_obj.get("memberCreator") or {}
    actor = str(mc.get("username") or action_obj.get("idMemberCreator") or "")
    member: AuthorAssociation = "MEMBER" if _trusted_actor(action_obj.get("idMemberCreator"), mc.get("username"),
                                                             mc.get("id")) else "NONE"
    common: dict[str, Any] = {"source": "trello", "project": board, "key": str(card.get("id") or "") or None, "actor": actor,
              "raw_type": kind, "delivery_id": str(action_obj.get("id") or "") or None, "raw": data}
    if kind == "createCard":
        return TrackerEvent(action="ticket_opened", association=member, text=str(card.get("desc") or "") or None,
                            native_state=str((d.get("list") or {}).get("name") or "") or None, **common)
    if kind == "updateCard":
        old = d.get("old") or {}
        if d.get("listAfter"):
            new_s = str(d["listAfter"].get("name") or "")
            prev_s = str((d.get("listBefore") or {}).get("name") or "")
            old_l = set(sm.native_to_labels("trello", prev_s))
            new_l = set(sm.native_to_labels("trello", new_s))
            return TrackerEvent(action="state_changed", association=member, native_state=new_s,
                                previous_native_state=prev_s, labels_added=sorted(new_l - old_l),
                                labels_removed=sorted(old_l - new_l), **common)
        if "closed" in old:
            return TrackerEvent(action="ticket_closed" if card.get("closed") else "ticket_reopened",
                                association=member, **common)
        return TrackerEvent(action="ticket_updated", association=member, **common)
    if kind in ("addLabelToCard", "removeLabelFromCard"):
        name = str((d.get("label") or {}).get("name") or d.get("text") or "")
        return TrackerEvent(action="labels_changed", association=member,
                            labels_added=[name] if kind == "addLabelToCard" and name else [],
                            labels_removed=[name] if kind == "removeLabelFromCard" and name else [], **common)
    if kind == "commentCard":
        # Public boards may allow non-member comments: membership is checked by the caller.
        return TrackerEvent(action="comment_created", association="NONE", text=str(d.get("text") or ""), **common)
    return TrackerEvent(action="other", association="NONE", **common)


def _gitlab(headers: Mapping[str, str], data: dict[str, Any]) -> TrackerEvent:
    event = header(headers, "X-Gitlab-Event") or ""
    kind = str(data.get("object_kind") or "")
    attrs = data.get("object_attributes") or {}
    act = str(attrs.get("action") or "")
    project = str((data.get("project") or {}).get("path_with_namespace") or "")
    user = data.get("user") or {}
    actor = str(user.get("username") or "")
    assoc: AuthorAssociation = "BOT" if user.get("bot") else "NONE"
    common: dict[str, Any] = {"source": "gitlab", "project": project, "actor": actor, "association": assoc,
              "raw_type": f"{event or kind}.{act}" if act else (event or kind),
              "delivery_id": header(headers, "X-Gitlab-Event-UUID"), "raw": data}
    changes = data.get("changes") or {}
    lab = changes.get("labels") or {}
    before = {str(x.get("title")) for x in lab.get("previous") or []}
    after = {str(x.get("title")) for x in lab.get("current") or []}
    added, removed = sorted(after - before), sorted(before - after)
    if kind == "issue":
        mapping = {"open": "ticket_opened", "close": "ticket_closed", "reopen": "ticket_reopened"}
        action = mapping.get(act) or ("labels_changed" if (added or removed) else "ticket_updated")
        return TrackerEvent(action=action, key=str(attrs.get("iid") or "") or None,
                            text=str(attrs.get("description") or "") if act == "open" else None,
                            labels_added=added, labels_removed=removed,
                            native_state=str(attrs.get("state") or "") or None, **common)
    if kind == "note":
        target = data.get("issue") or data.get("merge_request") or {}
        return TrackerEvent(action="comment_created", key=str(target.get("iid") or "") or None,
                            text=str(attrs.get("note") or ""), **common)
    if kind == "merge_request":
        mapping = {"open": "pr_opened", "reopen": "pr_opened", "close": "pr_closed", "merge": "pr_merged"}
        return TrackerEvent(action=mapping.get(act, "pr_updated"), key=str(attrs.get("iid") or "") or None,
                            labels_added=added, labels_removed=removed, **common)
    return TrackerEvent(action="other", key=None, **common)


_NORMALIZERS = {"github": _github, "jira": _jira, "linear": _linear, "trello": _trello, "gitlab": _gitlab}


def normalize_event(source: str, headers: Mapping[str, str], body: bytes | str | Mapping[str, Any]) -> TrackerEvent:
    """Normalise one webhook delivery. Unknown/malformed input -> ``action="other"``."""
    data = _load(body)
    fn = _NORMALIZERS.get(source.lower())
    if fn is None:
        return _other(source, "", data, None)
    try:
        return fn(headers, data)
    except Exception as e:  # malformed payloads must not crash the receiver
        ev = _other(source.lower(), "malformed", data, None)
        ev.text = f"normalisation failed: {e!r}"
        return ev


__all__ = [
    "SOURCES", "header", "normalize_event", "trello_signature", "verify", "verify_github", "verify_gitlab",
    "verify_jira", "verify_linear", "verify_trello",
]
