"""SQLite state store (stdlib ``sqlite3``; the default backend).

* WAL journal + ``busy_timeout`` so the API, worker and scheduler processes
  can share one database file on a single host.
* Every write runs in ``BEGIN IMMEDIATE``: the claim's SELECT+UPDATE and
  the audit append's read-head+insert are therefore atomic across processes.
* One connection per store, guarded by a re-entrant lock, so FastAPI's
  thread pool can share it.
* Triggers make ``audit_events`` append-only.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from squadhq.store.sql import Cursor, SQLStore


class SQLiteStore(SQLStore):
    dialect = "sqlite"

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 10_000) -> None:
        self.path = path
        if path not in (":memory:", "") and not path.startswith("file:"):
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False,
                                     timeout=busy_timeout_ms / 1000, uri=path.startswith("file:"))
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0

    @contextmanager
    def _tx(self, write: bool = True) -> Iterator[Cursor]:
        with self._lock:
            cur: Any = self._conn.cursor()
            if self._depth:  # nested use inside an open transaction
                self._depth += 1
                try:
                    yield cur
                finally:
                    self._depth -= 1
                return
            cur.execute("BEGIN IMMEDIATE" if write else "BEGIN")
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
                cur.close()

    def _append_only_ddl(self) -> list[str]:
        return [
            "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
