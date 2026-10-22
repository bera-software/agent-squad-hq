"""Exercise PostgresStore's code paths without a server.

A fake ``psycopg.connect`` hands out a connection backed by SQLite that
accepts ``%s`` placeholders, records every statement, and tolerates the
Postgres-only bits (``FOR UPDATE SKIP LOCKED``, advisory locks, plpgsql DDL).
This verifies placeholder translation, transactions and the dialect hooks;
real-Postgres behaviour (SKIP LOCKED under contention) is covered by the
compose smoke test in docs/deploy/docker-compose.md.
"""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from squadhq.store.postgres import PostgresStore, translate


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.cur = conn.db.cursor()
        self.rowcount = -1

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.conn.log.append(sql)
        if "plpgsql" in sql or "TRIGGER" in sql:
            return
        if "pg_advisory_xact_lock" in sql:
            sql = "SELECT 1"
            params = ()
        sql = sql.replace(" FOR UPDATE SKIP LOCKED", "").replace("%s", "?")
        self.cur.execute(sql, params)
        self.rowcount = self.cur.rowcount

    def fetchone(self) -> Any:
        return self.cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self.cur.fetchall()

    def close(self) -> None:
        self.cur.close()


class FakeConn:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.log: list[str] = []
        self.closed = False
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.db.rollback()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pg() -> tuple[PostgresStore, FakeConn]:
    conns: list[FakeConn] = []

    def connect(dsn: str, autocommit: bool = False) -> FakeConn:
        assert dsn.startswith("postgresql://") and autocommit is False
        c = FakeConn()
        conns.append(c)
        return c

    s = PostgresStore("postgresql://u:p@db/squadhq", connect=connect)
    s.migrate()
    return s, conns[0]


def test_translate() -> None:
    assert translate("SELECT ? , ?") == "SELECT %s , %s"


def test_postgres_dialect_paths(pg: tuple[PostgresStore, FakeConn]) -> None:
    store, conn = pg
    assert store.dialect == "postgres"
    ddl = "\n".join(conn.log)
    assert "DOUBLE PRECISION" in ddl and "plpgsql" in ddl and "?" not in ddl.replace("'?'", "")
    j = store.enqueue("x", {"a": 1}, idempotency_key="k").job_id
    claimed = store.claim("w")
    assert claimed is not None and claimed.id == j
    assert any("FOR UPDATE SKIP LOCKED" in s for s in conn.log)
    assert all("?" not in s for s in conn.log if s.startswith(("SELECT", "INSERT", "UPDATE")))
    store.append_audit("a", "b", "c")
    assert any("pg_advisory_xact_lock" in s for s in conn.log)
    assert store.complete(j, "w")
    assert store.ping()


def test_postgres_rollback_and_reconnect(pg: tuple[PostgresStore, FakeConn]) -> None:
    store, conn = pg
    with pytest.raises(sqlite3.OperationalError):
        with store._tx() as cur:
            cur.execute("SELECT * FROM no_such_table")
    assert conn.rollbacks == 1
    conn.closed = True
    assert store.ping()  # reconnects transparently
    store.close()


def test_missing_driver_message(monkeypatch: Any) -> None:
    import builtins

    real = builtins.__import__

    def fake_import(name: str, *a: Any, **kw: Any) -> Any:
        if name == "psycopg":
            raise ImportError("no psycopg")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="psycopg"):
        PostgresStore("postgresql://x")
