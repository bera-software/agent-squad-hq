"""Issue-tracker adapters behind :class:`squadhq.interfaces.IssueTracker`.

Pick one with :func:`get_tracker` (``github``, ``jira``, ``linear``,
``trello``, ``gitlab``); never import an adapter module directly from control
plane code. Status mapping lives in :mod:`squadhq.trackers.status_maps`,
webhook verification/normalisation in :mod:`squadhq.trackers.webhooks`.
"""
from __future__ import annotations

import importlib
from typing import Any

from squadhq.interfaces import IssueTracker

_REGISTRY: dict[str, tuple[str, str]] = {
    "github": ("squadhq.trackers.github", "GitHubTracker"),
    "jira": ("squadhq.trackers.jira", "JiraTracker"),
    "linear": ("squadhq.trackers.linear", "LinearTracker"),
    "trello": ("squadhq.trackers.trello", "TrelloTracker"),
    "gitlab": ("squadhq.trackers.gitlab", "GitLabTracker"),
}


def available() -> list[str]:
    """Registered tracker names."""
    return sorted(_REGISTRY)


def get_tracker(name: str, **kwargs: Any) -> IssueTracker:
    """Instantiate the tracker adapter ``name``; ``kwargs`` go to its constructor."""
    try:
        module_name, cls_name = _REGISTRY[name.lower()]
    except KeyError:
        raise ValueError(f"unknown tracker {name!r}; available: {', '.join(available())}") from None
    cls = getattr(importlib.import_module(module_name), cls_name)
    tracker: IssueTracker = cls(**kwargs)
    return tracker


__all__ = ["available", "get_tracker"]
