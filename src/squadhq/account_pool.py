"""Coding-agent account pool: several LLM credentials behind one breaker.

``providers.anthropic.pool`` in ``squadhq.yml`` (rendered to
``config/auth.json`` ``pool``) lists up to :data:`POOL_MAX` accounts, each
``{id, auth, secret}``. Every account has its own capacity breaker in the
capacity state (``state/capacity.json`` in Actions mode, the store's
``capacity`` namespace in server mode)::

    {"accounts": {"<id>": {"tripped_until": "<iso>", "tripped_at": "<iso>", "tripped_by": "<run url>"}}}

:func:`select` returns the account the next job uses: the first untripped
one in list order (``ordered``), or the first untripped one starting from a
rotating offset (``round_robin``; the offset is a stable hash of the run, so
selection needs no state write). An expired ``tripped_until`` simply makes
the account available again. Only when :func:`select` returns ``None`` does
the existing fleet behaviour apply (pause, or metered fallback when
``fallback_on_capacity_trip`` is on).

Pure functions over plain dicts: no I/O, no secrets. Account ids and secret
*names* are not sensitive and may appear in summaries; secret values never
pass through this module except :func:`credential_from_env` (server mode),
which only reads the environment.
"""
from __future__ import annotations

import datetime
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

POOL_MAX = 5
STRATEGIES = ("ordered", "round_robin")
AUTHS = ("api", "subscription")
TRIP_MINUTES = 60
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,33}$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# The env variable the executor reads for each auth mode.
EXECUTOR_ENV = {"api": "ANTHROPIC_API_KEY", "subscription": "CLAUDE_CODE_OAUTH_TOKEN"}
# GitHub reusable workflows must declare every secret they receive, so Actions
# mode can only reach these names: the single credential plus five pool slots
# per auth mode. Server mode reads any environment variable.
ACTIONS_SLOTS: dict[str, tuple[str, ...]] = {
    auth: (base, *(f"{base}_{n}" for n in range(1, POOL_MAX + 1))) for auth, base in EXECUTOR_ENV.items()
}


@dataclass(frozen=True)
class Account:
    id: str
    auth: str  # api | subscription
    secret: str  # secret / environment variable NAME holding the credential

    def public(self) -> dict[str, str]:
        return {"id": self.id, "auth": self.auth, "secret": self.secret}


def default_secret(auth: str, n: int) -> str:
    """Default secret name of the ``n``-th (1-based) pool entry."""
    return f"{EXECUTOR_ENV.get(auth, EXECUTOR_ENV['api'])}_{n}"


def actions_slot_ok(acct: Account) -> bool:
    """True when Actions workflows can pass this account's secret to the agent."""
    return acct.secret in ACTIONS_SLOTS.get(acct.auth, ())


def parse_pool(auth_json: Mapping[str, Any] | None) -> tuple[list[Account], str]:
    """``(accounts, strategy)`` from a rendered ``config/auth.json``.

    Tolerant: malformed entries are dropped (the loader already rejected them
    in ``squadhq.yml``; a hand-edited file must not crash a resolver). An
    absent or empty pool returns ``([], "ordered")``.
    """
    raw = (auth_json or {}).get("pool")
    strategy = str((auth_json or {}).get("pool_strategy") or "ordered")
    if strategy not in STRATEGIES:
        strategy = "ordered"
    out: list[Account] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out, strategy
    for i, e in enumerate(raw[:POOL_MAX], start=1):
        if not isinstance(e, Mapping):
            continue
        aid, auth = str(e.get("id") or ""), str(e.get("auth") or "subscription")
        secret = str(e.get("secret") or default_secret(auth, i))
        if not ID_RE.match(aid) or aid in seen or auth not in AUTHS or not SECRET_RE.match(secret):
            continue
        seen.add(aid)
        out.append(Account(aid, auth, secret))
    return out, strategy


def parse_ts(value: Any) -> datetime.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc)


def _accounts_state(state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    acc = (state or {}).get("accounts")
    return acc if isinstance(acc, Mapping) else {}


def tripped_until(state: Mapping[str, Any] | None, account_id: str,
                  now: datetime.datetime) -> datetime.datetime | None:
    """When the account's breaker re-closes, or None when it is closed now."""
    entry = _accounts_state(state).get(account_id)
    until = parse_ts(entry.get("tripped_until")) if isinstance(entry, Mapping) else None
    return until if until and until > now else None


def available(accounts: list[Account], state: Mapping[str, Any] | None,
              now: datetime.datetime) -> list[Account]:
    return [a for a in accounts if tripped_until(state, a.id, now) is None]


def rotation_seed(*parts: object) -> int:
    """Stable, non-cryptographic seed for ``round_robin`` (e.g. run id, attempt, job)."""
    digest = hashlib.blake2b(":".join(str(p) for p in parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def select(accounts: list[Account], state: Mapping[str, Any] | None, now: datetime.datetime,
           strategy: str = "ordered", seed: int = 0) -> Account | None:
    """The account the next job uses, or None when every account is tripped."""
    if not accounts:
        return None
    start = seed % len(accounts) if strategy == "round_robin" else 0
    for i in range(len(accounts)):
        a = accounts[(start + i) % len(accounts)]
        if tripped_until(state, a.id, now) is None:
            return a
    return None


def earliest_recovery(accounts: list[Account], state: Mapping[str, Any] | None,
                      now: datetime.datetime) -> datetime.datetime | None:
    """When the first tripped account becomes available again (None: one is available now)."""
    times = [tripped_until(state, a.id, now) for a in accounts]
    if not accounts or any(t is None for t in times):
        return None
    return min(t for t in times if t is not None)


def trip(state: Mapping[str, Any] | None, account_id: str, now: datetime.datetime, by: str = "",
         minutes: int = TRIP_MINUTES) -> dict[str, Any]:
    """A copy of ``state`` with ``account_id`` tripped until ``now + minutes``.

    A later existing ``tripped_until`` is kept (never shortens a window).
    """
    new: dict[str, Any] = dict(state or {})
    accounts = {k: dict(v) for k, v in _accounts_state(state).items() if isinstance(v, Mapping)}
    until = now + datetime.timedelta(minutes=minutes)
    cur = parse_ts((accounts.get(account_id) or {}).get("tripped_until"))
    if cur is None or cur < until:
        accounts[account_id] = {"tripped_until": until.isoformat(), "tripped_at": now.isoformat(), "tripped_by": by}
    new["accounts"] = accounts
    history = list(new.get("history") or []) + [{"at": now.isoformat(), "action": "trip", "by": by,
                                                 "account": account_id}]
    new["history"] = history[-20:]
    return new


def status(accounts: list[Account], state: Mapping[str, Any] | None,
           now: datetime.datetime) -> list[dict[str, Any]]:
    """Per-account breaker status (no secret values) for summaries, doctor and the API."""
    out = []
    for a in accounts:
        until = tripped_until(state, a.id, now)
        entry = _accounts_state(state).get(a.id)
        out.append({**a.public(), "state": "tripped" if until else "available",
                    "tripped_until": until.isoformat() if until else "",
                    "tripped_at": str(entry.get("tripped_at", "")) if isinstance(entry, Mapping) and until else ""})
    return out


def env_pool(env: Mapping[str, str]) -> list[Account]:
    """Server mode without a configured pool: discover ``ANTHROPIC_API_KEY_n`` /
    ``CLAUDE_CODE_OAUTH_TOKEN_n`` (n = 1..5) from the environment, API keys first.
    Ids are ``api-<n>`` / ``sub-<n>``. An empty list means no pool."""
    out: list[Account] = []
    for auth, prefix in (("api", "api"), ("subscription", "sub")):
        for n in range(1, POOL_MAX + 1):
            name = default_secret(auth, n)
            if env.get(name):
                out.append(Account(f"{prefix}-{n}", auth, name))
    return out[:POOL_MAX]


def credential_from_env(acct: Account, env: Mapping[str, str]) -> dict[str, str]:
    """``{executor env name: value}`` for the selected account, or ``{}`` when its secret is unset."""
    value = env.get(acct.secret, "")
    return {EXECUTOR_ENV[acct.auth]: value} if value else {}
