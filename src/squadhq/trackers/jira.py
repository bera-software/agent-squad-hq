"""Jira Cloud tracker (REST v3, basic auth with an API token).

Env: ``JIRA_BASE_URL`` (``https://<site>.atlassian.net``), ``JIRA_EMAIL``,
``JIRA_API_TOKEN``; optional ``JIRA_ISSUE_TYPE`` (default ``Task``).
``project`` is the Jira project key, ``key`` the issue key (``ENG-42``).

Flow labels that map to a native status (see :mod:`squadhq.trackers.status_maps`)
are applied as workflow transitions; all other labels are Jira labels.
Descriptions and comments are converted Markdown <-> ADF.

Author association is best effort: ``accountType == "atlassian"`` (a licensed
site user) is ``MEMBER``, ``app`` is ``BOT``, anything else (service-desk
customers) is ``NONE``. Configure approvers explicitly for trust decisions.
"""
from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from squadhq import http
from squadhq.interfaces import AuthorAssociation, Comment, FatalError, Ticket
from squadhq.trackers import status_maps as sm
from squadhq.trackers._common import dedupe, env, require, warn
from squadhq.trackers.adf import adf_to_markdown, markdown_to_adf

FIELDS = ["summary", "description", "status", "labels", "reporter", "assignee", "created", "updated", "project"]
PAGE = 100


def _user(u: Mapping[str, Any] | None) -> tuple[str, AuthorAssociation]:
    if not u:
        return "", "NONE"
    name = str(u.get("emailAddress") or u.get("accountId") or u.get("displayName") or "")
    kind = u.get("accountType")
    if kind == "app":
        return name, "BOT"
    if kind == "atlassian":
        return name, "MEMBER"
    return name, "NONE"


class JiraTracker:
    name = "jira"

    def __init__(self, base_url: str | None = None, email: str | None = None, api_token: str | None = None, *,
                 issue_type: str | None = None, status_overrides: Mapping[str, Any] | None = None,
                 transport: http.Transport | None = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or env("JIRA_BASE_URL") or "").rstrip("/")
        self.email = email or env("JIRA_EMAIL")
        self.api_token = api_token or env("JIRA_API_TOKEN")
        self.issue_type = issue_type or env("JIRA_ISSUE_TYPE") or "Task"
        self.overrides = status_overrides
        self.transport = transport
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        email = require(self.email, "Jira email", "JIRA_EMAIL")
        tok = require(self.api_token, "Jira API token", "JIRA_API_TOKEN")
        cred = base64.b64encode(f"{email}:{tok}".encode()).decode()
        return {"Authorization": f"Basic {cred}", "Accept": "application/json"}

    def _req(self, method: str, path: str, **kw: Any) -> http.HTTPResponse:
        base = require(self.base_url, "Jira base URL", "JIRA_BASE_URL")
        return http.request(method, f"{base}/rest/api/3/{path.lstrip('/')}", headers=self._headers(),
                            timeout=self.timeout, transport=self.transport, **kw)

    def _ticket(self, it: Mapping[str, Any]) -> Ticket:
        f = it.get("fields") or {}
        status = f.get("status") or {}
        native = str(status.get("name") or "") or None
        closed = (status.get("statusCategory") or {}).get("key") == "done"
        author, assoc = _user(f.get("reporter"))
        labels = dedupe([str(x) for x in f.get("labels") or []] + sm.native_to_labels("jira", native, self.overrides))
        assignee = f.get("assignee")
        key = str(it.get("key", ""))
        return Ticket(
            key=key,
            project=str((f.get("project") or {}).get("key") or key.split("-")[0]),
            title=str(f.get("summary") or ""),
            body=adf_to_markdown(f.get("description")),
            state="closed" if closed else "open",
            labels=labels,
            author=author,
            association=assoc,
            url=f"{self.base_url}/browse/{key}",
            created_at=str(f.get("created") or ""),
            updated_at=str(f.get("updated") or ""),
            native_state=native,
            assignees=[_user(assignee)[0]] if assignee else [],
        )

    @staticmethod
    def _jql_quote(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def build_jql(self, project: str, state: str, labels: Iterable[str]) -> str:
        parts = [f"project = {self._jql_quote(project)}"]
        if state == "open":
            parts.append("statusCategory != Done")
        elif state == "closed":
            parts.append("statusCategory = Done")
        state_labels, plain = sm.split_labels("jira", labels, self.overrides)
        for lb in plain:
            parts.append(f"labels = {self._jql_quote(lb)}")
        for lb in state_labels:
            names = ", ".join(self._jql_quote(s) for s in sm.candidate_states("jira", lb, self.overrides))
            parts.append(f"status in ({names})")
        return " AND ".join(parts) + " ORDER BY created ASC"

    def search(self, jql: str, limit: int = 500) -> list[dict[str, Any]]:
        """JQL search: ``/search/jql`` with ``nextPageToken``; falls back to legacy ``/search`` + ``startAt``."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        resp = None
        while len(out) < limit:
            body: dict[str, Any] = {"jql": jql, "maxResults": min(PAGE, limit - len(out)), "fields": FIELDS}
            if token:
                body["nextPageToken"] = token
            resp = self._req("POST", "search/jql", json_body=body, ok_statuses=(404, 410))
            if resp.status in (404, 410):
                return self._search_legacy(jql, limit)
            data = resp.json() or {}
            out.extend(data.get("issues") or [])
            token = data.get("nextPageToken")
            if not token or data.get("isLast"):
                token = None
                break
        if token:
            warn(f"Jira search stopped at limit={limit}; more results exist")
        return out[:limit]

    def _search_legacy(self, jql: str, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        total = None
        while len(out) < limit:
            data = self._req("GET", "search", params={"jql": jql, "startAt": start,
                                                      "maxResults": min(PAGE, limit - len(out)),
                                                      "fields": ",".join(FIELDS)}).json() or {}
            issues = data.get("issues") or []
            out.extend(issues)
            total = int(data.get("total", 0))
            start += len(issues)
            if not issues or start >= total:
                break
        if total is not None and total > len(out):
            warn(f"Jira search stopped at limit={limit}; {total} results exist")
        return out[:limit]

    # -- IssueTracker ------------------------------------------------------
    def list_tickets(self, project: str, *, state: Literal["open", "closed", "all"] = "open",
                     labels: Iterable[str] = (), limit: int = 500) -> list[Ticket]:
        return [self._ticket(it) for it in self.search(self.build_jql(project, state, labels), limit)]

    def get_ticket(self, project: str, key: str) -> Ticket:
        return self._ticket(self._req("GET", f"issue/{key}", params={"fields": ",".join(FIELDS)}).json())

    def list_comments(self, project: str, key: str) -> list[Comment]:
        out: list[Comment] = []
        start = 0
        while True:
            data = self._req("GET", f"issue/{key}/comment",
                             params={"startAt": start, "maxResults": PAGE, "orderBy": "created"}).json() or {}
            items = data.get("comments") or []
            for c in items:
                author, assoc = _user(c.get("author"))
                out.append(Comment(id=str(c.get("id", "")), author=author, association=assoc,
                                   body=adf_to_markdown(c.get("body")), created_at=str(c.get("created") or "")))
            start += len(items)
            if not items or start >= int(data.get("total", 0)):
                return out

    def create_ticket(self, project: str, title: str, body: str, labels: Iterable[str] = ()) -> Ticket:
        state_labels, plain = sm.split_labels("jira", labels, self.overrides)
        fields: dict[str, Any] = {"project": {"key": project}, "summary": title,
                                  "description": markdown_to_adf(body), "issuetype": {"name": self.issue_type}}
        if plain:
            fields["labels"] = [lb.replace(" ", "-") for lb in plain]
        created = self._req("POST", "issue", json_body={"fields": fields}).json() or {}
        key = str(created.get("key", ""))
        if state_labels:
            self._transition_to_label(key, state_labels[-1])
        return self.get_ticket(project, key)

    def comment(self, project: str, key: str, body: str) -> Comment:
        c = self._req("POST", f"issue/{key}/comment", json_body={"body": markdown_to_adf(body)}).json() or {}
        author, assoc = _user(c.get("author"))
        return Comment(id=str(c.get("id", "")), author=author, association=assoc, body=body,
                       created_at=str(c.get("created") or ""))

    def _update_labels(self, key: str, op: str, labels: list[str]) -> None:
        if labels:
            self._req("PUT", f"issue/{key}",
                      json_body={"update": {"labels": [{op: lb.replace(" ", "-")} for lb in labels]}})

    def add_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        state_labels, plain = sm.split_labels("jira", labels, self.overrides)
        self._update_labels(key, "add", plain)
        if state_labels:
            self._transition_to_label(key, state_labels[-1])

    def remove_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        state_labels, plain = sm.split_labels("jira", labels, self.overrides)
        self._update_labels(key, "remove", plain)
        if state_labels:
            current = self.get_ticket(project, key)
            implied = sm.native_to_labels("jira", current.native_state, self.overrides)
            if any(lb in implied for lb in state_labels):
                target = sm.special_state("jira", "open", self.overrides)
                if target:
                    self.transition(key, [target])

    def close_ticket(self, project: str, key: str,
                     reason: Literal["completed", "not_planned"] = "completed") -> None:
        names = [s for s in (sm.special_state("jira", "done" if reason == "completed" else "not_planned",
                                              self.overrides), sm.special_state("jira", "done", self.overrides))
                 if s]
        self.transition(key, names, done_fallback=True)

    # -- transitions -------------------------------------------------------
    def _transition_to_label(self, key: str, label: str) -> None:
        self.transition(key, sm.candidate_states("jira", label, self.overrides))

    def transition(self, key: str, target_states: list[str], *, done_fallback: bool = False) -> str:
        """Move ``key`` to the first reachable state in ``target_states``. Returns the transition id."""
        data = self._req("GET", f"issue/{key}/transitions").json() or {}
        transitions = data.get("transitions") or []
        chosen: dict[str, Any] | None = None
        for target in target_states:
            low = target.lower()
            chosen = next((t for t in transitions
                           if str((t.get("to") or {}).get("name", "")).lower() == low
                           or str(t.get("name", "")).lower() == low), None)
            if chosen:
                break
        if chosen is None and done_fallback:
            chosen = next((t for t in transitions
                           if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done"), None)
        if chosen is None:
            avail = [str((t.get("to") or {}).get("name", t.get("name"))) for t in transitions]
            raise FatalError(f"Jira {key}: no transition to any of {target_states}; available: {avail}")
        self._req("POST", f"issue/{key}/transitions", json_body={"transition": {"id": str(chosen["id"])}})
        return str(chosen["id"])
