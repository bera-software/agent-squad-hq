"""Hash-chained, append-only audit events.

Every event is ``{id, seq, ts, actor, action, subject, data, prev_hash, hash}``
where::

    hash = sha256(prev_hash + canonical_json({id, seq, ts, actor, action, subject, data}))

``canonical_json`` sorts keys, uses compact separators and UTF-8, so the hash
is reproducible in any language. The first event chains from
:data:`GENESIS_HASH`. Changing, deleting, inserting or reordering any event
breaks every later hash, which :func:`verify_chain` reports.

This module is pure (no I/O); storage lives in :mod:`squadhq.store`.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from typing import IO, Any

GENESIS_HASH = "0" * 64

__all__ = [
    "GENESIS_HASH",
    "AuditEvent",
    "VerifyResult",
    "canonical_json",
    "compute_hash",
    "verify_chain",
    "export_jsonl",
    "read_jsonl",
]


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, non-ASCII kept as UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class AuditEvent:
    id: str
    seq: int
    ts: str  # ISO-8601 UTC
    actor: str
    action: str
    subject: str
    data: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def body(self) -> dict[str, Any]:
        """The hashed part of the event (everything but the two hash fields)."""
        return {
            "id": self.id,
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "data": self.data,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuditEvent:
        return cls(
            id=str(d["id"]),
            seq=int(d["seq"]),
            ts=str(d["ts"]),
            actor=str(d["actor"]),
            action=str(d["action"]),
            subject=str(d.get("subject", "")),
            data=dict(d.get("data") or {}),
            prev_hash=str(d.get("prev_hash", GENESIS_HASH)),
            hash=str(d.get("hash", "")),
        )


def compute_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()


def seal(
    *, id: str, seq: int, ts: str, actor: str, action: str, subject: str,
    data: dict[str, Any], prev_hash: str,
) -> AuditEvent:
    """Build an event and compute its hash."""
    unsealed = AuditEvent(id=id, seq=seq, ts=ts, actor=actor, action=action, subject=subject,
                          data=data, prev_hash=prev_hash)
    return AuditEvent(**{**unsealed.to_dict(), "hash": compute_hash(prev_hash, unsealed.body())})


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    reason: str = ""
    head_hash: str = GENESIS_HASH

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_chain(events: Iterable[AuditEvent], *, start_hash: str = GENESIS_HASH,
                 start_seq: int = 1) -> VerifyResult:
    """Check that ``events`` (ordered by ``seq``) form an unbroken chain.

    ``start_hash``/``start_seq`` let callers verify a suffix whose predecessor
    hash is known (e.g. an anchored checkpoint).
    """
    prev = start_hash
    expected_seq = start_seq
    n = 0
    for ev in events:
        if ev.seq != expected_seq:
            return VerifyResult(False, n, ev.seq, f"sequence gap: expected {expected_seq}, got {ev.seq}", prev)
        if ev.prev_hash != prev:
            return VerifyResult(False, n, ev.seq, "prev_hash does not match the previous event's hash", prev)
        if compute_hash(prev, ev.body()) != ev.hash:
            return VerifyResult(False, n, ev.seq, "hash mismatch: event content was modified", prev)
        prev = ev.hash
        expected_seq += 1
        n += 1
    return VerifyResult(True, n, None, "", prev)


def export_jsonl(events: Iterable[AuditEvent], out: IO[str]) -> int:
    """Write one canonical JSON object per line. Returns the number written."""
    n = 0
    for ev in events:
        out.write(canonical_json(ev.to_dict()) + "\n")
        n += 1
    return n


def read_jsonl(lines: Iterable[str]) -> Iterator[AuditEvent]:
    for line in lines:
        line = line.strip()
        if line:
            yield AuditEvent.from_dict(json.loads(line))
