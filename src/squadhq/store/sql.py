"""One SQL implementation of :class:`StateStore` for SQLite and Postgres.

SQL is written with ``?`` placeholders; the Postgres subclass rewrites them to
``%s``. Dialect differences are isolated in a handful of hooks:

* ``_tx(write=True)`` — a transaction. SQLite uses ``BEGIN IMMEDIATE`` so
  writers serialise; Postgres uses a normal transaction.
* ``_claim_suffix`` — ``FOR UPDATE SKIP LOCKED`` on Postgres so concurrent
  workers never block on, or double-claim, the same row.
* ``_lock_audit`` — serialises audit appends (``pg_advisory_xact_lock`` on
  Postgres; the SQLite write lock already does it).
"""
# SQL strings are assembled only from module constants (column lists, dialect
# suffixes, "?" placeholder lists); every value is a bound parameter.
# ruff: noqa: S608
from __future__ import annotations

import abc
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from squadhq.audit.chain import GENESIS_HASH, AuditEvent, seal
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
    TicketRow,
    TokenRecord,
    ts,
    utcnow,
)


class Cursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, sql: str, params: Sequence[Any] = ...) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _uj(v: Any) -> Any:
    if v is None or v == "":
        return {}
    if isinstance(v, (dict, list)):
        return v
    return json.loads(v)


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Schema. Each migration is a list of statements; {REAL} is the float type.
# ---------------------------------------------------------------------------

MIGRATIONS: list[list[str]] = [
    # 1 — initial schema
    [
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            priority INTEGER NOT NULL DEFAULT 0,
            run_after TEXT NOT NULL,
            locked_by TEXT,
            locked_until TEXT,
            idempotency_key TEXT UNIQUE,
            last_error TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, run_after)",
        """CREATE TABLE IF NOT EXISTS kv (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (namespace, key))""",
        """CREATE TABLE IF NOT EXISTS locks (
            name TEXT PRIMARY KEY,
            holder TEXT NOT NULL,
            expires_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            project TEXT NOT NULL,
            subject TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            decided_by TEXT,
            decided_at TEXT,
            decision_comment TEXT NOT NULL DEFAULT '',
            action_job_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS approvals_lookup ON approvals (kind, project, subject, status)",
        """CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            role TEXT NOT NULL,
            outcome TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            executor TEXT NOT NULL DEFAULT '',
            cost_usd {REAL} NOT NULL DEFAULT 0,
            duration_s {REAL} NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            error_class TEXT NOT NULL DEFAULT 'none',
            job_id TEXT,
            data TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at)",
        """CREATE TABLE IF NOT EXISTS tickets (
            project TEXT NOT NULL,
            key TEXT NOT NULL,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            flow_state TEXT NOT NULL,
            labels TEXT NOT NULL,
            url TEXT NOT NULL,
            tracker TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project, key))""",
        """CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            tracker TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}')""",
        """CREATE TABLE IF NOT EXISTS briefs (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL DEFAULT '',
            markdown TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY,
            id TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            subject TEXT NOT NULL,
            data TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL UNIQUE)""",
        "@audit_append_only",
    ],
    # 2 — push devices and intake requests (dashboard / mobile)
    [
        """CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            expo_push_token TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            topics TEXT NOT NULL,
            owner TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS intake_requests (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            source TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            tracker TEXT NOT NULL,
            status TEXT NOT NULL,
            ticket_key TEXT,
            ticket_url TEXT,
            job_id TEXT,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
    ],
    # 3 — project-scoped reporter tokens
    [
        "ALTER TABLE api_tokens ADD COLUMN project TEXT NOT NULL DEFAULT ''",
    ],
]


_JOB_COLS = ("id, kind, payload, status, attempts, max_attempts, run_after, created_at, updated_at, "
             "idempotency_key, priority, locked_by, locked_until, last_error, result")
_APPROVAL_COLS = ("id, kind, project, subject, title, url, status, created_at, updated_at, data, "
                  "decided_by, decided_at, decision_comment, action_job_id")
_RUN_COLS = ("id, project, role, outcome, started_at, model, executor, cost_usd, duration_s, input_tokens, "
             "output_tokens, error_class, job_id, data")
_AUDIT_COLS = "id, seq, ts, actor, action, subject, data, prev_hash, hash"
_DEVICE_COLS = "id, expo_push_token, platform, name, topics, owner, created_at"
_INTAKE_COLS = ("id, project, title, body, source, requested_by, status, created_at, updated_at, tracker, "
                "ticket_key, ticket_url, job_id, error")


def _device(r: Sequence[Any]) -> Device:
    return Device(id=r[0], expo_push_token=r[1], platform=r[2], name=r[3], topics=list(_uj(r[4]) or []),
                  owner=r[5], created_at=r[6])


def _intake(r: Sequence[Any]) -> IntakeRequest:
    return IntakeRequest(id=r[0], project=r[1], title=r[2], body=r[3], source=r[4], requested_by=r[5],
                         status=r[6], created_at=r[7], updated_at=r[8], tracker=r[9], ticket_key=r[10],
                         ticket_url=r[11], job_id=r[12], error=r[13] or "")


def _job(r: Sequence[Any]) -> Job:
    return Job(id=r[0], kind=r[1], payload=_uj(r[2]), status=r[3], attempts=int(r[4]), max_attempts=int(r[5]),
               run_after=r[6], created_at=r[7], updated_at=r[8], idempotency_key=r[9], priority=int(r[10]),
               locked_by=r[11], locked_until=r[12], last_error=r[13] or "", result=_uj(r[14]))


def _approval(r: Sequence[Any]) -> Approval:
    return Approval(id=r[0], kind=r[1], project=r[2], subject=r[3], title=r[4], url=r[5], status=r[6],
                    created_at=r[7], updated_at=r[8], data=_uj(r[9]), decided_by=r[10], decided_at=r[11],
                    decision_comment=r[12] or "", action_job_id=r[13])


def _run(r: Sequence[Any]) -> Run:
    return Run(id=r[0], project=r[1], role=r[2], outcome=r[3], started_at=r[4], model=r[5], executor=r[6],
               cost_usd=float(r[7]), duration_s=float(r[8]), input_tokens=int(r[9]), output_tokens=int(r[10]),
               error_class=r[11], job_id=r[12], data=_uj(r[13]))


def _audit(r: Sequence[Any]) -> AuditEvent:
    return AuditEvent(id=r[0], seq=int(r[1]), ts=r[2], actor=r[3], action=r[4], subject=r[5],
                      data=_uj(r[6]), prev_hash=r[7], hash=r[8])


class SQLStore(StateStore, abc.ABC):
    real_type = "REAL"

    # --- dialect hooks ----------------------------------------------------
    @abc.abstractmethod
    def _tx(self, write: bool = True) -> AbstractContextManager[Cursor]: ...

    @abc.abstractmethod
    def _append_only_ddl(self) -> list[str]: ...

    _claim_suffix = ""

    def _lock_audit(self, cur: Cursor) -> None:  # noqa: B027 - optional hook
        """Serialise audit appends (no-op where the write lock already does)."""

    # --- lifecycle --------------------------------------------------------
    def migrate(self) -> int:
        with self._tx() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            cur.execute("SELECT MAX(version) FROM schema_version")
            row = cur.fetchone()
            current = int(row[0]) if row and row[0] is not None else 0
            for idx, stmts in enumerate(MIGRATIONS, start=1):
                if idx <= current:
                    continue
                for stmt in stmts:
                    if stmt == "@audit_append_only":
                        for ddl in self._append_only_ddl():
                            cur.execute(ddl)
                        continue
                    cur.execute(stmt.replace("{REAL}", self.real_type))
                cur.execute("INSERT INTO schema_version (version) VALUES (?)", (idx,))
                current = idx
        return current

    def schema_version(self) -> int:
        try:
            with self._tx(write=False) as cur:
                cur.execute("SELECT MAX(version) FROM schema_version")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except Exception:
            return 0

    def ping(self) -> bool:
        try:
            with self._tx(write=False) as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
        except Exception:
            return False

    # --- queue ------------------------------------------------------------
    def enqueue(self, kind: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
                run_after: datetime | None = None, max_attempts: int = 5, priority: int = 0) -> EnqueueResult:
        with self._tx() as cur:
            return self._enqueue(cur, kind, payload, idempotency_key=idempotency_key, run_after=run_after,
                                 max_attempts=max_attempts, priority=priority)

    def _enqueue(self, cur: Cursor, kind: str, payload: dict[str, Any], *, idempotency_key: str | None,
                 run_after: datetime | None, max_attempts: int, priority: int) -> EnqueueResult:
        if idempotency_key:
            cur.execute("SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,))
            row = cur.fetchone()
            if row:
                return EnqueueResult(row[0], False)
        jid = new_id()
        now = ts()
        cur.execute(
            "INSERT INTO jobs (id, kind, payload, status, attempts, max_attempts, priority, run_after, "
            "idempotency_key, created_at, updated_at) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (jid, kind, _j(payload), max(1, max_attempts), priority, ts(run_after) if run_after else now,
             idempotency_key, now, now),
        )
        if cur.rowcount == 0 and idempotency_key:  # lost a race with another inserter
            cur.execute("SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,))
            row = cur.fetchone()
            return EnqueueResult(row[0], False)
        return EnqueueResult(jid, True)

    def claim(self, worker_id: str, *, lease_s: float = 300.0, kinds: list[str] | None = None) -> Job | None:
        now_dt = utcnow()
        now = ts(now_dt)
        kind_sql = ""
        params: list[Any] = [now, now]
        if kinds:
            kind_sql = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        with self._tx() as cur:
            while True:
                cur.execute(
                    f"SELECT {_JOB_COLS} FROM jobs WHERE ((status = 'queued' AND run_after <= ?) "
                    f"OR (status = 'running' AND locked_until < ?)){kind_sql} "
                    f"ORDER BY priority DESC, run_after ASC LIMIT 1{self._claim_suffix}",
                    params,
                )
                row = cur.fetchone()
                if row is None:
                    return None
                job = _job(row)
                if job.status == "running" and job.attempts >= job.max_attempts:
                    # lease expired on its final attempt (worker crashed) -> dead-letter
                    cur.execute(
                        "UPDATE jobs SET status = 'dead', locked_by = NULL, locked_until = NULL, "
                        "last_error = ?, updated_at = ? WHERE id = ?",
                        ((job.last_error + "; " if job.last_error else "") + "lease expired on final attempt",
                         now, job.id),
                    )
                    continue
                until = ts(now_dt + timedelta(seconds=lease_s))
                cur.execute(
                    "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_by = ?, locked_until = ?, "
                    "updated_at = ? WHERE id = ?",
                    (worker_id, until, now, job.id),
                )
                job.status = "running"
                job.attempts += 1
                job.locked_by = worker_id
                job.locked_until = until
                job.updated_at = now
                return job

    def extend_lease(self, job_id: str, worker_id: str, lease_s: float) -> bool:
        with self._tx() as cur:
            cur.execute("UPDATE jobs SET locked_until = ?, updated_at = ? WHERE id = ? AND locked_by = ? "
                        "AND status = 'running'",
                        (ts(utcnow() + timedelta(seconds=lease_s)), ts(), job_id, worker_id))
            return cur.rowcount == 1

    def complete(self, job_id: str, worker_id: str, result: dict[str, Any] | None = None) -> bool:
        with self._tx() as cur:
            cur.execute(
                "UPDATE jobs SET status = 'done', locked_by = NULL, locked_until = NULL, result = ?, "
                "updated_at = ? WHERE id = ? AND locked_by = ? AND status = 'running'",
                (_j(result or {}), ts(), job_id, worker_id),
            )
            return cur.rowcount == 1

    def fail(self, job_id: str, worker_id: str, error: str, *, retry_at: datetime | None) -> bool:
        with self._tx() as cur:
            if retry_at is None:
                cur.execute(
                    "UPDATE jobs SET status = 'dead', locked_by = NULL, locked_until = NULL, last_error = ?, "
                    "updated_at = ? WHERE id = ? AND locked_by = ? AND status = 'running'",
                    (error[:4000], ts(), job_id, worker_id),
                )
            else:
                cur.execute(
                    "UPDATE jobs SET status = 'queued', locked_by = NULL, locked_until = NULL, last_error = ?, "
                    "run_after = ?, updated_at = ? WHERE id = ? AND locked_by = ? AND status = 'running'",
                    (error[:4000], ts(retry_at), ts(), job_id, worker_id),
                )
            return cur.rowcount == 1

    def get_job(self, job_id: str) -> Job | None:
        with self._tx(write=False) as cur:
            cur.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            return _job(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[Job]:
        with self._tx(write=False) as cur:
            if status:
                cur.execute(f"SELECT {_JOB_COLS} FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                            (status, limit))
            else:
                cur.execute(f"SELECT {_JOB_COLS} FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
            return [_job(r) for r in cur.fetchall()]

    def retry_job(self, job_id: str) -> bool:
        with self._tx() as cur:
            cur.execute("UPDATE jobs SET status = 'queued', attempts = 0, run_after = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'dead'", (ts(), ts(), job_id))
            return cur.rowcount == 1

    def count_jobs(self) -> dict[str, int]:
        out = {"queued": 0, "running": 0, "done": 0, "dead": 0}
        with self._tx(write=False) as cur:
            cur.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
            for status, n in cur.fetchall():
                out[str(status)] = int(n)
        return out

    # --- kv + locks -------------------------------------------------------
    def kv_get(self, namespace: str, key: str) -> tuple[Any, int] | None:
        with self._tx(write=False) as cur:
            cur.execute("SELECT value, version FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
            row = cur.fetchone()
            return (json.loads(row[0]), int(row[1])) if row else None

    def kv_cas(self, namespace: str, key: str, value: Any, expected_version: int | None) -> int:
        with self._tx() as cur:
            if expected_version is None:
                cur.execute("INSERT INTO kv (namespace, key, value, version, updated_at) VALUES (?, ?, ?, 1, ?) "
                            "ON CONFLICT (namespace, key) DO NOTHING", (namespace, key, _j(value), ts()))
                if cur.rowcount != 1:
                    raise ConflictError(f"{namespace}/{key} already exists")
                return 1
            cur.execute("UPDATE kv SET value = ?, version = version + 1, updated_at = ? "
                        "WHERE namespace = ? AND key = ? AND version = ?",
                        (_j(value), ts(), namespace, key, expected_version))
            if cur.rowcount != 1:
                raise ConflictError(f"{namespace}/{key} is not at version {expected_version}")
            return expected_version + 1

    def kv_put(self, namespace: str, key: str, value: Any) -> int:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO kv (namespace, key, value, version, updated_at) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value, "
                "version = kv.version + 1, updated_at = excluded.updated_at",
                (namespace, key, _j(value), ts()),
            )
            cur.execute("SELECT version FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
            return int(cur.fetchone()[0])

    def acquire_lock(self, name: str, holder: str, ttl_s: float) -> bool:
        now_dt = utcnow()
        expires = ts(now_dt + timedelta(seconds=ttl_s))
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO locks (name, holder, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, expires_at = excluded.expires_at "
                "WHERE locks.holder = excluded.holder OR locks.expires_at < ?",
                (name, holder, expires, ts(now_dt)),
            )
            return cur.rowcount == 1

    def release_lock(self, name: str, holder: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))

    # --- approvals --------------------------------------------------------
    def upsert_approval(self, *, kind: str, project: str, subject: str, title: str, url: str,
                        data: dict[str, Any] | None = None) -> tuple[Approval, bool]:
        with self._tx() as cur:
            cur.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE kind = ? AND project = ? AND subject = ? "
                        "AND status = 'pending'", (kind, project, subject))
            row = cur.fetchone()
            if row:
                return _approval(row), False
            now = ts()
            a = Approval(id=new_id(), kind=kind, project=project, subject=subject, title=title, url=url,
                         status="pending", created_at=now, updated_at=now, data=data or {})
            cur.execute(f"INSERT INTO approvals ({_APPROVAL_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (a.id, a.kind, a.project, a.subject, a.title, a.url, a.status, a.created_at, a.updated_at,
                         _j(a.data), None, None, "", None))
            self._append_audit(cur, "squadhq-worker", "approval.requested", f"{project}:{subject}",
                               {"approval_id": a.id, "kind": kind, "url": url})
            return a, True

    def resolve_approvals(self, *, kind: str, project: str, subject: str, actor: str,
                          action: str = "approval.resolved_externally", data: dict[str, Any] | None = None) -> int:
        with self._tx() as cur:
            cur.execute("UPDATE approvals SET status = 'resolved', decided_by = ?, decided_at = ?, updated_at = ? "
                        "WHERE kind = ? AND project = ? AND subject = ? AND status = 'pending'",
                        (actor, ts(), ts(), kind, project, subject))
            n = cur.rowcount
            if n:
                self._append_audit(cur, actor, action, f"{project}:{subject}", {"kind": kind, **(data or {})})
            return int(n)

    def get_approval(self, approval_id: str) -> Approval | None:
        with self._tx(write=False) as cur:
            cur.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE id = ?", (approval_id,))
            row = cur.fetchone()
            return _approval(row) if row else None

    def list_approvals(self, status: str | None = "pending", project: str | None = None,
                       limit: int = 200) -> list[Approval]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if project:
            where.append("project = ?")
            params.append(project)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._tx(write=False) as cur:
            cur.execute(f"SELECT {_APPROVAL_COLS} FROM approvals{clause} ORDER BY created_at ASC LIMIT ?",
                        [*params, limit])
            return [_approval(r) for r in cur.fetchall()]

    def decide_approval(self, approval_id: str, *, decision: Literal["approve", "reject"], actor: str,
                        comment: str, action_job: tuple[str, dict[str, Any]] | None) -> tuple[Approval, AuditEvent]:
        status = "approved" if decision == "approve" else "rejected"
        with self._tx() as cur:
            cur.execute(f"SELECT {_APPROVAL_COLS} FROM approvals WHERE id = ?", (approval_id,))
            row = cur.fetchone()
            if not row:
                raise NotFoundError(approval_id)
            a = _approval(row)
            if a.status != "pending":
                raise ConflictError(f"approval {approval_id} is already {a.status}")
            now = ts()
            job_id = None
            if action_job:
                kind, payload = action_job
                job_id = self._enqueue(cur, kind, {**payload, "approval_id": approval_id},
                                       idempotency_key=f"approval:{approval_id}:{status}", run_after=None,
                                       max_attempts=5, priority=10).job_id
            cur.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, decision_comment = ?, "
                        "action_job_id = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                        (status, actor, now, comment, job_id, now, approval_id))
            if cur.rowcount != 1:  # pragma: no cover - guarded by the write lock
                raise ConflictError(f"approval {approval_id} changed concurrently")
            ev = self._append_audit(cur, actor, f"approval.{status}", f"{a.project}:{a.subject}",
                                    {"approval_id": approval_id, "kind": a.kind, "comment": comment,
                                     "action_job_id": job_id})
            a.status = status  # type: ignore[assignment]
            a.decided_by, a.decided_at, a.decision_comment, a.action_job_id, a.updated_at = (
                actor, now, comment, job_id, now)
            return a, ev

    # --- runs / tickets / projects / briefs ------------------------------
    def record_run(self, run: Run) -> None:
        with self._tx() as cur:
            cur.execute(
                f"INSERT INTO runs ({_RUN_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO NOTHING",
                (run.id, run.project, run.role, run.outcome, run.started_at, run.model, run.executor,
                 float(run.cost_usd), float(run.duration_s), int(run.input_tokens), int(run.output_tokens),
                 run.error_class, run.job_id, _j(run.data)),
            )

    def list_runs(self, project: str | None = None, limit: int = 50) -> list[Run]:
        with self._tx(write=False) as cur:
            if project:
                cur.execute(f"SELECT {_RUN_COLS} FROM runs WHERE project = ? ORDER BY started_at DESC LIMIT ?",
                            (project, limit))
            else:
                cur.execute(f"SELECT {_RUN_COLS} FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
            return [_run(r) for r in cur.fetchall()]

    def cost_summary(self, month: str) -> dict[str, Any]:
        prefix = month + "-"
        out: dict[str, Any] = {"month": month, "total_usd": 0.0, "runs": 0,
                               "by_project": {}, "by_role": {}, "by_model": {}}
        with self._tx(write=False) as cur:
            cur.execute("SELECT project, role, model, cost_usd FROM runs WHERE started_at LIKE ?", (prefix + "%",))
            for project, role, model, cost in cur.fetchall():
                c = float(cost or 0)
                out["total_usd"] += c
                out["runs"] += 1
                for bucket, k in (("by_project", project), ("by_role", role), ("by_model", model or "unknown")):
                    out[bucket][k] = round(out[bucket].get(k, 0.0) + c, 6)
        out["total_usd"] = round(out["total_usd"], 6)
        return out

    def upsert_ticket(self, t: TicketRow) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO tickets (project, key, title, state, flow_state, labels, url, tracker, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (project, key) DO UPDATE SET "
                "title = excluded.title, state = excluded.state, flow_state = excluded.flow_state, "
                "labels = excluded.labels, url = excluded.url, tracker = excluded.tracker, "
                "updated_at = excluded.updated_at",
                (t.project, t.key, t.title, t.state, t.flow_state, _j(t.labels), t.url, t.tracker, t.updated_at),
            )

    def list_tickets(self, project: str | None = None, state: str | None = None, limit: int = 500) -> list[TicketRow]:
        where: list[str] = []
        params: list[Any] = []
        if project:
            where.append("project = ?")
            params.append(project)
        if state:
            where.append("(state = ? OR flow_state = ?)")
            params.extend([state, state])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._tx(write=False) as cur:
            cur.execute("SELECT project, key, title, state, flow_state, labels, url, tracker, updated_at FROM tickets"
                        f"{clause} ORDER BY updated_at DESC LIMIT ?", [*params, limit])
            return [TicketRow(project=r[0], key=r[1], title=r[2], state=r[3], flow_state=r[4],
                              labels=list(_uj(r[5]) or []), url=r[6], tracker=r[7], updated_at=r[8])
                    for r in cur.fetchall()]

    def upsert_project(self, p: Project) -> None:
        with self._tx() as cur:
            cur.execute("INSERT INTO projects (name, repo, tracker, data) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (name) DO UPDATE SET repo = excluded.repo, tracker = excluded.tracker, "
                        "data = excluded.data", (p.name, p.repo, p.tracker, _j(p.data)))

    def list_projects(self) -> list[Project]:
        with self._tx(write=False) as cur:
            cur.execute("SELECT name, repo, tracker, data FROM projects ORDER BY name")
            return [Project(name=r[0], repo=r[1], tracker=r[2], data=_uj(r[3])) for r in cur.fetchall()]

    def add_brief(self, markdown: str, project: str = "") -> Brief:
        b = Brief(id=new_id(), created_at=ts(), markdown=markdown, project=project)
        with self._tx() as cur:
            cur.execute("INSERT INTO briefs (id, project, markdown, created_at) VALUES (?, ?, ?, ?)",
                        (b.id, b.project, b.markdown, b.created_at))
        return b

    def latest_brief(self, project: str | None = None) -> Brief | None:
        with self._tx(write=False) as cur:
            if project is not None:
                cur.execute("SELECT id, created_at, markdown, project FROM briefs WHERE project = ? "
                            "ORDER BY created_at DESC LIMIT 1", (project,))
            else:
                cur.execute("SELECT id, created_at, markdown, project FROM briefs ORDER BY created_at DESC LIMIT 1")
            r = cur.fetchone()
            return Brief(id=r[0], created_at=r[1], markdown=r[2], project=r[3]) if r else None

    # --- tokens -----------------------------------------------------------
    def add_token(self, name: str, role: str, token_hash: str, project: str = "") -> TokenRecord:
        rec = TokenRecord(id=new_id(), name=name, role=role, token_hash=token_hash, created_at=ts(), project=project)
        with self._tx() as cur:
            cur.execute("INSERT INTO api_tokens (id, name, role, token_hash, revoked, created_at, project) "
                        "VALUES (?, ?, ?, ?, 0, ?, ?)", (rec.id, name, role, token_hash, rec.created_at, project))
            self._append_audit(cur, "cli", "token.created", f"token:{rec.id}",
                               {"name": name, "role": role, "project": project})
        return rec

    def list_tokens(self, include_revoked: bool = False) -> list[TokenRecord]:
        with self._tx(write=False) as cur:
            sql = "SELECT id, name, role, token_hash, created_at, revoked, project FROM api_tokens"
            if not include_revoked:
                sql += " WHERE revoked = 0"
            cur.execute(sql + " ORDER BY created_at")
            return [TokenRecord(id=r[0], name=r[1], role=r[2], token_hash=r[3], created_at=r[4],
                                revoked=bool(r[5]), project=r[6] or "") for r in cur.fetchall()]

    def revoke_token(self, token_id: str, actor: str = "api") -> bool:
        with self._tx() as cur:
            cur.execute("UPDATE api_tokens SET revoked = 1 WHERE id = ? AND revoked = 0", (token_id,))
            ok = cur.rowcount == 1
            if ok:
                self._append_audit(cur, actor, "token.revoked", f"token:{token_id}", {})
            return ok

    # --- devices ----------------------------------------------------------
    def upsert_device(self, *, expo_push_token: str, platform: str, name: str, topics: list[str],
                      owner: str, allow_takeover: bool = False) -> tuple[Device, bool]:
        with self._tx() as cur:
            cur.execute(f"SELECT {_DEVICE_COLS} FROM devices WHERE expo_push_token = ?", (expo_push_token,))
            row = cur.fetchone()
            if row:
                d = _device(row)
                if d.owner != owner and not allow_takeover:
                    raise ConflictError("push token is registered to another principal")
                cur.execute("UPDATE devices SET platform = ?, name = ?, topics = ?, owner = ? WHERE id = ?",
                            (platform, name, _j(topics), owner, d.id))
                d.platform, d.name, d.topics, d.owner = platform, name, topics, owner
                return d, False
            d = Device(id=new_id(), expo_push_token=expo_push_token, platform=platform, name=name, topics=topics,
                       owner=owner, created_at=ts())
            cur.execute(f"INSERT INTO devices ({_DEVICE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (d.id, d.expo_push_token, d.platform, d.name, _j(d.topics), d.owner, d.created_at))
            self._append_audit(cur, owner, "device.registered", f"device:{d.id}",
                               {"platform": platform, "topics": topics})
            return d, True

    def get_device(self, device_id: str) -> Device | None:
        with self._tx(write=False) as cur:
            cur.execute(f"SELECT {_DEVICE_COLS} FROM devices WHERE id = ?", (device_id,))
            row = cur.fetchone()
            return _device(row) if row else None

    def delete_device(self, device_id: str) -> bool:
        with self._tx() as cur:
            cur.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            return cur.rowcount == 1

    def delete_devices_by_token(self, tokens: list[str]) -> int:
        if not tokens:
            return 0
        with self._tx() as cur:
            cur.execute("DELETE FROM devices WHERE expo_push_token IN (" + ",".join("?" for _ in tokens) + ")",
                        list(tokens))
            return int(cur.rowcount)

    def list_devices(self, topic: str | None = None) -> list[Device]:
        with self._tx(write=False) as cur:
            cur.execute(f"SELECT {_DEVICE_COLS} FROM devices ORDER BY created_at")
            devices = [_device(r) for r in cur.fetchall()]
        return [d for d in devices if topic is None or topic in d.topics]

    # --- intake requests --------------------------------------------------
    def create_intake(self, *, project: str, title: str, body: str, source: str, requested_by: str,
                      tracker: str) -> IntakeRequest:
        now = ts()
        req = IntakeRequest(id=new_id(), project=project, title=title, body=body, source=source,
                            requested_by=requested_by, status="queued", created_at=now, updated_at=now,
                            tracker=tracker)
        with self._tx() as cur:
            req.job_id = self._enqueue(cur, "intake_ticket", {"intake_id": req.id},
                                       idempotency_key=f"intake:{req.id}", run_after=None, max_attempts=5,
                                       priority=5).job_id
            cur.execute(f"INSERT INTO intake_requests ({_INTAKE_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (req.id, project, title, body, source, requested_by, req.status, now, now, tracker,
                         None, None, req.job_id, ""))
            self._append_audit(cur, requested_by, "intake.requested", f"{project}:intake:{req.id}",
                               {"title": title, "source": source, "job_id": req.job_id})
        return req

    def get_intake(self, intake_id: str) -> IntakeRequest | None:
        with self._tx(write=False) as cur:
            cur.execute(f"SELECT {_INTAKE_COLS} FROM intake_requests WHERE id = ?", (intake_id,))
            row = cur.fetchone()
            return _intake(row) if row else None

    def update_intake(self, intake_id: str, *, status: str, ticket_key: str | None = None,
                      ticket_url: str | None = None, error: str = "") -> None:
        with self._tx() as cur:
            cur.execute("UPDATE intake_requests SET status = ?, ticket_key = COALESCE(?, ticket_key), "
                        "ticket_url = COALESCE(?, ticket_url), error = ?, updated_at = ? WHERE id = ?",
                        (status, ticket_key, ticket_url, error[:2000], ts(), intake_id))

    # --- audit ------------------------------------------------------------
    def append_audit(self, actor: str, action: str, subject: str, data: dict[str, Any] | None = None) -> AuditEvent:
        with self._tx() as cur:
            return self._append_audit(cur, actor, action, subject, data or {})

    def _append_audit(self, cur: Cursor, actor: str, action: str, subject: str,
                      data: dict[str, Any]) -> AuditEvent:
        self._lock_audit(cur)
        cur.execute("SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        prev_seq, prev_hash = (int(row[0]), row[1]) if row else (0, GENESIS_HASH)
        # round-trip data through JSON so the hashed body equals what is stored and read back
        clean = json.loads(_j(data))
        ev = seal(id=new_id(), seq=prev_seq + 1, ts=ts(), actor=actor, action=action, subject=subject,
                  data=clean, prev_hash=prev_hash)
        cur.execute(f"INSERT INTO audit_events ({_AUDIT_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ev.id, ev.seq, ev.ts, ev.actor, ev.action, ev.subject, _j(ev.data), ev.prev_hash, ev.hash))
        return ev

    def iter_audit(self, since_seq: int = 0, since_ts: str | None = None,
                   limit: int | None = None) -> Iterator[AuditEvent]:
        sql = f"SELECT {_AUDIT_COLS} FROM audit_events WHERE seq > ?"
        params: list[Any] = [since_seq]
        if since_ts:
            sql += " AND ts >= ?"
            params.append(since_ts)
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._tx(write=False) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        for r in rows:
            yield _audit(r)
