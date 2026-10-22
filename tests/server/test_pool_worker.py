"""Account pool in worker mode: per-job selection from env secrets, per-account
breaker in the store, failover and the /api/v1/capacity view."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from squadhq.interfaces import ErrorClass, ExecutionResult
from squadhq.server import accounts as pool_mod
from squadhq.server.sandbox import ContainerOutcome, FakeContainerRunner
from tests.server.conftest import VIEWER, Rig, auth, make_rig, make_settings

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ENV = {"CLAUDE_CODE_OAUTH_TOKEN_1": "tok-one-secret", "CLAUDE_CODE_OAUTH_TOKEN_2": "tok-two-secret",
       "ANTHROPIC_API_KEY": "base-api-key"}


@pytest.fixture(autouse=True)
def _no_hub_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_mod, "_auth_json", lambda: {})


def _rig(store: Any, outcomes: list[ContainerOutcome] | None = None, env: dict[str, str] | None = None) -> Rig:
    r = make_rig(store, make_settings(mode="worker", executor_image="img@sha256:abc"))
    r.worker.clock = lambda: NOW
    r.worker.deps.containers = r.containers = FakeContainerRunner(outcomes)
    r.worker.pool_env = dict(ENV if env is None else env)
    return r


def _capacity() -> ContainerOutcome:
    return ContainerOutcome(exit_code=1, timed_out=False, duration_s=1.0, result=ExecutionResult(
        executor="claude-code", role="implement", model="m", outcome="failure", error_class=ErrorClass.CAPACITY,
        error_message="hit your session limit", capacity_tripped=True))


def _enqueue(store: Any, key: str = "7") -> str:
    return str(store.enqueue("dispatch_coding", {"project": "acme/a", "key": key, "role": "implement"}).job_id)


def test_selects_first_account_and_passes_only_its_credential(store: Any) -> None:
    r = _rig(store)
    _enqueue(store)
    r.worker.run_once()
    cj = r.containers.jobs[0]
    assert cj.env["SQUADHQ_ACCOUNT"] == "sub-1"
    assert cj.secret_env == {"GH_TOKEN": "ghs_scoped", "CLAUDE_CODE_OAUTH_TOKEN": "tok-one-secret"}
    assert "tok-one-secret" not in str(cj.env)
    assert store.list_runs()[0].data["account"] == "sub-1"


def test_capacity_trips_account_and_retry_uses_the_next(store: Any) -> None:
    r = _rig(store, [_capacity()])
    jid = _enqueue(store)
    r.worker.run_once()
    job = store.get_job(jid)
    assert job.status == "queued"  # transient retry, not the whole capacity window
    state, _ = pool_mod.read_state(store)
    assert set(state["accounts"]) == {"sub-1"}
    assert "capacity.account_tripped" in [e.action for e in store.iter_audit()]
    # the retry (attempt 2) lands on the other account
    assert pool_mod.select(store, pool_mod.load_pool(r.worker.pool_env), NOW).id == "sub-2"
    acct, env = r.worker._pool_credentials(job, {"GH_TOKEN": "x"})
    assert acct == "sub-2" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-two-secret"


def test_all_tripped_raises_capacity_or_falls_back(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig(store)
    for a in ("sub-1", "sub-2"):
        pool_mod.trip(store, a, NOW)
    jid = _enqueue(store)
    r.worker.run_once()
    assert store.get_job(jid).status == "queued" and r.containers.jobs == []  # CapacityError: retried later
    # with fallback_on_capacity_trip the job runs on the metered single key
    monkeypatch.setattr(pool_mod, "_auth_json", lambda: {"fallback_on_capacity_trip": True})
    acct, env = r.worker._pool_credentials(store.get_job(jid), {"GH_TOKEN": "g", "ANTHROPIC_API_KEY": "base"})
    assert acct == "" and env == {"GH_TOKEN": "g", "ANTHROPIC_API_KEY": "base"}


def test_last_account_trip_is_a_capacity_error(store: Any) -> None:
    r = _rig(store, [_capacity()], env={"CLAUDE_CODE_OAUTH_TOKEN_1": "only-one-token"})
    jid = _enqueue(store)
    r.worker.run_once()
    job = store.get_job(jid)
    assert job.status == "queued" and "every pool account is tripped" in (job.last_error or "")


def test_expired_trip_recovers(store: Any) -> None:
    pool_mod.trip(store, "sub-1", NOW - timedelta(hours=2))
    pool = pool_mod.load_pool(ENV)
    assert pool_mod.select(store, pool, NOW).id == "sub-1"


def test_no_pool_keeps_minted_credentials(store: Any) -> None:
    r = _rig(store, env={})
    _enqueue(store)
    r.worker.run_once()
    cj = r.containers.jobs[0]
    assert cj.secret_env == {"GH_TOKEN": "ghs_scoped"} and "SQUADHQ_ACCOUNT" not in cj.env


def test_auth_json_pool_and_missing_secret_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_mod, "_auth_json", lambda: {"pool": [
        {"id": "org-a", "auth": "api", "secret": "ORG_A_KEY"}, {"id": "org-b", "auth": "api", "secret": "ORG_B_KEY"}],
        "pool_strategy": "round_robin"})
    pool = pool_mod.load_pool({"ORG_B_KEY": "k"})
    assert pool.source == "auth.json" and pool.strategy == "round_robin"
    assert [a.id for a in pool.accounts] == ["org-b"]


def test_trip_cas_retries_on_conflict(store: Any) -> None:
    from squadhq.store.base import ConflictError

    real = store.kv_cas
    calls = {"n": 0}

    def flaky(*a: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConflictError("race")
        return int(real(*a))

    store.kv_cas = flaky
    state = pool_mod.trip(store, "sub-1", NOW)
    assert "sub-1" in state["accounts"] and calls["n"] == 2


def test_capacity_api(client: Any, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    pool_mod.trip(store, "sub-1", datetime.now(timezone.utc))
    r = client.get("/api/v1/capacity", headers=auth(VIEWER))
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] and body["source"] == "env" and body["available"] == 1
    assert [(a["id"], a["state"]) for a in body["accounts"]] == [("sub-1", "tripped"), ("sub-2", "available")]
    assert "tok-one-secret" not in r.text and "tok-two-secret" not in r.text
    assert client.get("/api/v1/capacity").status_code == 401
