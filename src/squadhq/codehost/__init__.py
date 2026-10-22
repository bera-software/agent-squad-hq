"""Code-host adapters (``CodeHost`` protocol in :mod:`squadhq.interfaces`).

Pick one through :func:`get_codehost` — nothing else imports an adapter
module directly::

    host = get_codehost("github")          # GH_TOKEN / GITHUB_TOKEN, GITHUB_API_URL
    host = get_codehost("gitlab")          # GITLAB_TOKEN, GITLAB_URL
"""
from __future__ import annotations

from typing import Any

from squadhq.interfaces import CodeHost

__all__ = ["get_codehost", "available"]

_NAMES = ("github", "gitlab")


def available() -> tuple[str, ...]:
    return _NAMES


def get_codehost(name: str = "github", **kwargs: Any) -> CodeHost:
    """Return a code-host adapter. ``kwargs`` go to its constructor (``token``, ``transport`` ...)."""
    key = name.strip().lower()
    if key == "github":
        from squadhq.codehost.github import GitHubCodeHost

        return GitHubCodeHost(**kwargs)
    if key == "gitlab":
        from squadhq.codehost.gitlab import GitLabCodeHost

        return GitLabCodeHost(**kwargs)
    raise ValueError(f"unknown code host {name!r}; expected one of {', '.join(_NAMES)}")
