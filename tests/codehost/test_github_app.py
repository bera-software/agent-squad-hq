"""GitHub App token minter: RS256 vs an openssl vector, JWT, installation lookup, caching.

Fixture key ``app_test_key_*.pem`` is a throwaway 2048-bit test key generated
with ``openssl genrsa``; ``app_test_sig.b64`` is
``openssl dgst -sha256 -sign app_test_key_pkcs1.pem`` over b"squadhq rs256 vector".
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from squadhq.codehost import github_app as ga
from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError
from squadhq.server.integrations import EnvTokenMinter

FX = Path(__file__).parent / "fixtures"
PKCS1 = (FX / "app_test_key_pkcs1.pem").read_text()
PKCS8 = (FX / "app_test_key_pkcs8.pem").read_text()
SIG = base64.b64decode((FX / "app_test_sig.b64").read_text().strip())
API = "https://api.github.com"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_rs256_matches_openssl_for_pkcs1_and_pkcs8() -> None:
    for pem in (PKCS1, PKCS8, PKCS1.replace("\n", "\\n")):
        key = ga.load_private_key(pem)
        assert ga.rs256_sign(key, b"squadhq rs256 vector") == SIG
    k1, k8 = ga.load_private_key(PKCS1), ga.load_private_key(PKCS8.encode())
    assert k1 == k8 and k1.size_bytes == 256 and k1.e == 65537


@pytest.mark.parametrize("pem", [
    "nope",
    "-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nMAMCAQE=\n-----END RSA PRIVATE KEY-----",  # SEQ{INT 1}
    "-----BEGIN RSA PRIVATE KEY-----\nAgEA\n-----END RSA PRIVATE KEY-----",  # not a SEQUENCE
    "-----BEGIN PRIVATE KEY-----\nAgEA\n-----END PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\nMAMCAQA=\n-----END PRIVATE KEY-----",  # SEQ{INT 0} only
    "-----BEGIN RSA PRIVATE KEY-----\nMIQ=\n-----END RSA PRIVATE KEY-----",  # bad length
    "-----BEGIN RSA PRIVATE KEY-----\nMA==\n-----END RSA PRIVATE KEY-----",  # truncated
    "-----BEGIN RSA PRIVATE KEY-----\nMAU=\n-----END RSA PRIVATE KEY-----",  # truncated value
])
def test_bad_keys_rejected(pem: str) -> None:
    with pytest.raises(ValueError):
        ga.load_private_key(pem)


def test_pkcs8_non_rsa_and_inconsistent_key() -> None:
    # PKCS#8 with an EC OID.
    seq = bytes.fromhex("3010020100300906072a8648ce3d02010400")
    pem = "-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(seq).decode() + "\n-----END PRIVATE KEY-----"
    with pytest.raises(ValueError, match="not an RSA key"):
        ga.load_private_key(pem)
    k = ga.load_private_key(PKCS1)
    bad = ga.RSAPrivateKey(k.n, k.e, k.d, k.p, k.q, k.dp, k.dq + 2, k.qinv)
    with pytest.raises(ValueError, match="self-check"):
        ga.rs256_sign(bad, b"x")
    tiny = ga.RSAPrivateKey(3233, 17, 413, 61, 53, 53, 49, 38)
    with pytest.raises(ValueError, match="too short"):
        ga.rs256_sign(tiny, b"x")


def test_app_jwt_claims_and_signature() -> None:
    key = ga.load_private_key(PKCS1)
    tok = ga.app_jwt("12345", key, now=1_700_000_000)
    h, p, s = tok.split(".")
    assert json.loads(_b64d(h)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(_b64d(p)) == {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "12345"}
    sig = int.from_bytes(_b64d(s), "big")
    em = pow(sig, key.e, key.n).to_bytes(256, "big")
    assert em.startswith(b"\x00\x01\xff") and em.endswith(__import__("hashlib").sha256(f"{h}.{p}".encode()).digest())


def _env(**extra: str) -> dict[str, str]:
    return {"SQUADHQ_APP_ID": "777", "SQUADHQ_APP_PRIVATE_KEY": PKCS1, "ANTHROPIC_API_KEY": "sk-ant", **extra}


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_mint_scoped_token_and_cache() -> None:
    t = (FakeTransport()
         .add("GET /orgs/acme/installation", (200, {"id": 42}))
         .add("POST /app/installations/42/access_tokens",
              (201, {"token": "ghs_one", "expires_at": "2023-11-14T23:13:20Z"}),
              (201, {"token": "ghs_two", "expires_at": "2023-11-15T00:13:20Z"})))
    clock = Clock(1_700_000_000.0)  # 2023-11-14T22:13:20Z, token valid 1h
    m = ga.GitHubAppTokenMinter(env=_env(), transport=t, clock=clock)
    toks = m.mint("acme/web", "implement")
    assert [(x.env_name, x.value) for x in toks] == [("GH_TOKEN", "ghs_one"), ("ANTHROPIC_API_KEY", "sk-ant")]
    assert toks[0].expires_at == "2023-11-14T23:13:20Z"
    post = t.calls[1]
    assert post["body"]["repositories"] == ["web"]
    assert post["body"]["permissions"]["contents"] == "write"
    assert "administration" not in post["body"]["permissions"]
    assert post["headers"]["Authorization"].startswith("Bearer ey")
    # Cached while fresh; installation id cached per owner.
    clock.t += 3000
    assert m.mint("acme/web", "implement")[0].value == "ghs_one"
    assert len(t.calls) == 2
    # Within the 5-minute refresh margin -> re-mint.
    clock.t += 400
    assert m.mint("acme/web", "implement")[0].value == "ghs_two"
    assert [c["method"] for c in t.calls] == ["GET", "POST", "POST"]


def test_user_installation_fallback_and_not_installed(tmp_path: Path) -> None:
    keyfile = tmp_path / "k.pem"
    keyfile.write_text(PKCS8)
    env = {"SQUADHQ_APP_CLIENT_ID": "Iv1.abc", "SQUADHQ_APP_PRIVATE_KEY_FILE": str(keyfile)}
    t = (FakeTransport()
         .add("GET /orgs/alice/installation", (404, {"message": "Not Found"}))
         .add("GET /users/alice/installation", (200, {"id": 9}))
         .add("POST /app/installations/9/access_tokens", (201, {"token": "ghs_u", "expires_at": "garbage"}))
         .add("GET /orgs/ghost/installation", (404, {}))
         .add("GET /users/ghost/installation", (404, {})))
    m = ga.GitHubAppTokenMinter(env=env, transport=t, passthrough=(), api_url=API + "/")
    assert m.app_id == "Iv1.abc"
    assert [x.value for x in m.mint("alice/dotfiles", "review")] == ["ghs_u"]
    assert t.calls[2]["body"]["permissions"]["contents"] == "read"
    with pytest.raises(FatalError, match="not installed"):
        m.mint("ghost/repo", "review")


def test_non_github_project_and_bad_inputs() -> None:
    m = ga.GitHubAppTokenMinter(env=_env(), transport=FakeTransport())
    assert [x.env_name for x in m.mint("ENG", "implement")] == ["ANTHROPIC_API_KEY"]
    with pytest.raises(FatalError):
        m.installation_token("noslash", {})
    t = (FakeTransport().add("/installation", (200, {"id": 1}))
         .add("/access_tokens", (201, {"expires_at": "2030-01-01T00:00:00Z"})))
    with pytest.raises(FatalError, match="no token"):
        ga.GitHubAppTokenMinter(env=_env(), transport=t).mint("a/b", "x")
    with pytest.raises(FatalError, match="SQUADHQ_APP_ID"):
        ga.GitHubAppTokenMinter(env={})
    with pytest.raises(FatalError, match="PRIVATE_KEY"):
        ga.GitHubAppTokenMinter(env={"SQUADHQ_APP_ID": "1"})
    with pytest.raises(FatalError, match="bad private key"):
        ga.GitHubAppTokenMinter(env={"SQUADHQ_APP_ID": "1", "SQUADHQ_APP_PRIVATE_KEY": "junk"})


def test_permissions_for_overrides() -> None:
    assert ga.permissions_for("unknown-role", {}) == ga.DEFAULT_PERMISSIONS
    env = {"SQUADHQ_APP_PERMISSIONS_REVIEW_FIX": '{"contents": "read"}'}
    assert ga.permissions_for("review-fix", env) == {"contents": "read"}
    with pytest.raises(FatalError):
        ga.permissions_for("x", {"SQUADHQ_APP_PERMISSIONS_X": "{"})
    with pytest.raises(FatalError):
        ga.permissions_for("x", {"SQUADHQ_APP_PERMISSIONS_X": "[1]"})


def test_select_token_minter() -> None:
    assert isinstance(ga.select_token_minter({}), EnvTokenMinter)
    assert isinstance(ga.select_token_minter({"SQUADHQ_TOKEN_MINTER": "ENV"}), EnvTokenMinter)
    app = ga.select_token_minter({**_env(), "SQUADHQ_TOKEN_MINTER": "app"}, transport=FakeTransport())
    assert isinstance(app, ga.GitHubAppTokenMinter)
    with pytest.raises(FatalError):
        ga.select_token_minter({"SQUADHQ_TOKEN_MINTER": "vault"})
    with pytest.raises(FatalError):
        ga.select_token_minter({"SQUADHQ_TOKEN_MINTER": "app"})


def test_worker_default_deps_selects_app(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = pytest.importorskip("squadhq.server.worker")
    from squadhq.server.settings import ServerSettings

    for k, v in _env(SQUADHQ_TOKEN_MINTER="app").items():
        monkeypatch.setenv(k, v)
    try:
        settings = ServerSettings()  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - settings needs args in some revisions
        pytest.skip("ServerSettings() needs arguments")
    assert isinstance(worker.default_deps(settings).tokens, ga.GitHubAppTokenMinter)
    monkeypatch.setenv("SQUADHQ_TOKEN_MINTER", "env")
    assert isinstance(worker.default_deps(settings).tokens, EnvTokenMinter)
