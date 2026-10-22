"""GitHub Issues tracker (REST v3 over urllib).

Env: ``GH_TOKEN`` (or ``GITHUB_TOKEN``) and optional ``GITHUB_API_URL``
(default ``https://api.github.com``; set it for GitHub Enterprise Server).
``project`` is ``owner/name``; ``key`` is the issue number as a string.
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from typing import Any, Literal

from squadhq import http
from squadhq.interfaces import Comment, Ticket
from squadhq.trackers._common import association, env, require, warn

PER_PAGE = 100


def ticket_from_issue(repo: str, it: dict[str, Any]) -> Ticket:
    user = it.get("user") or {}
    login = str(user.get("login", ""))
    assoc = "BOT" if user.get("type") == "Bot" else association(it.get("author_association"))
    return Ticket(
        key=str(it.get("number", "")),
        project=repo,
        title=str(it.get("title") or ""),
        body=str(it.get("body") or ""),
        state="closed" if it.get("state") == "closed" else "open",
        labels=[str(lb["name"] if isinstance(lb, dict) else lb) for lb in it.get("labels") or []],
        author=login,
        association=assoc,
        url=str(it.get("html_url") or ""),
        created_at=str(it.get("created_at") or ""),
        updated_at=str(it.get("updated_at") or ""),
        native_state=str(it.get("state_reason") or it.get("state") or "") or None,
        assignees=[str(a.get("login", "")) for a in it.get("assignees") or []],
    )


def comment_from_json(c: dict[str, Any]) -> Comment:
    user = c.get("user") or {}
    assoc = "BOT" if user.get("type") == "Bot" else association(c.get("author_association"))
    return Comment(id=str(c.get("id", "")), author=str(user.get("login", "")), association=assoc,
                   body=str(c.get("body") or ""), created_at=str(c.get("created_at") or ""))


class GitHubTracker:
    name = "github"

    def __init__(self, token: str | None = None, *, api_url: str | None = None,
                 transport: http.Transport | None = None, timeout: float = 30.0) -> None:
        self.token = token or env("GH_TOKEN", "GITHUB_TOKEN")
        self.api_url = (api_url or env("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        tok = require(self.token, "GitHub token", "GH_TOKEN or GITHUB_TOKEN")
        return {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.api_url}/{path.lstrip('/')}"

    def _req(self, method: str, path: str, **kw: Any) -> http.HTTPResponse:
        return http.request(method, self._url(path), headers=self._headers(), timeout=self.timeout,
                            transport=self.transport, **kw)

    def _paginate(self, path: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = self._url(path)
        first = True
        while url and len(out) < limit:
            resp = self._req("GET", url, params=params if first else None)
            first = False
            page = resp.json() or []
            out.extend(page)
            url = http.parse_link_next(resp.header("Link"))
        if url and len(out) >= limit:
            warn(f"GET {path}: stopped at limit={limit}; more results exist")
        return out[:limit]

    @staticmethod
    def _q(repo: str) -> str:
        owner, _, name = repo.partition("/")
        return f"repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"

    # -- IssueTracker ------------------------------------------------------
    def list_tickets(self, project: str, *, state: Literal["open", "closed", "all"] = "open",
                     labels: Iterable[str] = (), limit: int = 500) -> list[Ticket]:
        params: dict[str, Any] = {"state": state, "per_page": PER_PAGE}
        lbs = list(labels)
        if lbs:
            params["labels"] = ",".join(lbs)
        items = self._paginate(f"{self._q(project)}/issues", params, limit * 2)
        tickets = [ticket_from_issue(project, it) for it in items if "pull_request" not in it]
        return tickets[:limit]

    def get_ticket(self, project: str, key: str) -> Ticket:
        return ticket_from_issue(project, self._req("GET", f"{self._q(project)}/issues/{key}").json())

    def list_comments(self, project: str, key: str) -> list[Comment]:
        items = self._paginate(f"{self._q(project)}/issues/{key}/comments", {"per_page": PER_PAGE}, 1000)
        return [comment_from_json(c) for c in items]

    def create_ticket(self, project: str, title: str, body: str, labels: Iterable[str] = ()) -> Ticket:
        payload: dict[str, Any] = {"title": title, "body": body}
        lbs = list(labels)
        if lbs:
            payload["labels"] = lbs
        return ticket_from_issue(project, self._req("POST", f"{self._q(project)}/issues", json_body=payload).json())

    def comment(self, project: str, key: str, body: str) -> Comment:
        return comment_from_json(
            self._req("POST", f"{self._q(project)}/issues/{key}/comments", json_body={"body": body}).json())

    def add_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        lbs = list(labels)
        if lbs:
            self._req("POST", f"{self._q(project)}/issues/{key}/labels", json_body={"labels": lbs})

    def remove_labels(self, project: str, key: str, labels: Iterable[str]) -> None:
        for lb in labels:
            # 404 = label not on the issue: already in the desired state.
            self._req("DELETE", f"{self._q(project)}/issues/{key}/labels/{urllib.parse.quote(lb, safe='')}",
                      ok_statuses=(404,))

    def close_ticket(self, project: str, key: str,
                     reason: Literal["completed", "not_planned"] = "completed") -> None:
        self._req("PATCH", f"{self._q(project)}/issues/{key}",
                  json_body={"state": "closed", "state_reason": reason})
