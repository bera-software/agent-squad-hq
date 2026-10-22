"""Account pool in server (worker) mode.

The pool comes from the hub's rendered ``config/auth.json`` (``pool``, from
``providers.anthropic.pool``) when it has one; otherwise it is discovered from
the worker's environment: ``ANTHROPIC_API_KEY_1..5`` (ids ``api-<n>``) and
``CLAUDE_CODE_OAUTH_TOKEN_1..5`` (ids ``sub-<n>``). Unlike Actions mode the
worker can see which secrets are set, so an account whose secret is missing is
never selected.

Per-account breakers live in the store (kv ``capacity/accounts``, same shape as
``state/capacity.json``: ``{"accounts": {id: {tripped_until, ...}}}``), so every
worker replica shares them. Writes use compare-and-set.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from squadhq import account_pool as AP
from squadhq.store.base import ConflictError, StateStore

log = logging.getLogger("squadhq.accounts")

KV_NS = "capacity"
KV_KEY = "accounts"


@dataclass
class Pool:
    accounts: list[AP.Account]
    strategy: str = "ordered"
    fallback: bool = False  # fallback_on_capacity_trip: metered ANTHROPIC_API_KEY when all are tripped
    source: str = "none"  # auth.json | env | none

    @property
    def configured(self) -> bool:
        return bool(self.accounts)


def _auth_json() -> dict[str, Any]:
    from squadhq.paths import hub_path

    try:
        with open(hub_path("config", "auth.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_pool(env: Mapping[str, str] | None = None, auth: Mapping[str, Any] | None = None) -> Pool:
    env = os.environ if env is None else env
    auth = _auth_json() if auth is None else auth
    fallback = auth.get("fallback_on_capacity_trip") is True
    accounts, strategy = AP.parse_pool(auth)
    source = "auth.json"
    if not accounts:
        accounts, source = AP.env_pool(env), "env"
        strategy = str(env.get("SQUADHQ_POOL_STRATEGY") or strategy)
        if strategy not in AP.STRATEGIES:
            strategy = "ordered"
    if not accounts:
        source = "none"
    # Only accounts whose credential is actually present can run a job.
    usable = [a for a in accounts if env.get(a.secret)]
    for a in accounts:
        if a not in usable:
            log.warning("pool account has no credential in the environment", extra={"account": a.id,
                                                                                    "secret": a.secret})
    return Pool(usable, strategy, fallback, source)


def read_state(store: StateStore) -> tuple[dict[str, Any], int | None]:
    got = store.kv_get(KV_NS, KV_KEY)
    if got is None:
        return {}, None
    value, version = got
    return (value if isinstance(value, dict) else {}), version


def select(store: StateStore, pool: Pool, now: datetime, seed: int = 0) -> AP.Account | None:
    state, _ = read_state(store)
    return AP.select(pool.accounts, state, now, pool.strategy, seed)


def trip(store: StateStore, account_id: str, now: datetime, by: str = "") -> dict[str, Any]:
    """Trip one account (CAS, a few retries). Returns the state written (or last read)."""
    state: dict[str, Any] = {}
    for _ in range(5):
        state, version = read_state(store)
        new = AP.trip(state, account_id, now, by)
        try:
            store.kv_cas(KV_NS, KV_KEY, new, version)
            return new
        except ConflictError:
            continue
    log.warning("could not record a pool account trip (contention)", extra={"account": account_id})
    return state


def status(store: StateStore, pool: Pool, now: datetime) -> dict[str, Any]:
    """Public pool status (ids, auth, secret names, breaker state — never a secret value)."""
    state, _ = read_state(store)
    accounts = AP.status(pool.accounts, state, now)
    until = AP.earliest_recovery(pool.accounts, state, now)
    return {"configured": pool.configured, "source": pool.source, "strategy": pool.strategy,
            "fallback_on_capacity_trip": pool.fallback, "accounts": accounts,
            "available": sum(1 for a in accounts if a["state"] == "available"),
            "all_tripped_until": until.isoformat() if until else ""}
