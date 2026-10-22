"""Seams between the server and the rest of the package.

Everything the worker does to the outside world goes through one of these
small interfaces, so tests inject fakes and adapters stay swappable:

* :class:`Integrations` — tracker / code-host adapters via the registries
  (``squadhq.trackers.get_tracker``, ``squadhq.codehost.get_codehost``);
* :class:`ModuleRunner` — runs the existing control-plane modules
  (``stuck_sweeper``, ``ticket_transition``, ``hub_janitor``, ``agent_runner``)
  as ``python -m`` subprocesses: they parse ``sys.argv`` and keep module
  globals, so a fresh interpreter per run is the safe contract;
* :class:`WorkflowDispatcher` — BYO-compute: GitHub ``workflow_dispatch``;
* :class:`TokenMinter` — per-job, short-lived credentials for executor containers.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from squadhq.interfaces import CodeHost, FatalError, IssueTracker, TransientError


class Integrations(Protocol):
    def tracker(self, name: str) -> IssueTracker: ...

    def code_host(self, name: str) -> CodeHost: ...


class DefaultIntegrations:
    """Resolve adapters lazily through the subpackage registries; cache per name."""

    def __init__(self, tracker_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
                 codehost_kwargs: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._tk = dict(tracker_kwargs or {})
        self._ck = dict(codehost_kwargs or {})
        self._trackers: dict[str, IssueTracker] = {}
        self._hosts: dict[str, CodeHost] = {}

    def tracker(self, name: str) -> IssueTracker:
        if name not in self._trackers:
            from squadhq.trackers import get_tracker

            self._trackers[name] = get_tracker(name, **dict(self._tk.get(name, {})))
        return self._trackers[name]

    def code_host(self, name: str) -> CodeHost:
        if name not in self._hosts:
            from squadhq.codehost import get_codehost

            self._hosts[name] = get_codehost(name, **dict(self._ck.get(name, {})))
        return self._hosts[name]


@dataclass
class ModuleResult:
    returncode: int
    stdout_tail: str
    stderr_tail: str
    duration_s: float


class ModuleRunner(Protocol):
    def run(self, module: str, args: list[str], *, env: Mapping[str, str], timeout_s: float) -> ModuleResult: ...


ALLOWED_MODULES = frozenset({"stuck_sweeper", "ticket_transition", "hub_janitor", "agent_runner"})


class SubprocessModuleRunner:
    def __init__(self, python: str = sys.executable, cwd: str | None = None) -> None:
        self.python = python
        self.cwd = cwd

    def run(self, module: str, args: list[str], *, env: Mapping[str, str], timeout_s: float) -> ModuleResult:
        if module not in ALLOWED_MODULES:
            raise FatalError(f"module {module!r} is not runnable by the worker")
        full_env = {**os.environ, **env}
        start = time.monotonic()
        try:
            proc = subprocess.run([self.python, "-m", f"squadhq.{module}", *args], env=full_env, cwd=self.cwd,
                                  capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as e:
            raise TransientError(f"squadhq.{module} timed out after {timeout_s:.0f}s") from e
        return ModuleResult(proc.returncode, (proc.stdout or "")[-4000:], (proc.stderr or "")[-4000:],
                            time.monotonic() - start)


class WorkflowDispatcher(Protocol):
    def dispatch(self, repo: str, workflow: str, ref: str, inputs: Mapping[str, str]) -> None: ...


class GitHubWorkflowDispatcher:
    """``POST /repos/{repo}/actions/workflows/{workflow}/dispatches`` (stdlib HTTP, 30 s timeout)."""

    def __init__(self, token: str, api_url: str = "https://api.github.com", transport: Any = None) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.transport = transport

    def dispatch(self, repo: str, workflow: str, ref: str, inputs: Mapping[str, str]) -> None:
        if not self.token:
            raise FatalError("SQUADHQ_GITHUB_TOKEN is not set; cannot dispatch workflows")
        from squadhq.http import request

        request(
            "POST", f"{self.api_url}/repos/{repo}/actions/workflows/{workflow}/dispatches",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json_body={"ref": ref, "inputs": {k: str(v) for k, v in inputs.items()}},
            timeout=30.0, retries=2, transport=self.transport, ok_statuses=(204,),
        )


@dataclass
class ScopedToken:
    env_name: str
    value: str
    expires_at: str = ""


class TokenMinter(Protocol):
    def mint(self, repo: str, role: str) -> list[ScopedToken]: ...


class EnvTokenMinter:
    """Pass through pre-provisioned credentials (development / single-tenant installs).

    Production should mint a GitHub App installation token scoped to ``repo``
    with the role's permissions (see the request to the code-host owner).
    """

    def __init__(self, env: Mapping[str, str] | None = None,
                 names: tuple[str, ...] = ("SQUADHQ_JOB_GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")) -> None:
        self.env = os.environ if env is None else env
        self.names = names

    def mint(self, repo: str, role: str) -> list[ScopedToken]:
        out = []
        for n in self.names:
            v = self.env.get(n)
            if v:
                target = "GH_TOKEN" if n == "SQUADHQ_JOB_GITHUB_TOKEN" else n
                out.append(ScopedToken(env_name=target, value=v))
        return out
