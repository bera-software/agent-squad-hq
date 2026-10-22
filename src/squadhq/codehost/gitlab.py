"""Minimal GitLab implementation of :class:`squadhq.interfaces.CodeHost` (REST v4).

Env: ``GITLAB_TOKEN`` (sent as ``PRIVATE-TOKEN``), ``GITLAB_URL`` (default
``https://gitlab.com``). ``repo`` is the project path (``group/name``) or a
numeric project id. Merge requests map to :class:`PullRequest` (``number`` is
the MR ``iid``); commit statuses map to :class:`CheckRun`. File versions are
the blob's ``last_commit_id`` (GitLab's CAS token for file updates).

Best effort: verified against GitLab's documented API shapes only.
"""
from __future__ import annotations

import base64
import logging
import os
import urllib.parse
from typing import Any, Literal

from squadhq.codehost.github import PullHead
from squadhq.http import Transport, parse_link_next, request
from squadhq.interfaces import CheckRun, FatalError, PullRequest, TransientError

log = logging.getLogger(__name__)

_STATUS_MAP: dict[str, tuple[Literal["queued", "in_progress", "completed"], str | None]] = {
    "created": ("queued", None),
    "pending": ("queued", None),
    "waiting_for_resource": ("queued", None),
    "preparing": ("queued", None),
    "scheduled": ("queued", None),
    "manual": ("queued", None),
    "running": ("in_progress", None),
    "success": ("completed", "success"),
    "failed": ("completed", "failure"),
    "canceled": ("completed", "cancelled"),
    "skipped": ("completed", "skipped"),
}


def _pid(repo: str) -> str:
    return urllib.parse.quote(repo, safe="")


class GitLabCodeHost:
    name = "gitlab"

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.token = token if token is not None else os.environ.get("GITLAB_TOKEN", "")
        self.base_url = (base_url or os.environ.get("GITLAB_URL") or "https://gitlab.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def _api(self, path: str) -> str:
        return f"{self.base_url}/api/v4{path}"

    def _paginate(self, path: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = self._api(path)
        p: dict[str, Any] | None = {**params, "per_page": min(100, max(1, limit))}
        while url:
            resp = request("GET", url, headers=self._headers(), params=p, timeout=self.timeout,
                           transport=self.transport)
            out.extend(resp.json() or [])
            nxt = parse_link_next(resp.header("Link"))
            if len(out) >= limit:
                if nxt or len(out) > limit:
                    log.warning("gitlab: %s hit limit %d; results truncated", path, limit)
                return out[:limit]
            url, p = nxt, None
        return out

    @staticmethod
    def _to_pr(repo: str, d: dict[str, Any], files: list[str] | None = None) -> PullRequest:
        author = d.get("author") or {}
        login = str(author.get("username") or "")
        raw = d.get("state")
        state: Literal["open", "closed", "merged"] = (
            "merged" if raw == "merged" else "closed" if raw in ("closed", "locked") else "open")
        return PullRequest(
            number=str(d.get("iid", "")),
            repo=repo,
            title=str(d.get("title") or ""),
            body=str(d.get("description") or ""),
            state=state,
            author=login,
            author_is_bot=bool(author.get("bot")) or login.endswith("[bot]") or login.startswith("project_"),
            head_ref=str(d.get("source_branch") or ""),
            base_ref=str(d.get("target_branch") or ""),
            labels=[str(x) for x in d.get("labels") or []],
            draft=bool(d.get("draft") or d.get("work_in_progress")),
            url=str(d.get("web_url") or ""),
            changed_files=files or [],
            head_sha=str(d.get("sha") or "") or None,
            head_repo=(repo if d.get("source_project_id") == d.get("target_project_id")
                       else str(d.get("source_project_id") or "") or None),
        )

    def list_pull_requests(self, repo: str, *, state: Literal["open", "closed", "all"] = "open",
                           limit: int = 200) -> list[PullRequest]:
        gl_state = {"open": "opened", "closed": "closed", "all": "all"}[state]
        items = self._paginate(f"/projects/{_pid(repo)}/merge_requests", {"state": gl_state}, limit)
        return [self._to_pr(repo, d) for d in items]

    def get_pull_request(self, repo: str, number: str) -> PullRequest:
        d = request("GET", self._api(f"/projects/{_pid(repo)}/merge_requests/{number}/changes"),
                    headers=self._headers(), timeout=self.timeout, transport=self.transport).json()
        files = [str(c.get("new_path") or c.get("old_path")) for c in d.get("changes") or []]
        return self._to_pr(repo, d, files)

    def pull_request_head(self, repo: str, number: str) -> PullHead:
        """Head SHA of an MR; ``fork`` when the source project differs from the target."""
        d = request("GET", self._api(f"/projects/{_pid(repo)}/merge_requests/{number}"),
                    headers=self._headers(), timeout=self.timeout, transport=self.transport).json()
        fork = d.get("source_project_id") != d.get("target_project_id")
        return PullHead(sha=str(d.get("sha") or ""), repo="" if fork else repo, fork=bool(fork))

    def list_checks(self, repo: str, ref: str) -> list[CheckRun]:
        items = self._paginate(f"/projects/{_pid(repo)}/repository/commits/{urllib.parse.quote(ref, safe='')}/statuses",
                               {}, 500)
        out: list[CheckRun] = []
        for s in items:
            status, conclusion = _STATUS_MAP.get(str(s.get("status")), ("completed", "neutral"))
            out.append(CheckRun(name=str(s.get("name") or ""), status=status, conclusion=conclusion))
        return out

    def read_file(self, repo: str, path: str, ref: str | None = None) -> tuple[str, str]:
        d = request("GET", self._api(f"/projects/{_pid(repo)}/repository/files/{urllib.parse.quote(path, safe='')}"),
                    headers=self._headers(), params={"ref": ref or "HEAD"}, timeout=self.timeout,
                    transport=self.transport).json()
        content = base64.b64decode(d.get("content") or "").decode("utf-8")
        return content, str(d.get("last_commit_id") or "")

    def write_file(self, repo: str, path: str, content: str, message: str,
                   expected_version: str | None, branch: str = "main") -> str:
        url = self._api(f"/projects/{_pid(repo)}/repository/files/{urllib.parse.quote(path, safe='')}")
        body: dict[str, Any] = {"branch": branch, "content": content, "commit_message": message}
        if expected_version:
            body["last_commit_id"] = expected_version
        method = "PUT" if expected_version else "POST"
        resp = request(method, url, headers=self._headers(), json_body=body, timeout=self.timeout,
                       transport=self.transport, ok_statuses=(400, 409))
        if resp.status in (400, 409):
            text = resp.text
            if resp.status == 409 or "last_commit_id" in text or "changed" in text.lower():
                raise TransientError(f"version conflict writing {repo}:{path}: {text[:300]}", status=resp.status)
            raise FatalError(f"write {repo}:{path} rejected: {text[:300]}", status=resp.status)
        # The API returns {file_path, branch}; re-read for the new version token.
        return self.read_file(repo, path, branch)[1]

    def merge(self, repo: str, number: str, method: Literal["squash", "merge", "rebase"] = "squash",
              sha: str | None = None) -> None:
        """Merge the MR. With ``sha``, GitLab refuses (409) if the source branch head moved."""
        if method == "rebase":
            request("PUT", self._api(f"/projects/{_pid(repo)}/merge_requests/{number}/rebase"),
                    headers=self._headers(), timeout=self.timeout, transport=self.transport)
        resp = request("PUT", self._api(f"/projects/{_pid(repo)}/merge_requests/{number}/merge"),
                       headers=self._headers(),
                       json_body={"squash": method == "squash", **({"sha": sha} if sha else {})}, timeout=self.timeout,
                       transport=self.transport, ok_statuses=(405, 406, 409))
        if resp.status in (406, 409):
            raise TransientError(f"merge conflict on {repo}!{number}: {resp.text[:300]}", status=resp.status)
        if resp.status == 405:
            raise FatalError(f"{repo}!{number} is not mergeable: {resp.text[:300]}", status=405)
