"""Append-only, hash-chained audit log (see :mod:`squadhq.audit.chain`).

Storage is provided by :class:`squadhq.store.StateStore`; use
``store.append_audit(...)`` to write and :func:`verify_store` to check.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from squadhq.audit.chain import (
    GENESIS_HASH,
    AuditEvent,
    VerifyResult,
    canonical_json,
    compute_hash,
    export_jsonl,
    read_jsonl,
    verify_chain,
)

if TYPE_CHECKING:  # pragma: no cover
    from squadhq.store.base import StateStore

__all__ = [
    "GENESIS_HASH",
    "AuditEvent",
    "VerifyResult",
    "canonical_json",
    "compute_hash",
    "export_jsonl",
    "read_jsonl",
    "verify_chain",
    "verify_store",
]


def verify_store(store: StateStore) -> VerifyResult:
    """Verify the whole chain held by ``store``."""
    return verify_chain(store.iter_audit())
