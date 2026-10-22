"""providers.anthropic.pool: validation, render to config/auth.json, secrets,
doctor and `validate --explain`."""
import json

import yaml

from squadhq import cli, doctor
from squadhq.config import parse
from squadhq.config import render as RN

from .hubfix import MIN_YAML, make_hub
from .test_doctor import FakeGh, full_gh


def with_anthropic(**anth):
    d = yaml.safe_load(MIN_YAML)
    d["providers"] = {"anthropic": {"auth": "subscription", **anth}}
    return d


def issues(d):
    cfg, iss = parse(d)
    return cfg, [i for i in iss if i.level == "error"], [i for i in iss if i.level == "warning"]


POOL = [{"id": "main"}, {"id": "team-b", "auth": "subscription", "secret": "CLAUDE_CODE_OAUTH_TOKEN_4"},
        {"id": "keys", "auth": "api"}]


def test_pool_defaults_and_model():
    cfg, errs, warns = issues(with_anthropic(pool=POOL, pool_strategy="round_robin"))
    assert not errs, [str(e) for e in errs]
    anth = cfg.providers.configs["anthropic"]
    assert [(a.id, a.auth, a.secret) for a in anth.pool] == [
        ("main", "subscription", "CLAUDE_CODE_OAUTH_TOKEN_1"),
        ("team-b", "subscription", "CLAUDE_CODE_OAUTH_TOKEN_4"),
        ("keys", "api", "ANTHROPIC_API_KEY_3")]
    assert anth.pool_strategy == "round_robin"
    assert not [w for w in warns if "pool" in w.path]


def test_no_pool_is_unchanged():
    cfg, errs, _ = issues(with_anthropic())
    assert not errs
    assert cfg.providers.configs["anthropic"].pool == []
    assert "pool" not in RN.render_auth(cfg)
    assert RN.pool_secrets(cfg) == [] and RN.pool_secret_commands(cfg) == []


def test_pool_validation_errors():
    _, errs, _ = issues(with_anthropic(pool=[{"id": "a"}, {"id": "a"}]))
    assert any("duplicate pool account id" in e.message for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"id": "a", "secret": "bad-name"}]))
    assert any(e.path == "providers.anthropic.pool[0].secret" for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"id": "a", "secret": "_X"}]))
    assert any(e.path == "providers.anthropic.pool[0].secret" for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"id": f"a{i}"} for i in range(6)]))
    assert any("at most 5" in e.message for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"id": "Bad"}]))
    assert any(e.path == "providers.anthropic.pool[0].id" for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"auth": "api"}]))
    assert any(e.path == "providers.anthropic.pool[0].id" and "required" in e.message for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"id": "a", "auth": "iam"}]))
    assert any(e.path == "providers.anthropic.pool[0].auth" for e in errs)
    _, errs, _ = issues(with_anthropic(pool=[{"id": "a", "secret": "X_1"}, {"id": "b", "secret": "X_1"}]))
    assert any("already used" in e.message for e in errs)
    _, errs, _ = issues(with_anthropic(pool="main"))
    assert any(e.path == "providers.anthropic.pool" for e in errs)
    _, errs, _ = issues(with_anthropic(pool_strategy="random"))
    assert any(e.path == "providers.anthropic.pool_strategy" for e in errs)
    d = yaml.safe_load(MIN_YAML)
    d["providers"] = {"anthropic": {"auth": "api"}, "openai": {"auth": "api", "pool": []}}
    _, errs, _ = issues(d)
    assert any(e.path == "providers.openai.pool" for e in errs)


def test_pool_warnings():
    _, errs, warns = issues(with_anthropic(pool=[{"id": "a", "auth": "api", "secret": "MY_ORG_KEY"}]))
    assert not errs
    assert any("not a declared workflow secret slot" in w.message for w in warns)
    _, _, warns = issues(with_anthropic(pool_strategy="ordered"))
    assert any(w.path == "providers.anthropic.pool_strategy" for w in warns)


def test_render_auth_json_pool():
    cfg, _, _ = issues(with_anthropic(pool=POOL))
    auth = RN.render_auth(cfg)
    assert auth["pool"] == [{"id": "main", "auth": "subscription", "secret": "CLAUDE_CODE_OAUTH_TOKEN_1"},
                            {"id": "team-b", "auth": "subscription", "secret": "CLAUDE_CODE_OAUTH_TOKEN_4"},
                            {"id": "keys", "auth": "api", "secret": "ANTHROPIC_API_KEY_3"}]
    assert auth["pool_strategy"] == "ordered"
    notes = " ".join(auth["_notes"])
    assert "personal consumer subscriptions" in notes and "SQUADHQ_HANDOFF_KEY" in notes
    # the runtime parser reads exactly what render writes
    from squadhq import account_pool as AP
    assert [a.id for a in AP.parse_pool(json.loads(json.dumps(auth)))[0]] == ["main", "team-b", "keys"]


def test_pool_secrets_and_commands():
    cfg, _, _ = issues(with_anthropic(pool=POOL))
    names = [n for n, _ in RN.required_secrets(cfg)]
    for n in ("CLAUDE_CODE_OAUTH_TOKEN_1", "CLAUDE_CODE_OAUTH_TOKEN_4", "ANTHROPIC_API_KEY_3", "SQUADHQ_HANDOFF_KEY"):
        assert n in names
    lines = RN.pool_secret_commands(cfg)
    assert lines[0].startswith("gh secret set CLAUDE_CODE_OAUTH_TOKEN_1 --org acme --visibility all")
    assert "claude setup-token" in lines[0]
    assert any(line.startswith("gh secret set ANTHROPIC_API_KEY_3 ") for line in lines)
    assert lines[-1] == "openssl rand -hex 32 | gh secret set SQUADHQ_HANDOFF_KEY --org acme --visibility all"
    assert RN.pool_secret_commands(cfg, "repo")[0].startswith("gh secret set CLAUDE_CODE_OAUTH_TOKEN_1 -R ")


def _write_cfg(tmp_path, d):
    root = make_hub(tmp_path, config_text=yaml.safe_dump(d))
    return root, f"{root}/squadhq.yml"


def _cli_in(root, monkeypatch, *argv):
    monkeypatch.chdir(root)
    monkeypatch.delenv("SQUADHQ_CONFIG", raising=False)
    monkeypatch.setenv("SQUADHQ_HOME", root)
    return cli.main(list(argv))


def test_render_cli_prints_pool_secret_lines(tmp_path, capsys, monkeypatch):
    root, _ = _write_cfg(tmp_path, with_anthropic(pool=POOL))
    assert _cli_in(root, monkeypatch, "render") == 0
    out = capsys.readouterr().out
    assert "account pool secrets" in out
    assert "gh secret set CLAUDE_CODE_OAUTH_TOKEN_4 --org acme --visibility all" in out
    assert "SQUADHQ_HANDOFF_KEY" in out
    with open(f"{root}/config/auth.json") as f:
        assert [a["id"] for a in json.load(f)["pool"]] == ["main", "team-b", "keys"]


def test_validate_explain_shows_pool(tmp_path, capsys, monkeypatch):
    root, path = _write_cfg(tmp_path, with_anthropic(pool=POOL, pool_strategy="round_robin"))
    assert _cli_in(root, monkeypatch, "validate", path, "--explain") == 0
    out = capsys.readouterr().out
    assert "account pool (round_robin; all tripped -> pause)" in out
    assert "team-b" in out and "CLAUDE_CODE_OAUTH_TOKEN_4" in out
    root2, path2 = _write_cfg(tmp_path / "b", with_anthropic())
    assert _cli_in(root2, monkeypatch, "validate", path2, "--explain") == 0
    assert "account pool: none" in capsys.readouterr().out


def test_doctor_pool_offline_lists_accounts_and_warns_on_mixing(tmp_path):
    root, path = _write_cfg(tmp_path, with_anthropic(pool=POOL))
    checks = [c for c in doctor.run(path, root, FakeGh(), offline=True) if c.id == "pool"]
    names = " | ".join(c.name for c in checks)
    assert "3 account(s), ordered" in names and "main (subscription, CLAUDE_CODE_OAUTH_TOKEN_1)" in names
    assert any("mixes api and subscription" in c.name and c.status == "warn" for c in checks)
    assert any("subscription accounts" in c.name and "terms" in c.fix for c in checks)
    only = doctor.run(path, root, FakeGh(), offline=True, only="pool")
    assert only and all(c.id == "pool" for c in only)


def test_doctor_pool_online_warns_on_missing_secrets():
    cfg, _, _ = issues(with_anthropic(pool=[{"id": "main"}, {"id": "two"}]))
    checks = doctor.check_github(cfg, full_gh(cfg))
    pool = {c.name: c for c in checks if c.id == "pool"}
    missing = [n for n in pool if "missing" in n]
    assert any("CLAUDE_CODE_OAUTH_TOKEN_2" in n for n in missing)
    assert any("SQUADHQ_HANDOFF_KEY" in n for n in missing)
    assert all(pool[n].status == "warn" for n in missing)
    # pool secrets are reported by the pool check, not as failing required secrets
    assert not [c for c in checks if c.id == "secrets" and "CLAUDE_CODE_OAUTH_TOKEN_1" in c.name]


def test_doctor_no_pool_offline():
    cfg, _, _ = issues(with_anthropic())
    assert [c.name for c in doctor.check_pool(cfg)] == ["account pool: none (single credential)"]
    assert doctor.check_pool(cfg, {}) == []
