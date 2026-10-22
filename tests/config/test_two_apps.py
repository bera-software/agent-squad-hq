"""Ops App + agent App: config, render, ruleset and manifest."""
import json
import os

import yaml

from squadhq.config import github_ops as ops
from squadhq.config import parse
from squadhq.config import render as RN
from squadhq.config.app_manifest import build_manifest, manifest_template_path
from squadhq.config.gh import FakeGh, Result
from squadhq.config.loader import default_agent_slug

from .hubfix import MIN_YAML, REPO_ROOT


def doc(**hub):
    d = yaml.safe_load(MIN_YAML)
    d["hub"].update(hub)
    return d


def test_default_agent_slug():
    assert default_agent_slug("squadhq-ops") == "squadhq-agent"
    assert default_agent_slug("acme-ops") == "acme-agent"
    assert default_agent_slug("acme-squad") == "acme-squad-agent"
    assert len(default_agent_slug("a" * 34)) <= 34


def test_agent_bot_defaults_and_validation():
    cfg, _ = parse(doc())
    assert cfg.hub.bot.login == "squadhq-ops[bot]" and cfg.hub.agent_bot.login == "squadhq-agent[bot]"
    assert not cfg.hub.single_app
    cfg, _ = parse(doc(bot="acme-squad", agent_bot={"app": "acme-coder", "client_id": "Iv-a"}))
    assert cfg.hub.agent_bot.login == "acme-coder[bot]" and cfg.hub.agent_bot.client_id == "Iv-a"
    cfg, issues = parse(doc(bot="acme", agent_bot="acme"))
    assert cfg is None and any(i.path == "hub.agent_bot.app" for i in issues)
    cfg, issues = parse(doc(single_app=True, agent_bot="x-agent"))
    assert cfg is None and any("single_app" in i.message for i in issues)
    cfg, issues = parse(doc(single_app=True))
    assert cfg.hub.single_app and any(i.path == "hub.single_app" and i.level == "warning" for i in issues)


def test_render_vars_and_secrets_for_both_apps():
    cfg, _ = parse(doc(bot={"app": "acme-squad", "client_id": "Iv-o"}, agent_bot={"app": "acme-squad-agent",
                                                                                  "client_id": "Iv-a"}))
    v = RN.repo_variables(cfg)
    assert v["SQUADHQ_BOT_LOGIN"] == "acme-squad[bot]" and v["SQUADHQ_AGENT_BOT_LOGIN"] == "acme-squad-agent[bot]"
    assert v["SQUADHQ_APP_CLIENT_ID"] == "Iv-o" and v["SQUADHQ_AGENT_APP_CLIENT_ID"] == "Iv-a"
    assert "SQUADHQ_AGENT_APP_PRIVATE_KEY" in dict(RN.required_secrets(cfg))
    cmds = RN.app_commands(cfg)
    assert "gh variable set SQUADHQ_AGENT_APP_CLIENT_ID --org acme --visibility all --body Iv-a" in cmds
    assert "gh secret set SQUADHQ_AGENT_APP_PRIVATE_KEY --org acme --visibility all < acme-squad-agent.private-key.pem" in cmds
    assert RN.app_commands(cfg, "repo")[0].startswith("gh variable set SQUADHQ_APP_CLIENT_ID -R acme/agent-squad-hq")
    single, _ = parse(doc(single_app=True))
    sv = RN.repo_variables(single)
    assert "SQUADHQ_AGENT_BOT_LOGIN" not in sv and "SQUADHQ_AGENT_APP_CLIENT_ID" not in sv
    assert "SQUADHQ_AGENT_APP_PRIVATE_KEY" not in dict(RN.required_secrets(single))
    assert len(RN.app_commands(single)) == 2
    assert "'<squadhq-ops client id>'" in RN.app_commands(single)[0]


def test_render_plan_warns_in_single_app(tmp_path):
    single, _ = parse(doc(single_app=True))
    rp = RN.plan(single, str(tmp_path))
    assert any(w.startswith("single-app mode") for w in rp.warnings)


def test_ruleset_requires_independent_approval():
    body = ops.ruleset_body()
    pr_rule = next(r for r in body["rules"] if r["type"] == "pull_request")["parameters"]
    assert pr_rule["required_approving_review_count"] == 1
    assert pr_rule["require_last_push_approval"] is True and pr_rule["dismiss_stale_reviews_on_push"] is True
    assert body["bypass_actors"] == []
    checks = next(r for r in body["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert checks["required_status_checks"] == [{"context": "hub / ci-gate"}]


def test_ruleset_review():
    rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2,
                                                     "require_last_push_approval": True,
                                                     "dismiss_stale_reviews_on_push": True}}, "junk"]
    gh = FakeGh(routes={("api", "repos/acme/a/rules/branches/main"): Result(0, json.dumps(rules))})
    assert ops.ruleset_review(gh, "acme/a") == {"approvals": 2, "last_push": True, "dismiss_stale": True}
    gh = FakeGh(routes={("api", "repos/acme/a/rules/branches/main"): Result(0, "[]")})
    assert ops.ruleset_review(gh, "acme/a") == {"approvals": 0, "last_push": False, "dismiss_stale": False}
    assert ops.ruleset_review(FakeGh(), "acme/a") is None


def test_agent_manifest_is_narrow_and_documented():
    path = manifest_template_path("agent", REPO_ROOT)
    assert path.endswith(os.path.join("deploy", "github-app", "app-manifest-agent.json"))
    with open(path) as f:
        raw = json.load(f)
    perms = raw["default_permissions"]
    assert perms == {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "read",
                     "metadata": "read"}
    for p, level in perms.items():
        assert f"{p}: {level}" in raw["_permissions_rationale"]
    assert raw["hook_attributes"]["active"] is False and raw["default_events"] == [] and raw["public"] is False
    m = build_manifest("acme-squad-agent", "acme/hq", "http://127.0.0.1:1/callback", template_path=path)
    assert m["name"] == "acme-squad-agent" and not any(k.startswith("_") for k in m)
    with open(manifest_template_path("ops", REPO_ROOT)) as f:
        ops_perms = json.load(f)["default_permissions"]
    assert "workflows" in ops_perms and "workflows" not in perms and "administration" not in ops_perms
