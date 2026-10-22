"""Two GitHub App identities: ops (protocol, merges) and agent (LLM work)."""
import pytest

from squadhq import settings, untrusted


@pytest.fixture
def two_apps(monkeypatch):
    monkeypatch.setenv("SQUADHQ_AGENT_APP_CLIENT_ID", "Iv-agent")
    monkeypatch.setenv("SQUADHQ_BOT_LOGIN", "acme-ops[bot]")
    monkeypatch.setenv("SQUADHQ_AGENT_BOT_LOGIN", "acme-agent[bot]")
    monkeypatch.delenv("SQUADHQ_BOT_ALIASES", raising=False)
    monkeypatch.delenv("SQUADHQ_AGENT_BOT_ALIASES", raising=False)


def test_single_app_mode_defaults(monkeypatch):
    for k in ("SQUADHQ_AGENT_APP_CLIENT_ID", "SQUADHQ_BOT_LOGIN", "SQUADHQ_AGENT_BOT_LOGIN", "SQUADHQ_BOT_ALIASES"):
        monkeypatch.delenv(k, raising=False)
    assert settings.single_app_mode()
    assert settings.agent_bot_login() == settings.bot_login() == "squadhq-ops[bot]"
    assert settings.agent_bot_logins() == settings.bot_logins()
    monkeypatch.setenv("SQUADHQ_AGENT_BOT_LOGIN", "ignored[bot]")  # no agent App -> agents ARE the ops bot
    assert settings.agent_bot_login() == "squadhq-ops[bot]"
    assert untrusted.is_agent_login("squadhq-ops[bot]") and untrusted.is_bot_login("app/squadhq-ops")


def test_two_apps_are_distinct(two_apps, monkeypatch):
    assert not settings.single_app_mode()
    assert settings.bot_logins() == {"acme-ops[bot]"}
    assert settings.agent_bot_logins() == {"acme-agent[bot]"}
    assert settings.automation_logins() == {"acme-ops[bot]", "acme-agent[bot]"}
    monkeypatch.delenv("SQUADHQ_AGENT_BOT_LOGIN")
    assert settings.agent_bot_login() == "squadhq-agent[bot]"
    monkeypatch.setenv("SQUADHQ_AGENT_BOT_LOGIN", "acme-agent[bot]")
    monkeypatch.setenv("SQUADHQ_AGENT_BOT_ALIASES", "old-agent[bot]")
    assert "old-agent[bot]" in settings.agent_bot_logins() and "old-agent[bot]" not in settings.bot_logins()


def test_untrusted_helpers_split_ops_and_agent(two_apps):
    ops = {"user": {"login": "acme-ops[bot]"}}
    agent = {"author": {"login": "app/acme-agent"}}
    other = {"user": {"login": "renovate[bot]"}, "author_association": "NONE"}
    human = {"user": {"login": "alice"}, "author_association": "MEMBER"}
    assert untrusted.by_bot(ops) and not untrusted.by_bot(agent) and not untrusted.by_bot(other)
    assert untrusted.by_agent(agent) and not untrusted.by_agent(ops)
    assert untrusted.by_automation(ops) and untrusted.by_automation(agent) and not untrusted.by_automation(other)
    assert untrusted.is_agent_login("acme-agent[bot]") and not untrusted.is_agent_login("")
    assert not untrusted.is_automation_login(None)
    assert untrusted.by_automation_or_trusted(agent) and untrusted.by_automation_or_trusted(human)
    assert not untrusted.by_automation_or_trusted(other)
    assert not untrusted.by_bot_or_trusted(agent)  # code-written statements: ops only
    assert untrusted.by_trusted_human(human)
    assert not untrusted.by_trusted_human({**agent, "author_association": "MEMBER"})


def test_sweeper_markers_are_ops_only(two_apps):
    from squadhq import stuck_sweeper as sw
    forged = {"user": {"login": "acme-agent[bot]"}, "body": f"{sw.BADGE} state=x"}
    real = {"user": {"login": "acme-ops[bot]"}, "body": f"{sw.BADGE} state=x"}
    eng = {"user": {"login": "acme-agent[bot]"}, "body": "[Engineer] delivered"}
    assert not sw.is_sweeper(forged) and sw.is_sweeper(real)
    assert sw.bot_said(eng, "[Engineer]") and not sw.ops_said(eng, "[Engineer]")
    assert sw.trusted_author({"user": {"login": "acme-agent[bot]"}})
    assert sw.gates({"author": {"login": "acme-agent[bot]"}, "body": "Gate-for: #3"}) == {3}
    assert sw.gates({"author": {"login": "renovate[bot]"}, "body": "Gate-for: #3"}) == set()


@pytest.mark.parametrize("item", [
    {"user": {"login": "acme-ops", "type": "User"}},           # REST: a human who registered the slug
    {"user": {"login": "acme-ops"}},                             # no type at all: fail closed
    {"author": {"login": "acme-ops", "is_bot": False}},          # gh --json human
    {"author": {"login": "acme-ops"}, "authorAssociation": "NONE"},
    {"author": {"login": "acme-agent", "__typename": "User"}},
])
def test_human_named_like_a_bot_is_rejected(two_apps, item):
    assert not untrusted.by_bot(item) and not untrusted.by_agent(item) and not untrusted.by_automation(item)


@pytest.mark.parametrize("item,ops", [
    ({"user": {"login": "acme-ops[bot]", "type": "Bot"}}, True),   # REST
    ({"user": {"login": "acme-ops[bot]"}}, True),                  # `[bot]` alone is unforgeable
    ({"author": {"login": "app/acme-ops", "is_bot": True}}, True),  # gh --json
    ({"author": {"login": "app/acme-ops"}}, True),                 # `/` is unforgeable too
    ({"author": {"login": "acme-ops", "is_bot": True}}, True),      # bare + bot flag
    ({"author": {"login": "acme-ops", "__typename": "Bot"}}, True),
    ({"author": {"login": "acme-agent", "is_bot": True}}, False),   # the agent, not ops
])
def test_genuine_bot_forms(two_apps, item, ops):
    assert untrusted.by_bot(item) is ops
    assert untrusted.by_automation(item)


def test_bare_strings_need_the_flag(two_apps):
    assert not untrusted.is_bot_login("acme-ops")
    assert untrusted.is_bot_login("acme-ops", is_bot=True)
    assert untrusted.is_bot_login("acme-ops[bot]") and untrusted.is_bot_login("app/acme-ops")
    assert not untrusted.is_bot_login("evil[bot]") and not untrusted.is_bot_login("app/evil")
    assert not untrusted.is_automation_login("acme-agent") and untrusted.is_automation_login("acme-agent", is_bot=True)
    assert untrusted.author_is_bot({"actor": {"type": "Bot"}}) and not untrusted.author_is_bot(None)


def test_loader_requires_full_bot_login():
    import yaml

    from squadhq.config import parse
    from tests.config.hubfix import MIN_YAML
    d = yaml.safe_load(MIN_YAML)
    d["hub"]["bot"] = {"app": "acme-ops", "login": "acme-ops"}
    cfg, issues = parse(d)
    assert cfg is None and any(i.path == "hub.bot.login" and "[bot]" in i.hint for i in issues)
