"""Notify by opening (or commenting on) a GitHub issue in the hub repo.

Env: ``GH_TOKEN`` (or ``GITHUB_TOKEN``), ``SQUADHQ_NOTIFY_REPO`` (default:
``settings.hub_repo()``), ``SQUADHQ_NOTIFY_LABEL`` (default
``notification``), ``GITHUB_API_URL``.

Dedupe: a notification with ``dedupe_key`` gets ``[<dedupe_key>]`` appended
to its issue title. If an *open* issue carrying the label already has that
marker in its title, a comment is added instead of a new issue.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from squadhq import settings
from squadhq.http import Transport, parse_link_next, request
from squadhq.interfaces import Notification
from squadhq.notify._common import SEVERITY_EMOJI

log = logging.getLogger(__name__)

_SCAN_LIMIT = 500


class GitHubIssueNotifier:
    name = "github_issue"

    def __init__(
        self,
        repo: str | None = None,
        *,
        token: str | None = None,
        label: str | None = None,
        api_url: str | None = None,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.repo = repo or os.environ.get("SQUADHQ_NOTIFY_REPO") or settings.hub_repo()
        self.token = token if token is not None else (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "")
        self.label = label or os.environ.get("SQUADHQ_NOTIFY_LABEL") or "notification"
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, url: str, **kw: Any) -> Any:
        return request(method, url, headers=self._headers(), timeout=self.timeout, transport=self.transport, **kw)

    @staticmethod
    def title_for(n: Notification) -> str:
        title = n.title.strip()
        if n.dedupe_key and f"[{n.dedupe_key}]" not in title:
            title = f"{title} [{n.dedupe_key}]"
        return title

    @staticmethod
    def body_for(n: Notification) -> str:
        meta = [f"{SEVERITY_EMOJI.get(n.severity, '')} **{n.severity}** · {n.kind}"]
        if n.project:
            meta.append(f"Project: `{n.project}`")
        if n.url:
            meta.append(f"Link: {n.url}")
        return "\n".join(meta) + "\n\n" + n.body_markdown

    def find_open(self, marker: str) -> int | None:
        url: str | None = f"{self.api_url}/repos/{self.repo}/issues"
        params: dict[str, Any] | None = {"state": "open", "labels": self.label, "per_page": 100}
        seen = 0
        while url:
            resp = self._req("GET", url, params=params)
            for it in resp.json() or []:
                seen += 1
                if "pull_request" not in it and marker in str(it.get("title", "")):
                    return int(it["number"])
            url = parse_link_next(resp.header("Link"))
            params = None
            if seen >= _SCAN_LIMIT and url:
                log.warning("github_issue: scanned %d open issues without finding %s; creating new", seen, marker)
                return None
        return None

    def send(self, notification: Notification) -> None:
        body = self.body_for(notification)
        if notification.dedupe_key:
            existing = self.find_open(f"[{notification.dedupe_key}]")
            if existing is not None:
                self._req("POST", f"{self.api_url}/repos/{self.repo}/issues/{existing}/comments",
                          json_body={"body": body})
                return
        self._req("POST", f"{self.api_url}/repos/{self.repo}/issues",
                  json_body={"title": self.title_for(notification), "body": body, "labels": [self.label]})
