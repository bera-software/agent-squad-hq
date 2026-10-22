"""Pluggable state store for server mode.

``open_store(url)`` accepts:

* ``sqlite:///relative/path.db``, ``sqlite:////abs/path.db``, ``sqlite://:memory:``
  (stdlib, the default);
* ``postgresql://user:pass@host:5432/db`` / ``postgres://...`` (needs ``psycopg``).
"""
from __future__ import annotations

from squadhq.store.base import (
    Approval,
    Brief,
    ConflictError,
    Device,
    EnqueueResult,
    IntakeRequest,
    Job,
    NotFoundError,
    Project,
    Run,
    StateStore,
    StoreError,
    TicketRow,
    TokenRecord,
    parse_ts,
    ts,
    utcnow,
)

__all__ = [
    "Approval",
    "Brief",
    "ConflictError",
    "Device",
    "EnqueueResult",
    "IntakeRequest",
    "Job",
    "NotFoundError",
    "Project",
    "Run",
    "StateStore",
    "StoreError",
    "TicketRow",
    "TokenRecord",
    "open_store",
    "parse_ts",
    "redact_url",
    "ts",
    "utcnow",
]


def _sqlite_path(url: str) -> str:
    rest = url[len("sqlite://"):]
    if rest in ("", "/", "/:memory:", ":memory:"):
        return ":memory:"
    # sqlite:///rel.db -> "rel.db"; sqlite:////abs.db -> "/abs.db"
    return rest[1:] if rest.startswith("/") else rest


def open_store(url: str, *, migrate: bool = True) -> StateStore:
    """Open (and by default migrate) the store at ``url``."""
    store: StateStore
    if url.startswith("sqlite:"):
        from squadhq.store.sqlite import SQLiteStore

        store = SQLiteStore(_sqlite_path(url))
    elif url.startswith(("postgresql://", "postgres://")):
        from squadhq.store.postgres import PostgresStore

        store = PostgresStore(url)
    else:
        raise ValueError(f"unsupported store URL scheme: {redact_url(url)!r}")
    if migrate:
        store.migrate()
    return store


def redact_url(url: str) -> str:
    """Hide the password in a DSN for logs."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
