"""Bearer-token authentication with role-based access.

Tokens are random 256-bit secrets (``sqh_<base64url>``). The server only
ever stores ``sha256(token)``: in the database (``create-token``) or in
``SQUADHQ_API_TOKENS`` as ``name:role:sha256hex`` entries separated by commas.
Because tokens carry 256 bits of entropy a fast hash is sufficient (no
password-stretching needed); comparison uses :func:`hmac.compare_digest`
against every candidate so timing does not reveal which entry matched.

Roles are ordered ``viewer < approver < admin``. ``reporter`` sits outside
that ladder: a reporter token is bound to exactly one project at mint time
and may only report runs (``POST /api/v1/runs``) for that project — it is
what a project's ``squadhq-agent.yml`` holds as ``SQUADHQ_SERVER_TOKEN``, so
a project maintainer who can read the secret gains nothing beyond that.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

HIERARCHY = ("viewer", "approver", "admin")
REPORTER = "reporter"
ROLES = (*HIERARCHY, REPORTER)
_RANK = {r: i for i, r in enumerate(HIERARCHY)}  # reporter has no rank: it passes no hierarchy check
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TOKEN_PREFIX = "sqh_"  # noqa: S105 - a public prefix, not a secret


@dataclass(frozen=True)
class StaticToken:
    name: str
    role: str
    token_hash: str
    project: str = ""  # reporter tokens only


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    token_id: str = ""
    project: str = ""  # set (and only set) for reporter tokens

    @property
    def is_reporter(self) -> bool:
        return self.role == REPORTER

    def has(self, role: str) -> bool:
        return self.role in _RANK and _RANK[self.role] >= _RANK[role]

    def may_report_for(self, project: str) -> bool:
        if self.has("admin"):
            return True
        return self.is_reporter and bool(self.project) and self.project.lower() == project.strip().lower()


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_static_tokens(spec: str) -> list[StaticToken]:
    out = []
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) not in (3, 4) or len(parts[2]) != 64:
            raise ValueError("SQUADHQ_API_TOKENS entries must be name:role:sha256hex[:owner/repo]")
        name, role, digest = parts[:3]
        project = parts[3].strip() if len(parts) == 4 else ""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r} for token {name!r}")
        check_scope(role, project)
        out.append(StaticToken(name=name, role=role, token_hash=digest.lower(), project=project))
    return out


def check_scope(role: str, project: str) -> None:
    """Reporter tokens need exactly one ``owner/name`` project; other roles take none."""
    if role == REPORTER:
        if not _PROJECT_RE.match(project or ""):
            raise ValueError("a reporter token must be bound to one project: --project owner/repo")
    elif project:
        raise ValueError(f"only reporter tokens are project-scoped (got role {role!r} with a project)")


def match(presented: str, candidates: Iterable[tuple[str, ...]]) -> Principal | None:
    """``candidates`` = ``(token_id, name, role, sha256hex[, project])``. Constant-time per candidate."""
    digest = hash_token(presented)
    found: Principal | None = None
    for cand in candidates:
        token_id, name, role, h = cand[:4]
        project = cand[4] if len(cand) > 4 else ""
        if hmac.compare_digest(digest, h) and found is None:
            found = Principal(name=name, role=role, token_id=token_id, project=project)
    return found


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
