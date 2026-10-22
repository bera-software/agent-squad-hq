"""GitHub App installation-token minter (stdlib only: RS256 JWT with Python big ints).

Implements the server's ``TokenMinter`` protocol (``mint(repo, role) ->
list[ScopedToken]``): every executor job gets a short-lived installation
token scoped to **one repository** with the **least permissions** its role
needs, plus pass-through LLM keys.

Env:

* ``SQUADHQ_APP_ID`` (numeric App id) or ``SQUADHQ_APP_CLIENT_ID`` — the JWT ``iss``
* ``SQUADHQ_APP_PRIVATE_KEY`` (PEM text) or ``SQUADHQ_APP_PRIVATE_KEY_FILE`` (path);
  PKCS#1 (``BEGIN RSA PRIVATE KEY``) and unencrypted PKCS#8 (``BEGIN PRIVATE KEY``)
* ``GITHUB_API_URL`` (default ``https://api.github.com``)
* ``SQUADHQ_APP_PERMISSIONS_<ROLE>`` — optional JSON override of a role's permissions

Flow: App JWT -> ``GET /orgs/{owner}/installation`` (fallback
``/users/{owner}/installation``; id cached per owner) -> ``POST
/app/installations/{id}/access_tokens`` with ``repositories: [name]`` and the
role's ``permissions`` -> token cached per (repo, permissions) until 5 minutes
before ``expires_at``.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from squadhq import http
from squadhq.http import Transport
from squadhq.interfaces import FatalError

log = logging.getLogger(__name__)

__all__ = [
    "RSAPrivateKey",
    "load_private_key",
    "rs256_sign",
    "app_jwt",
    "GitHubAppTokenMinter",
    "ROLE_PERMISSIONS",
    "permissions_for",
    "select_token_minter",
]

# --------------------------------------------------------------------------
# Minimal DER / PEM parsing (only what RSA private keys need)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RSAPrivateKey:
    n: int
    e: int
    d: int
    p: int
    q: int
    dp: int
    dq: int
    qinv: int

    @property
    def size_bytes(self) -> int:
        return (self.n.bit_length() + 7) // 8


def _der_read(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """Read one TLV at ``pos``; return ``(tag, value, next_pos)``."""
    if pos + 2 > len(buf):
        raise ValueError("DER: truncated")
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        nbytes = length & 0x7F
        if nbytes == 0 or nbytes > 4 or pos + nbytes > len(buf):
            raise ValueError("DER: bad length")
        length = int.from_bytes(buf[pos:pos + nbytes], "big")
        pos += nbytes
    end = pos + length
    if end > len(buf):
        raise ValueError("DER: truncated value")
    return tag, buf[pos:end], end


def _der_children(seq: bytes) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(seq):
        tag, val, pos = _der_read(seq, pos)
        out.append((tag, val))
    return out


_SEQ, _INT, _OCTET = 0x30, 0x02, 0x04
_RSA_OID = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1


def _parse_pkcs1(der: bytes) -> RSAPrivateKey:
    tag, body, _ = _der_read(der, 0)
    if tag != _SEQ:
        raise ValueError("PKCS#1: expected SEQUENCE")
    ints = [int.from_bytes(v, "big", signed=True) for t, v in _der_children(body) if t == _INT]
    if len(ints) < 9 or ints[0] != 0:
        raise ValueError("PKCS#1: not a two-prime RSAPrivateKey")
    return RSAPrivateKey(*ints[1:9])


def _parse_pkcs8(der: bytes) -> RSAPrivateKey:
    tag, body, _ = _der_read(der, 0)
    if tag != _SEQ:
        raise ValueError("PKCS#8: expected SEQUENCE")
    kids = _der_children(body)
    if len(kids) < 3 or kids[1][0] != _SEQ or kids[2][0] != _OCTET:
        raise ValueError("PKCS#8: unexpected structure")
    alg = _der_children(kids[1][1])
    if not alg or alg[0][1] != _RSA_OID:
        raise ValueError("PKCS#8: not an RSA key")
    return _parse_pkcs1(kids[2][1])


_PEM_RE = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)


def load_private_key(pem: str | bytes) -> RSAPrivateKey:
    """Parse an unencrypted RSA private key PEM (PKCS#1 or PKCS#8)."""
    text = pem.decode() if isinstance(pem, bytes) else pem
    text = text.replace("\\n", "\n")  # tolerate keys pasted into a single-line env var
    m = _PEM_RE.search(text)
    if not m:
        raise ValueError("not a PEM private key")
    kind = m.group(1)
    der = base64.b64decode("".join(m.group(2).split()))
    if kind == "RSA PRIVATE KEY":
        key = _parse_pkcs1(der)
    elif kind == "PRIVATE KEY":
        key = _parse_pkcs8(der)
    else:
        raise ValueError(f"unsupported PEM type {kind!r} (encrypted keys are not supported)")
    if key.p * key.q != key.n:
        raise ValueError("RSA key is inconsistent (p*q != n)")
    return key


# --------------------------------------------------------------------------
# RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 §8.2) and the App JWT
# --------------------------------------------------------------------------

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256_sign(key: RSAPrivateKey, message: bytes) -> bytes:
    k = key.size_bytes
    t = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise ValueError("RSA key too short")
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    m = int.from_bytes(em, "big")
    # CRT exponentiation (about 4x faster than pow(m, d, n)); verified below.
    s1 = pow(m, key.dp, key.p)
    s2 = pow(m, key.dq, key.q)
    h = (key.qinv * (s1 - s2)) % key.p
    s = s2 + h * key.q
    if pow(s, key.e, key.n) != m:  # fault check: never emit a bad signature
        raise ValueError("RSA signature self-check failed")
    return s.to_bytes(k, "big")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def app_jwt(app_id: str, key: RSAPrivateKey, *, now: float | None = None, ttl_s: int = 540) -> str:
    """GitHub App JWT: ``iat`` backdated 60 s for clock drift, ``exp`` <= 10 minutes."""
    t = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": t - 60, "exp": t + min(ttl_s, 600 - 60), "iss": app_id}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    return signing_input + "." + _b64url(rs256_sign(key, signing_input.encode()))


# --------------------------------------------------------------------------
# Least-privilege permissions per role
# --------------------------------------------------------------------------

_READ = {"metadata": "read", "contents": "read"}
ROLE_PERMISSIONS: dict[str, dict[str, str]] = {
    # Writes code on an agent/* branch and opens a PR (never merges).
    "implement": {**_READ, "contents": "write", "pull_requests": "write", "issues": "write", "checks": "read"},
    "review-fix": {**_READ, "contents": "write", "pull_requests": "write", "issues": "write", "checks": "read"},
    "design": {**_READ, "contents": "write", "pull_requests": "write", "issues": "write"},
    # Comments / reviews only.
    "review": {**_READ, "pull_requests": "write", "issues": "write", "checks": "read"},
    "mention": {**_READ, "pull_requests": "write", "issues": "write"},
    "po-shape": {**_READ, "issues": "write"},
    "po-continue": {**_READ, "issues": "write"},
    "concierge": {**_READ, "issues": "write"},
    "scan": {**_READ, "issues": "write"},
}
DEFAULT_PERMISSIONS: dict[str, str] = {**_READ, "issues": "write"}


def permissions_for(role: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Role permissions; ``SQUADHQ_APP_PERMISSIONS_<ROLE>`` (JSON object) overrides."""
    env = os.environ if env is None else env
    override = env.get("SQUADHQ_APP_PERMISSIONS_" + re.sub(r"[^A-Za-z0-9]", "_", role).upper())
    if override:
        try:
            data = json.loads(override)
        except ValueError as e:
            raise FatalError(f"SQUADHQ_APP_PERMISSIONS_* for {role!r} is not JSON: {e}") from None
        if not isinstance(data, dict):
            raise FatalError(f"SQUADHQ_APP_PERMISSIONS_* for {role!r} must be a JSON object")
        return {str(k): str(v) for k, v in data.items()}
    return dict(ROLE_PERMISSIONS.get(role, DEFAULT_PERMISSIONS))


# --------------------------------------------------------------------------
# Minter
# --------------------------------------------------------------------------


def _parse_iso(s: str) -> float:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class GitHubAppTokenMinter:
    """``TokenMinter`` that mints per-repo, least-privilege installation tokens."""

    REFRESH_MARGIN_S = 300.0

    def __init__(
        self,
        *,
        app_id: str | None = None,
        private_key_pem: str | None = None,
        api_url: str | None = None,
        env: Mapping[str, str] | None = None,
        passthrough: tuple[str, ...] = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.env = os.environ if env is None else env
        self.app_id = app_id or self.env.get("SQUADHQ_APP_ID") or self.env.get("SQUADHQ_APP_CLIENT_ID") or ""
        if not self.app_id:
            raise FatalError("GitHub App minter: set SQUADHQ_APP_ID or SQUADHQ_APP_CLIENT_ID")
        pem = private_key_pem or self.env.get("SQUADHQ_APP_PRIVATE_KEY")
        if not pem and (path := self.env.get("SQUADHQ_APP_PRIVATE_KEY_FILE")):
            with open(path, encoding="utf-8") as f:
                pem = f.read()
        if not pem:
            raise FatalError("GitHub App minter: set SQUADHQ_APP_PRIVATE_KEY or SQUADHQ_APP_PRIVATE_KEY_FILE")
        try:
            self.key = load_private_key(pem)
        except ValueError as e:
            raise FatalError(f"GitHub App minter: bad private key: {e}") from None
        self.api_url = (api_url or self.env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.passthrough = passthrough
        self.transport = transport
        self.clock = clock
        self._installations: dict[str, int] = {}
        self._tokens: dict[tuple[str, str], tuple[str, float, str]] = {}
        self._lock = threading.Lock()

    def _headers(self, bearer: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def jwt(self) -> str:
        return app_jwt(self.app_id, self.key, now=self.clock())

    def installation_id(self, owner: str) -> int:
        if owner in self._installations:
            return self._installations[owner]
        hdrs = self._headers(self.jwt())
        for path in (f"/orgs/{owner}/installation", f"/users/{owner}/installation"):
            resp = http.request("GET", self.api_url + path, headers=hdrs, timeout=30.0, retries=2,
                                transport=self.transport, ok_statuses=(404,))
            if resp.status == 404:
                continue
            data: Any = resp.json() or {}
            inst = int(data["id"])
            self._installations[owner] = inst
            return inst
        raise FatalError(f"GitHub App {self.app_id} is not installed for {owner!r}")

    def installation_token(self, repo: str, permissions: Mapping[str, str]) -> tuple[str, str]:
        """``(token, expires_at)`` for ``owner/name``, served from cache while fresh."""
        owner, _, name = repo.partition("/")
        if not owner or not name:
            raise FatalError(f"GitHub App minter: repo must be owner/name, got {repo!r}")
        cache_key = (repo.lower(), json.dumps(dict(permissions), sort_keys=True))
        with self._lock:
            hit = self._tokens.get(cache_key)
            if hit and hit[1] - self.clock() > self.REFRESH_MARGIN_S:
                return hit[0], hit[2]
            inst = self.installation_id(owner)
            data: Any = http.request_json(
                "POST", f"{self.api_url}/app/installations/{inst}/access_tokens",
                headers=self._headers(self.jwt()),
                json_body={"repositories": [name], "permissions": dict(permissions)},
                timeout=30.0, retries=2, transport=self.transport) or {}
            token = str(data.get("token") or "")
            if not token:
                raise FatalError("GitHub App minter: access_tokens response had no token")
            expires_at = str(data.get("expires_at") or "")
            try:
                exp = _parse_iso(expires_at)
            except ValueError:
                exp = self.clock() + 3600.0
            self._tokens[cache_key] = (token, exp, expires_at)
            return token, expires_at

    def mint(self, repo: str, role: str) -> list[Any]:
        """``TokenMinter.mint``: ``GH_TOKEN`` for ``repo`` + pass-through LLM keys.

        A non-GitHub project key (no ``/``) gets no GitHub token.
        """
        from squadhq.server.integrations import ScopedToken

        out: list[Any] = []
        if "/" in repo:
            token, expires_at = self.installation_token(repo, permissions_for(role, self.env))
            out.append(ScopedToken(env_name="GH_TOKEN", value=token, expires_at=expires_at))
        else:
            log.warning("github_app minter: %r is not owner/name; no GitHub token minted", repo)
        for n in self.passthrough:
            v = self.env.get(n)
            if v:
                out.append(ScopedToken(env_name=n, value=v))
        return out


def select_token_minter(env: Mapping[str, str] | None = None, **kwargs: Any) -> Any:
    """Server hook: ``SQUADHQ_TOKEN_MINTER=app`` -> :class:`GitHubAppTokenMinter`, else ``EnvTokenMinter``.

    ``app`` fails fast (``FatalError``) when the App id or key is missing, so
    a misconfigured server never silently runs jobs with broad static tokens.
    """
    env = os.environ if env is None else env
    choice = (env.get("SQUADHQ_TOKEN_MINTER") or "env").strip().lower()
    if choice == "app":
        return GitHubAppTokenMinter(env=env, **kwargs)
    if choice != "env":
        raise FatalError(f"SQUADHQ_TOKEN_MINTER must be 'app' or 'env', got {choice!r}")
    from squadhq.server.integrations import EnvTokenMinter

    return EnvTokenMinter(env=env)
