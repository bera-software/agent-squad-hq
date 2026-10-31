"""What an agent's findings file may make the ops identity do.

Debt-scan and quarterly-review agents write ``.squadhq-out/agent-findings.json``; the
post job creates issues from it with the ops token. The file is agent-written
(untrusted), so:

* labels pass only through :func:`allowed_labels` — an explicit allowlist;
  dispatch, verdict, gate and trust labels can never be applied this way;
* bodies pass through :func:`clean_body`, which drops protocol lines and
  markers the hub trusts only from its own code (``Originated-by:``,
  ``Requested-by:``, ``Requested-by-association:``, ``<!-- squadhq:... -->``)
  and neutralises untrusted-fence closing tags.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from squadhq import untrusted

ALLOWED_EXACT: frozenset[str] = frozenset({"tech-debt", "debt", "debt-proposal"})
ALLOWED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^debt:[a-z0-9][a-z0-9-]{0,40}$"),
    re.compile(r"^severity:(low|medium|high|critical)$"),
    re.compile(r"^area:[a-z0-9][a-z0-9-]{0,40}$"),
    re.compile(r"^dept:[a-z0-9][a-z0-9-]{0,40}$"),
)
# Defence in depth: these never pass, whatever the allowlist above says.
DENIED_PREFIXES: tuple[str, ...] = ("agent:", "risk:", "gate:", "verdict:", "status:", "security:", "fork:")
DENIED_EXACT: frozenset[str] = frozenset({"needs-design", "ready-to-merge", "needs-approver-merge",
                                          "review:changes-requested", "needs-human", "escalation"})

_PROTOCOL_LINE = re.compile(r"^\s*(Originated-by|Requested-by|Requested-by-association)\s*:.*$", re.I | re.M)
_MARKER = re.compile(r"<!--\s*squadhq:.*?-->", re.I | re.S)


def allowed_labels(labels: Iterable[object]) -> list[str]:
    """The labels from an agent finding that the ops identity may apply."""
    out: list[str] = []
    for raw in labels or ():
        if not isinstance(raw, str):
            continue
        name = raw.strip().lower()
        if not name or name in DENIED_EXACT or name.startswith(DENIED_PREFIXES):
            continue
        if name in ALLOWED_EXACT or any(p.match(name) for p in ALLOWED_PATTERNS):
            if name not in out:
                out.append(name)
    return out


def clean_body(text: object, max_chars: int = 6000) -> str:
    """An agent-written issue body, safe for the ops identity to post."""
    body = str(text or "")
    body = _MARKER.sub("", body)
    body = _PROTOCOL_LINE.sub("", body)
    return untrusted.neutralise(body)[:max_chars]


def target_repo(requested: object, *, default: str, allowed: Iterable[object]) -> str:
    """Where a finding becomes an issue: its ``repo`` when that is one of
    ``allowed`` (the hub and the configured projects), else ``default``."""
    if isinstance(requested, str) and requested.strip():
        want = requested.strip().lower()
        for repo in allowed:
            if isinstance(repo, str) and repo.lower() == want:
                return repo
    return default
