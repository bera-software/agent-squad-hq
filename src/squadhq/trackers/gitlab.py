"""GitLab Issues tracker (REST v4).

Env: ``GITLAB_TOKEN`` (personal/project/group access token, ``PRIVATE-TOKEN``
header) and optional ``GITLAB_URL`` (default ``https://gitlab.com``).
``project`` is the full path (``group/sub/name``) or numeric id; ``key`` is
the issue IID. Labels are native, so flow labels stay labels.

Association comes from the author's project membership (cached per user):
access level 50 -> ``OWNER``, 30/40 -> ``MEMBER``, 10/20 -> ``CONTRIBUTOR``,
not a member -> ``NONE``; bot users -> ``BOT``.
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from typing import Any, Literal

from squadhq import http
from squadhq.interfaces import AuthorAssociation, Comment, Ticket
from squadhq.trackers._common import env, require, warn

PER_PAGE = 100


def access_to_association(level: int | None) -> AuthorAssociation:
    if level is None:
        return "NONE"
    if level >= 50:
        return "OWNER"
    if level >= 30:
        return "MEMBER"
    if level >= 10:
        return "CONTRIBUTOR"
    return "NONE"


class GitLabTracker:
    name = "gitlab"

    def __init__(self, token: str | None = None, *, base_url: str | None = None,
                 transport: http.Transport | None = None, timeout: float = 30.0,
                 resolve_association: bool = True) -> None:
        self.token = token or env("GITLAB_TOKEN")
        self.base_url = (base_url or env("GITLAB_URL") or "https://gitlab.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self.resolve_association = resolve_association
        self._assoc: dict[tuple[str, int], AuthorAssociation] = {}

    def _req(self, method: str, path: str, **kw: Any) -> http.HTTPResponse:
        tok = require(self.token, "GitLab token", "GITLAB_TOKEN")
        url = path if path.startswith("http") else f"{self.base_url}/api/v4/{path.lstrip('/')}"
        return http.request(method, url, headers={"PRIVATE-TOKEN": tok}, timeout=self.timeout,
                            transport=self.transport, **kw)

    @staticmethod
    def _p(project: str) -> str:
        return "projects/" + urllib.parse.quote(project, safe="")

    def _paginate(self, path: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = path
        first = True
        while url and len(out) < limit:
            resp = self._req("GET", url, params=params if first else None)
            first = False
            out.extend(resp.json() or [])
            url = http.parse_link_next(resp.header("Link"))
        if url:
            warn(f"GitLab GET {path}: stopped at limit={limit}; more results exist")
        return out[:limit]

    def association_for(self, project: str, user: dict[str, Any] | None) -> AuthorAssociation:
        if not user:
            return "NONE"
        if user.get("bot") or str(user.get("username", "")).endswith("-bot"):
            return "BOT"
        if not self.resolve_association or user.get("id") is None:
            return "NONE"
        cache_key = (project, int(user["id"]))
        if cache_key not in self._assoc:
            resp = self._req("GET", f"{self._p(project)}/members/all/{int(user['id'])}", ok_statuses=(404,))
            level = (resp.json() or {}).get("access_level") if resp.status == 200 else None
            self._assoc[cache_key] = access_to_association(level)
        return self._assoc[cache_key]

    def _ticket(self, project: str, it: dict[str, Any]) -> Ticket:
        author = it.get("author") or {}
        return Ticket(
            key=str(it.get("iid", "")),
            project=project,
            title=str(it.get("title") or ""),
            body=str(it.get("description") or ""),
            state="closed" if it.get("state") == "closed" else "open",
            labels=[str(x) for x in it.get("labels") or []],
            author=str(author.get("username", "")),
            association=self.association_for(project, author),
            url=str(it.get("web_url") or ""),
            created_at=str(it.get("created_at") or ""),
            updated_at=str(it.get("updated_at") or ""),
            native_state=str(it.get("state") or "") or None,
            assignees=[str(a.get("username", "")) for a in it.get("assignees") or []],
        )

    def list_tickets(self, project: str, *, state: Literal["open", "closed", "all"] = "open",
                     labels: Iterable[str] = (), limit: int = 500) -> list[Ticket]:
        params: dict[str, Any] = {"per_page": PER_PAGE,
                                  "state": {"open": "opened", "closed": "closed"}.get(state)}
        lbs = list(labels)
        if lbs:
            params["labels"] = ",".join(lbs)
        return [self._ticket(project, it) for it in self._paginate(f"{self._p(project)}/issues", params, limit)]

    def get_ticket(self, project: str, key: str) -> Ticket:
        return self._ticket(project, self._req("GET", f"{self._p(project)}/issues/{key}").json())

    def list_comments(self, project: str, key: str) -> list[Comment]:
        notes = self._paginate(f"{self._p(project)}/issues/{key}/notes",
                               {"per_page": PER_PAGE, "sort": "asc", "order_by": "created_at"}, 2000)
        out: list[Comment] = []
        for n in notes:
            if n.get("system"):
                continue  # label/state change notes are not human comments
            author = n.get("author") or {}
            out.append(Comment(id=str(n.get("id")), author=str(author.get("username", "")),
                               association=self.association_for(project, author),
                               body=str(n.get("body") or ""), created_at=str(n.get("created_at") or "")))
        return out

    def create_ticket(self, project: str, title: str, body: str, labels: Iterable[str] = ()) -> Ticket:
        payload: dict[str, Any] = {"title": title, "description": body}
        lbs = list(labels)
        if lbs:
            payload["labels"] = ",".join(lbs)
        return self._ticket(project, self._req("POST", f"{self._p(project)}/issues", json_body=payload).json())

    def comment(self, project: str, key: str, body: str) -> Comment:
        n = self._req("POST", f"{self._p(project)}/issues/{key}/notes", json_body={"body": body}).json() or {}
        author = n.get("author") or {}
        return Comment(id=str(n.get("id", "")), author=str(author.get("username", "")), association="BOT",
                       body=str(n.get("body") or body), created_at=str(n.get("created_at") or ""))

    def add_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        lbs = list(labels)
        if lbs:
            self._req("PUT", f"{self._p(project)}/issues/{key}", json_body={"add_labels": ",".join(lbs)})

    def remove_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        lbs = list(labels)
        if lbs:
            self._req("PUT", f"{self._p(project)}/issues/{key}", json_body={"remove_labels": ",".join(lbs)})

    def close_ticket(self, project: str, key: str,
                     reason: Literal["completed", "not_planned"] = "completed") -> None:
        # GitLab has no close reason; not_planned is recorded as a label for the flow engine.
        payload: dict[str, Any] = {"state_event": "close"}
        if reason == "not_planned":
            payload["add_labels"] = "not-planned"
        self._req("PUT", f"{self._p(project)}/issues/{key}", json_body=payload)
