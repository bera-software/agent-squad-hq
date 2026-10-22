"""GitHub implementation of :class:`squadhq.interfaces.CodeHost` (REST v3).

Env: ``GH_TOKEN`` (or ``GITHUB_TOKEN``) for auth, ``GITHUB_API_URL`` for
GitHub Enterprise Server (default ``https://api.github.com``).
"""
from __future__ import annotations

import base64
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

from squadhq.http import Transport, parse_link_next, request
from squadhq.interfaces import CheckRun, FatalError, PullRequest, TransientError

log = logging.getLogger(__name__)

_CHECK_STATUSES = ("queued", "in_progress", "completed")


@dataclass(frozen=True)
class PullHead:
    """The exact commit a PR points at, and where it lives (for merge gating)."""

    sha: str
    repo: str  # full name of the head repository ("" when a fork was deleted)
    fork: bool


@dataclass(frozen=True)
class CheckDetail:
    """A check run plus who produced it (merge gating: a check name alone proves nothing)."""

    name: str
    status: str
    conclusion: str | None
    app_slug: str
    app_id: int | None
    workflow_run_id: str  # Actions run id parsed from details_url ("" for non-Actions checks)


_ACTIONS_RUN = re.compile(r"/actions/runs/(\d+)")


def _q(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


class GitHubCodeHost:
    name = "github"

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str | None = None,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.token = token if token is not None else (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "")
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.api_url}{path}"

    def _call(self, method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None,
              ok_statuses: tuple[int, ...] = ()) -> Any:
        resp = request(method, self._url(path), headers=self._headers(), json_body=json_body, params=params,
                       timeout=self.timeout, transport=self.transport, ok_statuses=ok_statuses)
        return resp

    def _paginate(self, path: str, params: dict[str, Any], limit: int, key: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = self._url(path)
        p: dict[str, Any] | None = {**params, "per_page": min(100, max(1, limit))}
        while url:
            resp = request("GET", url, headers=self._headers(), params=p, timeout=self.timeout,
                           transport=self.transport)
            data = resp.json()
            items = data.get(key, []) if key and isinstance(data, dict) else data
            out.extend(items or [])
            if len(out) >= limit:
                if parse_link_next(resp.header("Link")) or len(out) > limit:
                    log.warning("github: %s hit limit %d; results truncated", path, limit)
                return out[:limit]
            url = parse_link_next(resp.header("Link"))
            p = None  # next link already carries the query
        return out

    # -- mapping ----------------------------------------------------------
    @staticmethod
    def _to_pr(repo: str, d: dict[str, Any], files: list[str] | None = None) -> PullRequest:
        user = d.get("user") or {}
        login = str(user.get("login") or "")
        state: Literal["open", "closed", "merged"]
        if d.get("merged_at") or d.get("merged"):
            state = "merged"
        elif d.get("state") == "closed":
            state = "closed"
        else:
            state = "open"
        return PullRequest(
            number=str(d.get("number", "")),
            repo=repo,
            title=str(d.get("title") or ""),
            body=str(d.get("body") or ""),
            state=state,
            author=login,
            author_is_bot=user.get("type") == "Bot" or login.endswith("[bot]"),
            head_ref=str((d.get("head") or {}).get("ref") or ""),
            base_ref=str((d.get("base") or {}).get("ref") or ""),
            labels=[str(lab.get("name")) for lab in d.get("labels") or [] if isinstance(lab, dict)],
            draft=bool(d.get("draft")),
            url=str(d.get("html_url") or ""),
            changed_files=files or [],
            head_sha=str((d.get("head") or {}).get("sha") or "") or None,
            head_repo=str(((d.get("head") or {}).get("repo") or {}).get("full_name") or "") or None,
        )

    # -- CodeHost ---------------------------------------------------------
    def list_pull_requests(self, repo: str, *, state: Literal["open", "closed", "all"] = "open",
                           limit: int = 200) -> list[PullRequest]:
        items = self._paginate(f"/repos/{repo}/pulls", {"state": state}, limit)
        return [self._to_pr(repo, d) for d in items]

    def get_pull_request(self, repo: str, number: str) -> PullRequest:
        d = self._call("GET", f"/repos/{repo}/pulls/{number}").json()
        files = self._paginate(f"/repos/{repo}/pulls/{number}/files", {}, 3000)
        return self._to_pr(repo, d, [str(f.get("filename")) for f in files])

    def pull_request_head(self, repo: str, number: str) -> PullHead:
        """Head SHA and head repository of a PR (optional extension of ``CodeHost``)."""
        d = self._call("GET", f"/repos/{repo}/pulls/{number}").json()
        head = d.get("head") or {}
        head_repo = str((head.get("repo") or {}).get("full_name") or "")
        return PullHead(sha=str(head.get("sha") or ""), repo=head_repo,
                        fork=head_repo.lower() != repo.lower())

    def list_checks(self, repo: str, ref: str) -> list[CheckRun]:
        runs = self._paginate(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}/check-runs", {}, 500, key="check_runs")
        out: list[CheckRun] = []
        for r in runs:
            status = r.get("status")
            out.append(CheckRun(
                name=str(r.get("name") or ""),
                status=status if status in _CHECK_STATUSES else "queued",
                conclusion=r.get("conclusion"),
            ))
        return out

    def check_runs_detail(self, repo: str, sha: str) -> list[CheckDetail]:
        """Check runs on a commit with their producing app (optional ``CodeHost`` extension)."""
        runs = self._paginate(f"/repos/{repo}/commits/{urllib.parse.quote(sha, safe='')}/check-runs", {}, 500,
                              key="check_runs")
        out: list[CheckDetail] = []
        for r in runs:
            app = r.get("app") or {}
            m = _ACTIONS_RUN.search(str(r.get("details_url") or r.get("html_url") or ""))
            out.append(CheckDetail(name=str(r.get("name") or ""), status=str(r.get("status") or "queued"),
                                   conclusion=r.get("conclusion"), app_slug=str(app.get("slug") or ""),
                                   app_id=app.get("id") if isinstance(app.get("id"), int) else None,
                                   workflow_run_id=m.group(1) if m else ""))
        return out

    def workflow_run_path(self, repo: str, run_id: str) -> str:
        """Workflow file of an Actions run, e.g. ``.github/workflows/agents.yml``."""
        d = self._call("GET", f"/repos/{repo}/actions/runs/{urllib.parse.quote(run_id, safe='')}").json()
        return str((d or {}).get("path") or "")

    def read_file(self, repo: str, path: str, ref: str | None = None) -> tuple[str, str]:
        params = {"ref": ref} if ref else None
        d = self._call("GET", f"/repos/{repo}/contents/{_q(path)}", params=params).json()
        if not isinstance(d, dict) or d.get("type", "file") != "file":
            raise FatalError(f"{repo}:{path} is not a file")
        content = base64.b64decode(d.get("content") or "").decode("utf-8")
        return content, str(d.get("sha") or "")

    def write_file(self, repo: str, path: str, content: str, message: str,
                   expected_version: str | None) -> str:
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if expected_version:
            body["sha"] = expected_version
        resp = self._call("PUT", f"/repos/{repo}/contents/{_q(path)}", json_body=body, ok_statuses=(409, 422))
        if resp.status in (409, 422):
            text = resp.text
            if resp.status == 409 or "sha" in text.lower():
                raise TransientError(f"version conflict writing {repo}:{path}: {text[:300]}", status=resp.status,
                                     body=text[:2000])
            raise FatalError(f"write {repo}:{path} rejected: {text[:300]}", status=resp.status, body=text[:2000])
        d = resp.json() or {}
        return str((d.get("content") or {}).get("sha") or "")

    def merge(self, repo: str, number: str, method: Literal["squash", "merge", "rebase"] = "squash",
              sha: str | None = None) -> None:
        """Merge the PR. With ``sha``, GitHub refuses (409) if the head moved since it was checked."""
        body: dict[str, Any] = {"merge_method": method}
        if sha:
            body["sha"] = sha
        resp = self._call("PUT", f"/repos/{repo}/pulls/{number}/merge", json_body=body,
                          ok_statuses=(405, 409))
        if resp.status == 409:
            # Head moved while merging: retrying after a refresh may succeed.
            raise TransientError(f"merge conflict on {repo}#{number}: {resp.text[:300]}", status=409)
        if resp.status == 405:
            raise FatalError(f"{repo}#{number} is not mergeable: {resp.text[:300]}", status=405)
