"""Redact credentials from text before it leaves the runner.

Anything posted to an issue, a comment, a step summary, a log line or an
API response that may contain error output or log tails goes through
:func:`redact` first.

API::

    redact(text, *, env=None, extra_values=()) -> str
    find_secrets(text, env=None) -> list[str]   # kinds found, never the values
    secret_env_values(env=None) -> list[str]
    REDACTED  # the replacement marker, "[REDACTED]"

CLI (pre-push hook, CI)::

    python -m squadhq.redact check [FILE ...]   # stdin when no FILE; exit 1 + kinds on stderr if any
    python -m squadhq.redact filter             # stdin -> stdout, redacted

Two layers:

1. Known token shapes: GitHub tokens (``ghp_ ghs_ gho_ ghu_ ghr_
   github_pat_``), Anthropic ``sk-ant-…``, OpenAI-style ``sk-…``, AWS access
   key ids ``AKIA…``/``ASIA…``, Slack ``xox[abpr]-…``, ``Bearer <token>``
   and ``Authorization: token|Basic …`` headers, PEM private-key blocks, and
   credentials embedded in URLs (``https://user:pass@host``).
2. The exact values of environment variables whose NAME contains KEY,
   TOKEN, SECRET, PASSWORD or PRIVATE (values shorter than 8 characters are skipped —
   they would redact ordinary words).
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence

REDACTED = "[REDACTED]"

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE", re.IGNORECASE)
_MIN_VALUE_LEN = 8

_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                               re.S), REDACTED),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), REDACTED),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), REDACTED),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),
    ("openai-style-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}"), REDACTED),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    ("bearer-token", re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + REDACTED),
    ("authorization-header", re.compile(r"(?i)(authorization:\s*(?:token|basic))\s+[A-Za-z0-9._~+/=\-]{8,}"),
     r"\1 " + REDACTED),
    ("url-credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1" + REDACTED + "@"),
)


def secret_env_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Values of env vars whose name looks secret, longest first."""
    env = os.environ if env is None else env
    vals = {v for k, v in env.items() if _SECRET_NAME.search(k) and v and len(v) >= _MIN_VALUE_LEN}
    return sorted(vals, key=lambda v: (-len(v), v))


def redact(text: str | None, *, env: Mapping[str, str] | None = None, extra_values: Iterable[str] = ()) -> str:
    """``text`` with every recognised credential replaced by ``[REDACTED]``."""
    if not text:
        return ""
    out = text
    values = secret_env_values(env) + sorted({v for v in extra_values if v and len(v) >= _MIN_VALUE_LEN},
                                            key=len, reverse=True)
    for v in values:
        out = out.replace(v, REDACTED)
    for _kind, rx, repl in _PATTERNS:
        out = rx.sub(repl, out)
    return out


def find_secrets(text: str | None, env: Mapping[str, str] | None = None) -> list[str]:
    """Kinds of credentials present in ``text`` (e.g. ``["github-token",
    "env:GH_TOKEN"]``). Never returns the secret itself — safe to print."""
    if not text:
        return []
    env = os.environ if env is None else env
    found = [kind for kind, rx, _ in _PATTERNS if rx.search(text)]
    for k, v in sorted(env.items()):
        if _SECRET_NAME.search(k) and v and len(v) >= _MIN_VALUE_LEN and v in text:
            found.append(f"env:{k}")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help") or args[0] not in ("check", "filter"):
        print("usage: python -m squadhq.redact {check [FILE ...] | filter}", file=sys.stderr)
        return 0 if args and args[0] in ("-h", "--help") else 2
    if args[0] == "filter":
        sys.stdout.write(redact(sys.stdin.read()))
        return 0
    sources = args[1:] or ["-"]
    bad = 0
    for src in sources:
        try:
            text = sys.stdin.read() if src == "-" else open(src, errors="replace").read()
        except OSError as e:
            print(f"redact: cannot read {src}: {e}", file=sys.stderr)
            return 2
        kinds = find_secrets(text)
        if kinds:
            bad += 1
            print(f"redact: {src}: possible secret(s): {', '.join(kinds)}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
