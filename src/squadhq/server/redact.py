"""Server-side redaction: :func:`squadhq.redact.redact` plus server-specific shapes.

The core module covers GitHub/Anthropic/OpenAI/AWS/Slack tokens, bearer and
authorization headers, PEM keys, URL credentials and the values of
secret-named env vars. On top of that the server scrubs its own API tokens
(``sqh_…``), Expo push tokens and the values of known server secrets (the
GitHub token, webhook secrets, the database URL).
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

from squadhq.redact import REDACTED
from squadhq.redact import redact as _core_redact

__all__ = ["REDACTED", "redact", "secret_values"]

_SERVER_PATTERNS = re.compile(r"sqh_[A-Za-z0-9_\-]{16,}|(?:ExponentPushToken|ExpoPushToken)\[[^\]]+\]")
_EXTRA_NAMES = ("SQUADHQ_DB_URL",)


def secret_values(env: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> list[str]:
    """Known secret values not caught by name by the core module (>= 8 chars), longest first."""
    e = os.environ if env is None else env
    vals = {e[n] for n in _EXTRA_NAMES if len(e.get(n, "")) >= 8}
    vals.update(v for v in extra if v and len(v) >= 8)
    return sorted(vals, key=len, reverse=True)


def redact(text: str, known: Iterable[str] = ()) -> str:
    if not text:
        return text
    return _SERVER_PATTERNS.sub(REDACTED, _core_redact(text, extra_values=list(known)))
