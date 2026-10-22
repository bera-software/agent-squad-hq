"""Small helpers shared by tracker adapters."""
from __future__ import annotations

import os
import sys
from typing import Any

from squadhq.interfaces import AuthorAssociation, FatalError

_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE", "BOT"}


def env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def require(value: str | None, what: str, env_names: str) -> str:
    if not value:
        raise FatalError(f"{what} not configured: pass it explicitly or set {env_names}")
    return value


def association(value: Any) -> AuthorAssociation:
    v = str(value or "NONE").upper()
    if v == "FIRST_TIME_CONTRIBUTOR" or v == "FIRST_TIMER":
        return "CONTRIBUTOR"
    if v == "MANNEQUIN":
        return "NONE"
    return v if v in _ASSOCIATIONS else "NONE"  # type: ignore[return-value]


def warn(msg: str) -> None:
    print(f"squadhq.trackers: warning: {msg}", file=sys.stderr, flush=True)


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
