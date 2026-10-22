"""Token-broker mode (ADR-0011) in squadhq.yml, render, doctor and init."""
import os

import pytest
import yaml

from squadhq import cli, doctor
from squadhq.config import load_text, parse
from squadhq.config import render as RN
from squadhq.config.gh import FakeGh, Result

from .hubfix import MIN_YAML, make_hub
from .test_doctor import NOW, full_gh

BROKER = "https://api.squadhq.example"
CLOUD = {"bot": {"app": "agent-squad-hq"}, "agent_bot": {"app": "agent-squad-hq-agent"}, "single_app": False}


def doc(**hub):
    d = yaml.safe_load(MIN_YAML)
    d["hub"].update(hub)
    return d


def test_cloud_slugs_with_broker():
    cfg, issues = parse(doc(token_broker=BROKER + "/", **CLOUD))
    assert cfg and not [i for i in issues if i.level == "error"]
    assert cfg.hub.token_broker == BROKER and cfg.hub.token_broker_audience == ""
    assert cfg.hub.bot.login == "agent-squad-hq[bot]" and cfg.hub.agent_bot.login == "agent-squad-hq-agent[bot]"
    # the agent slug defaults to <ops slug>-agent, which is the Cloud agent App
    cfg, _ = parse(doc(bot="agent-squad-hq", token_broker={"url": BROKER, "audience": "squadhq-cloud"}))
    assert cfg.hub.agent_bot.app == "agent-squad-hq-agent" and cfg.hub.token_broker_audience == "squadhq-cloud"


@pytest.mark.parametrize("value,path", [
    ("http://api.squadhq.example", "hub.token_broker.url"),
    ("https://user:pw@api.squadhq.example", "hub.token_broker.url"),
    ("https://api.squadhq.example/?x=1", "hub.token_broker.url"),
    ({"audience": "x"}, "hub.token_broker.url"),
    ({"url": BROKER, "extra": 1}, "hub.token_broker.extra"),
])
def test_broker_url_validation(value, path):
    cfg, issues = parse(doc(token_broker=value))
    assert cfg is None and any(i.path == path for i in issues), issues


def test_broker_and_single_app_conflict():
    cfg, issues = parse(doc(token_broker=BROKER, single_app=True))
    assert cfg is None and any(i.path == "hub.single_app" and "token_broker" in i.message for i in issues)


def test_render_in_broker_mode():
    cfg, _ = parse(doc(token_broker={"url": BROKER, "audience": "squadhq-cloud"}, **CLOUD))
    v = RN.repo_variables(cfg)
    assert v["SQUADHQ_TOKEN_BROKER"] == BROKER and v["SQUADHQ_TOKEN_BROKER_AUDIENCE"] == "squadhq-cloud"
    assert v["SQUADHQ_BOT_LOGIN"] == "agent-squad-hq[bot]"
    assert v["SQUADHQ_AGENT_BOT_LOGIN"] == "agent-squad-hq-agent[bot]"
    secrets = dict(RN.required_secrets(cfg))
    assert "SQUADHQ_APP_PRIVATE_KEY" not in secrets and "SQUADHQ_AGENT_APP_PRIVATE_KEY" not in secrets
    assert RN.app_commands(cfg) == []
    plain, _ = parse(doc())
    assert not any(k.startswith("SQUADHQ_TOKEN_BROKER") for k in RN.repo_variables(plain))
    assert "SQUADHQ_APP_PRIVATE_KEY" in dict(RN.required_secrets(plain))


def broker_cfg():
    return load_text(MIN_YAML.replace("hub:\n  repo: acme/agent-squad-hq",
                                      f"hub:\n  repo: acme/agent-squad-hq\n  token_broker: {BROKER}"))[0]


def test_doctor_broker_mode_needs_no_app_keys(monkeypatch):
    cfg = broker_cfg()
    probed = []
    monkeypatch.setattr(doctor, "PROBE", lambda url: probed.append(url) or "")
    gh = full_gh(cfg, agent_app=False)
    gh.routes[("secret", "list", "--org")] = Result(0, "[]")
    checks = doctor.check_github(cfg, gh, NOW)
    bad = [c.name for c in checks if c.status == "fail"]
    assert not [n for n in bad if "APP" in n], bad
    assert probed == [BROKER]
    assert any(c.id == "token-broker" and c.status == "ok" for c in checks)
    assert doctor.check_agent_config(cfg).status == "ok"


def test_doctor_broker_unreachable_or_not_https(monkeypatch):
    cfg = broker_cfg()
    monkeypatch.setattr(doctor, "PROBE", lambda url: "Name or service not known")
    c = doctor.check_token_broker(cfg, {"SQUADHQ_TOKEN_BROKER": {"value": BROKER}})
    assert c[0].status == "warn" and "did not answer" in c[0].name
    c = doctor.check_token_broker(cfg, {"SQUADHQ_TOKEN_BROKER": {"value": "http://api.squadhq.example"}})
    assert c[0].status == "fail" and "gh variable set SQUADHQ_TOKEN_BROKER" in c[0].fix


def test_doctor_variable_without_config_warns():
    cfg = load_text(MIN_YAML)[0]
    c = doctor.check_token_broker(cfg, {"SQUADHQ_TOKEN_BROKER": {"value": BROKER}})
    assert c[0].status == "warn" and "no hub.token_broker" in c[0].name
    assert doctor.check_token_broker(cfg, {}) == []


def test_probe_reports_unreachable():
    assert doctor.probe_url("https://127.0.0.1:9/", timeout=0.5) != ""


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "GH", FakeGh())
    root = make_hub(tmp_path, config_text=MIN_YAML)
    monkeypatch.chdir(root)
    monkeypatch.delenv("SQUADHQ_CONFIG", raising=False)
    monkeypatch.setenv("SQUADHQ_HOME", root)
    return root


FLAGS = ["init", "--non-interactive", "--owner", "acme", "--org-name", "Acme", "--timezone", "Europe/London",
         "--approver", "@acme/leads", "--app-slug", "agent-squad-hq"]


def test_init_token_broker_sets_the_variable_instead_of_apps(capsys, hub):
    os.remove("squadhq.yml")
    code = cli.main([*FLAGS, "--token-broker", BROKER + "/", "--create-app"])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "--create-app ignored" in out.err
    assert f"token_broker: {BROKER}" in out.out and "app: agent-squad-hq-agent" in out.out
    assert f"gh variable set SQUADHQ_TOKEN_BROKER --body {BROKER} --org acme --visibility all" in out.out
    assert "would create GitHub App" not in out.out and "PRIVATE_KEY" not in out.out


@pytest.mark.parametrize("extra", [["--token-broker", "http://x.example"], ["--token-broker", BROKER, "--single-app"]])
def test_init_token_broker_rejects(capsys, hub, extra):
    assert cli.main([*FLAGS, *extra]) == 2
    assert "--token-broker needs an https URL" in capsys.readouterr().err
