import json
import urllib.error

import pytest
import yaml

from squadhq.skills import uptime

CFG = {
    "defaults": {"interval": "5m", "timeout_s": 10, "failure_threshold": 2, "recovery_threshold": 2,
                 "severity": "P0", "auto_close": True, "incident_label": "incident"},
    "checks": [{"name": "home", "url": "https://example.com/healthz", "expect_body_regex": "ok"}],
}


def fetcher(responses):
    seq = list(responses)

    def fetch(url, method, timeout):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return fetch


def test_validate_config_ok_and_errors():
    assert uptime.validate_config(CFG) == []
    assert uptime.validate_config([]) == ["config must be a mapping"]
    bad = {
        "defaults": {"interval": "30s", "failure_threshold": 0, "timeout_s": 999, "severity": "P9",
                     "auto_close": "yes", "incident_label": "", "bogus": 1},
        "checks": [
            {"name": "Bad Name", "url": "http://x", "method": "POST", "expect_status": ["200"],
             "expect_body_regex": "(", "extra": 1},
            {"name": "dup", "url": "https://a"}, {"name": "dup", "url": "https://b"}, "nope",
        ],
    }
    errs = "\n".join(uptime.validate_config(bad))
    for needle in ("at least 1m", "failure_threshold must be", "timeout_s must be", "severity must be",
                   "auto_close must be", "incident_label must be", "unknown keys ['bogus']", "kebab-case",
                   "https:// URL", "method must be", "expect_status", "expect_body_regex", "duplicated",
                   "checks[3] must be a mapping", "unknown keys ['extra']"):
        assert needle in errs, needle
    assert "interval must look like" in "\n".join(uptime.validate_config(
        {"defaults": {"interval": "often"}, "checks": [{"name": "a", "url": "https://a"}]}))
    assert "defaults must be a mapping" in uptime.validate_config({"defaults": 1, "checks": []})[0]


def test_parse_interval():
    assert uptime.parse_interval("90s") == 90
    assert uptime.parse_interval("2h") == 7200
    with pytest.raises(ValueError):
        uptime.parse_interval("5 minutes")


def test_probe_variants():
    check = uptime.effective(CFG["checks"][0], CFG)
    assert uptime.probe(check, fetcher([(200, "all ok")])).ok
    r = uptime.probe(check, fetcher([(503, "")]))
    assert not r.ok and r.status == 503
    r = uptime.probe(check, fetcher([(200, "down")]))
    assert not r.ok and "regex" in r.error
    r = uptime.probe(check, fetcher([TimeoutError("slow")]))
    assert not r.ok and "TimeoutError" in r.error


def test_state_machine_open_once_then_close():
    state = {}
    fetch = fetcher([(500, ""), (500, ""), (500, ""), (200, "ok"), (200, "ok"), (200, "ok")])
    actions = []
    for _ in range(6):
        actions += uptime.run_checks(CFG, state, fetch=fetch, now="2026-10-21T00:00:00Z")[1]
    assert [a["type"] for a in actions] == ["open_incident", "close_incident"]
    opened = actions[0]
    assert opened["labels"] == ["P0", "incident"] and opened["title"] == "Incident: home down"
    assert state["home"]["incident_open"] is False


def test_no_auto_close_comments_instead():
    cfg = {"defaults": dict(CFG["defaults"], auto_close=False, failure_threshold=1, recovery_threshold=1),
           "checks": CFG["checks"]}
    state = {}
    fetch = fetcher([(500, ""), (200, "ok")])
    a1 = uptime.run_checks(cfg, state, fetch=fetch, now="t")[1]
    a2 = uptime.run_checks(cfg, state, fetch=fetch, now="t")[1]
    assert a1[0]["type"] == "open_incident" and "human closes it" in a1[0]["body"]
    assert a2[0]["type"] == "comment_recovered"


def test_http_fetch_rejects_http_and_handles_errors(monkeypatch):
    with pytest.raises(ValueError):
        uptime.http_fetch("http://example.com", "GET", 1)

    class Resp:
        status = 200
        def read(self, n): return b"ok"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(uptime.urllib.request, "urlopen", lambda req, timeout: Resp())
    assert uptime.http_fetch("https://example.com", "GET", 1) == (200, "ok")

    def boom(req, timeout):
        raise urllib.error.HTTPError("https://example.com", 502, "bad", {}, None)
    monkeypatch.setattr(uptime.urllib.request, "urlopen", boom)
    assert uptime.http_fetch("https://example.com", "GET", 1) == (502, "")


def test_load_config_shapes(tmp_path):
    skill = tmp_path / "skill.yml"
    skill.write_text(yaml.safe_dump({"id": "uptime-watcher", "config": CFG}))
    assert uptime.load_config(str(skill)) == CFG
    sq = tmp_path / "squadhq.yml"
    sq.write_text(yaml.safe_dump({"uptime": CFG}))
    assert uptime.load_config(str(sq)) == CFG
    bare = tmp_path / "bare.yml"
    bare.write_text(yaml.safe_dump(CFG))
    assert uptime.load_config(str(bare)) == CFG
    bad = tmp_path / "bad.yml"
    bad.write_text("- 1\n")
    with pytest.raises(ValueError):
        uptime.load_config(str(bad))


def test_main_validate_and_run(tmp_path, capsys):
    cfg = tmp_path / "c.yml"
    cfg.write_text(yaml.safe_dump(dict(CFG, defaults=dict(CFG["defaults"], failure_threshold=1))))
    assert uptime.main(["validate", "--config", str(cfg)]) == 0
    assert "OK: 1 checks" in capsys.readouterr().out
    state = tmp_path / "state" / "uptime.json"
    assert uptime.main(["run", "--config", str(cfg), "--state", str(state)],
                       fetch=fetcher([(500, "")])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["actions"][0]["type"] == "open_incident"
    assert json.loads(state.read_text())["home"]["incident_open"] is True
    assert uptime.main(["run", "--config", str(cfg), "--state", str(state)], fetch=fetcher([(500, "")])) == 0
    assert json.loads(capsys.readouterr().out)["actions"] == []
    bad = tmp_path / "bad.yml"
    bad.write_text(yaml.safe_dump({"checks": []}))
    assert uptime.main(["validate", "--config", str(bad)]) == 1
    assert uptime.main(["validate", "--config", str(tmp_path / "missing.yml")]) == 2


def test_catalogue_uptime_config_is_valid():
    from squadhq.skills import loader
    skill = loader.get_skill("uptime-watcher")
    assert uptime.validate_config(skill.config) == []
