from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from typing import Any

import pytest

from squadhq.store import ConflictError, NotFoundError, Run, TicketRow, open_store, redact_url, utcnow
from squadhq.store.base import Project, parse_ts, ts
from squadhq.store.sqlite import SQLiteStore


@pytest.fixture
def store() -> SQLiteStore:
    s = SQLiteStore(":memory:")
    s.migrate()
    return s


def _expire(store: SQLiteStore, job_id: str) -> None:
    with store._tx() as cur:
        cur.execute("UPDATE jobs SET locked_until = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (job_id,))


def test_migrate_is_idempotent(store: SQLiteStore) -> None:
    assert store.migrate() == 3
    assert store.schema_version() == 3
    assert store.ping()


def test_enqueue_idempotency_and_priority(store: SQLiteStore) -> None:
    a = store.enqueue("x", {"n": 1}, idempotency_key="k")
    b = store.enqueue("x", {"n": 2}, idempotency_key="k")
    assert a.created and not b.created and a.job_id == b.job_id
    hi = store.enqueue("y", {}, priority=10)
    store.enqueue("z", {}, run_after=utcnow() + timedelta(hours=1))
    first = store.claim("w")
    assert first.id == hi.job_id and first.status == "running" and first.attempts == 1
    second = store.claim("w")
    assert second.id == a.job_id and second.payload == {"n": 1}
    assert store.claim("w") is None  # z is in the future
    assert store.claim("w", kinds=["z"]) is None
    assert store.count_jobs() == {"queued": 1, "running": 2, "done": 0, "dead": 0}


def test_claim_filter_by_kind(store: SQLiteStore) -> None:
    store.enqueue("a", {})
    b = store.enqueue("b", {})
    assert store.claim("w", kinds=["b"]).id == b.job_id


def test_complete_and_fail_require_lease_owner(store: SQLiteStore) -> None:
    j = store.enqueue("x", {}).job_id
    store.claim("w1")
    assert not store.complete(j, "w2")
    assert not store.fail(j, "w2", "e", retry_at=None)
    assert store.extend_lease(j, "w1", 60)
    assert not store.extend_lease(j, "w2", 60)
    assert store.complete(j, "w1", {"ok": True})
    assert store.get_job(j).result == {"ok": True}
    assert not store.complete(j, "w1")  # already done


def test_fail_retry_and_dead_letter(store: SQLiteStore) -> None:
    j = store.enqueue("x", {}, max_attempts=3).job_id
    store.claim("w")
    assert store.fail(j, "w", "boom", retry_at=utcnow() - timedelta(seconds=1))
    job = store.get_job(j)
    assert job.status == "queued" and job.last_error == "boom"
    store.claim("w")
    assert store.fail(j, "w", "fatal", retry_at=None)
    assert store.get_job(j).status == "dead"
    assert [x.id for x in store.list_jobs("dead")] == [j]
    assert store.retry_job(j)
    assert store.get_job(j).attempts == 0 and store.get_job(j).status == "queued"
    assert not store.retry_job(j)


def test_expired_lease_is_reclaimed_then_dead_on_last_attempt(store: SQLiteStore) -> None:
    j = store.enqueue("x", {}, max_attempts=2).job_id
    store.claim("crashed-1")
    _expire(store, j)
    again = store.claim("w2")
    assert again.id == j and again.attempts == 2 and again.locked_by == "w2"
    _expire(store, j)
    assert store.claim("w3") is None  # final attempt's lease expired -> dead-lettered
    dead = store.get_job(j)
    assert dead.status == "dead" and "lease expired" in dead.last_error


def test_concurrent_claims_never_double_claim(tmp_path: Any) -> None:
    path = str(tmp_path / "q.db")
    seed = SQLiteStore(path)
    seed.migrate()
    for i in range(60):
        seed.enqueue("x", {"i": i})
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        s = SQLiteStore(path)  # separate connection, like separate processes
        while True:
            j = s.claim(name)
            if j is None:
                return
            with lock:
                claimed.append(j.id)
            s.complete(j.id, name)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 60 and len(set(claimed)) == 60
    assert seed.count_jobs()["done"] == 60


def test_kv_compare_and_set(store: SQLiteStore) -> None:
    assert store.kv_get("ns", "k") is None
    assert store.kv_cas("ns", "k", {"a": 1}, None) == 1
    with pytest.raises(ConflictError):
        store.kv_cas("ns", "k", {"a": 2}, None)
    with pytest.raises(ConflictError):
        store.kv_cas("ns", "k", {"a": 2}, 7)
    assert store.kv_cas("ns", "k", {"a": 2}, 1) == 2
    assert store.kv_get("ns", "k") == ({"a": 2}, 2)
    assert store.kv_put("ns", "k", "x") == 3
    assert store.kv_put("ns", "new", 1) == 1


def test_locks(store: SQLiteStore) -> None:
    assert store.acquire_lock("l", "a", 60)
    assert not store.acquire_lock("l", "b", 60)
    assert store.acquire_lock("l", "a", 60)  # renew
    store.release_lock("l", "b")  # not the holder: no-op
    assert not store.acquire_lock("l", "b", 60)
    store.release_lock("l", "a")
    assert store.acquire_lock("l", "b", 60)
    store.acquire_lock("l", "b", -5)  # expire
    assert store.acquire_lock("l", "c", 60)


def test_approvals_and_audit_in_one_transaction(store: SQLiteStore) -> None:
    a, created = store.upsert_approval(kind="merge", project="p", subject="pr#1", title="t", url="u", data={"x": 1})
    again, created2 = store.upsert_approval(kind="merge", project="p", subject="pr#1", title="t", url="u")
    assert created and not created2 and again.id == a.id
    with pytest.raises(NotFoundError):
        store.decide_approval("nope", decision="approve", actor="x", comment="", action_job=None)
    decided, ev = store.decide_approval(a.id, decision="reject", actor="lead", comment="no",
                                        action_job=("approval_action", {"k": 1}))
    assert decided.status == "rejected" and ev.action == "approval.rejected"
    job = store.get_job(decided.action_job_id)
    assert job.payload == {"k": 1, "approval_id": a.id} and job.priority == 10
    with pytest.raises(ConflictError):
        store.decide_approval(a.id, decision="approve", actor="x", comment="", action_job=None)
    # a new request for the same subject after a decision opens a fresh approval
    b, created3 = store.upsert_approval(kind="merge", project="p", subject="pr#1", title="t", url="u")
    assert created3 and b.id != a.id
    assert store.resolve_approvals(kind="merge", project="p", subject="pr#1", actor="gh") == 1
    assert store.resolve_approvals(kind="merge", project="p", subject="pr#1", actor="gh") == 0
    assert len(store.list_approvals(None, project="p")) == 2
    assert store.list_approvals("pending") == []


def test_decision_rolls_back_on_failure(store: SQLiteStore, monkeypatch: Any) -> None:
    a, _ = store.upsert_approval(kind="merge", project="p", subject="pr#2", title="t", url="u")
    n_events = len(list(store.iter_audit()))

    def boom(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_append_audit", boom)
    with pytest.raises(RuntimeError):
        store.decide_approval(a.id, decision="approve", actor="x", comment="", action_job=("approval_action", {}))
    monkeypatch.undo()
    assert store.get_approval(a.id).status == "pending"  # nothing half-applied
    assert store.count_jobs()["queued"] == 0
    assert len(list(store.iter_audit())) == n_events


def test_runs_costs_tickets_projects_briefs_tokens(store: SQLiteStore) -> None:
    store.record_run(Run(id="r", project="p", role="implement", outcome="success",
                         started_at="2026-10-01T00:00:00.000000Z", cost_usd=2))
    store.record_run(Run(id="r", project="p", role="implement", outcome="success",
                         started_at="2026-10-01T00:00:00.000000Z", cost_usd=2))  # idempotent
    assert store.cost_summary("2026-10")["total_usd"] == 2
    assert store.cost_summary("2026-11")["runs"] == 0
    store.upsert_ticket(TicketRow("p", "1", "a", "open", "new", ["x"], "u", "github", "t1"))
    store.upsert_ticket(TicketRow("p", "1", "b", "open", "shaped", ["y"], "u", "github", "t2"))
    [t] = store.list_tickets("p")
    assert (t.title, t.labels, t.flow_state) == ("b", ["y"], "shaped")
    store.upsert_project(Project("A", "o/a", data={"packs": ["x"]}))
    store.upsert_project(Project("A", "o/a2"))
    assert [p.repo for p in store.list_projects()] == ["o/a2"]
    assert store.latest_brief() is None
    store.add_brief("one", project="p")
    store.add_brief("two")
    assert store.latest_brief().markdown == "two"
    assert store.latest_brief("p").markdown == "one"
    rec = store.add_token("ci", "viewer", "h" * 64)
    assert [t.id for t in store.list_tokens()] == [rec.id]
    assert store.revoke_token(rec.id) and not store.revoke_token(rec.id)
    assert store.list_tokens() == [] and store.list_tokens(include_revoked=True)[0].revoked


def test_audit_is_append_only_in_the_database(store: SQLiteStore) -> None:
    store.append_audit("a", "x", "s", {"n": 1})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with store._tx() as cur:
            cur.execute("UPDATE audit_events SET actor = 'mallory'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with store._tx() as cur:
            cur.execute("DELETE FROM audit_events")
    assert list(store.iter_audit())[0].actor == "a"


def test_iter_audit_filters(store: SQLiteStore) -> None:
    for i in range(5):
        store.append_audit("a", "x", f"s{i}")
    assert [e.seq for e in store.iter_audit(since_seq=3)] == [4, 5]
    assert [e.seq for e in store.iter_audit(limit=2)] == [1, 2]
    assert len(list(store.iter_audit(since_ts="2999-01-01T00:00:00.000000Z"))) == 0


def test_open_store_urls(tmp_path: Any) -> None:
    s = open_store(f"sqlite:///{tmp_path}/a/b.db")
    assert s.schema_version() == 3 and (tmp_path / "a" / "b.db").exists()
    assert open_store("sqlite://:memory:").ping()
    assert open_store("sqlite:///:memory:", migrate=False).schema_version() == 0
    with pytest.raises(ValueError, match="unsupported"):
        open_store("mysql://u:secret@h/db")
    assert redact_url("postgresql://u:secret@h:5432/db") == "postgresql://u:***@h:5432/db"
    assert redact_url("sqlite:///x.db") == "sqlite:///x.db"


def test_closed_store_reports_unhealthy(store: SQLiteStore) -> None:
    store.close()
    assert store.ping() is False
    assert store.schema_version() == 0


def test_ts_roundtrip() -> None:
    now = utcnow()
    assert parse_ts(ts(now)) == now
    assert parse_ts("2026-01-01T00:00:00").tzinfo is not None
