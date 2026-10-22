"""Postgres state store (optional dependency ``psycopg`` >= 3).

Install with ``pip install 'agent-squad-hq[postgres]'`` (or ``psycopg[binary]``).

* Job claims use ``SELECT ... FOR UPDATE SKIP LOCKED`` so any number of
  workers can poll the same queue without blocking or double-claiming.
* Audit appends take ``pg_advisory_xact_lock`` so the chain stays linear.
* A trigger rejects UPDATE/DELETE on ``audit_events``; additionally grant the
  application role only ``INSERT, SELECT`` on that table (see
  docs/deploy/security-hardening.md).
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from squadhq.store.sql import Cursor, SQLStore

_AUDIT_LOCK_KEY = 0x5D4A0D17  # arbitrary, stable advisory-lock id for audit appends
_PLACEHOLDER = re.compile(r"\?")


class _PgCursor:
    """Adapts psycopg's ``%s`` paramstyle to the ``?`` SQL used by :class:`SQLStore`."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._cur.execute(translate(sql), tuple(params))

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cur.fetchall())


def translate(sql: str) -> str:
    """``?`` -> ``%s``. The store's SQL never contains a literal ``?`` or ``%``."""
    return _PLACEHOLDER.sub("%s", sql)


class PostgresStore(SQLStore):
    dialect = "postgres"
    real_type = "DOUBLE PRECISION"
    _claim_suffix = " FOR UPDATE SKIP LOCKED"

    def __init__(self, dsn: str, *, connect: Any = None) -> None:
        if connect is None:
            try:
                import psycopg  # type: ignore[import-not-found,unused-ignore]
            except ImportError as e:  # pragma: no cover - depends on the environment
                raise RuntimeError(
                    "Postgres store needs psycopg: pip install 'psycopg[binary]>=3.1'"
                ) from e
            connect = psycopg.connect
        self._connect = connect
        self._dsn = dsn
        self._lock = threading.RLock()
        self._conn: Any = connect(dsn, autocommit=False)
        self._depth = 0

    @contextmanager
    def _tx(self, write: bool = True) -> Iterator[Cursor]:
        with self._lock:
            if getattr(self._conn, "closed", False):  # reconnect after a dropped connection
                self._conn = self._connect(self._dsn, autocommit=False)
            raw = self._conn.cursor()
            cur = _PgCursor(raw)
            if self._depth:
                self._depth += 1
                try:
                    yield cur
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                yield cur
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()
            finally:
                self._depth = 0
                raw.close()

    def _lock_audit(self, cur: Cursor) -> None:
        cur.execute("SELECT pg_advisory_xact_lock(?)", (_AUDIT_LOCK_KEY,))

    def _append_only_ddl(self) -> list[str]:
        return [
            "CREATE OR REPLACE FUNCTION squadhq_audit_append_only() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$ LANGUAGE plpgsql",
            "DROP TRIGGER IF EXISTS audit_append_only ON audit_events",
            "CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION squadhq_audit_append_only()",
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
