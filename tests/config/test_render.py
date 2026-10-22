import os
from datetime import date

import pytest
import yaml

from squadhq.config import load_text, loader, parse
from squadhq.config import render as RN
from squadhq.config import registry as R

from .hubfix import EXAMPLE, MIN_YAML, WORKFLOW, make_hub

REF = date(2026, 1, 1)
REPO_CONFIG = os.path.join(os.path.dirname(EXAMPLE), "config")


def example():
    with open(EXAMPLE) as f:
        cfg, _ = load_text(f.read())
    return cfg


def cfg_from(**over):
    d = yaml.safe_load(MIN_YAML)
    d.update(over)
    cfg, issues = parse(d)
    assert cfg, [str(i) for i in issues]
    return cfg


def test_committed_config_equals_example_render():
    """config/*.json are generated from squadhq.example.yml (the single source of truth)."""
    root = os.path.dirname(EXAMPLE)
    cfg = loader.load(EXAMPLE, hub_root=root)
    for name, obj in (("models.json", RN.render_models(cfg)), ("auth.json", RN.render_auth(cfg)),
                      ("projects.json", RN.render_projects(cfg))):
        with open(os.path.join(REPO_CONFIG, name)) as f:
            assert f.read() == RN.dump_json(obj), f"config/{name} is stale: squadhq render --config squadhq.example.yml"
    models = RN.render_models(cfg)
    heavy = models["tiers"]["heavy"]["jobs"]
    assert "quarterly-review" in heavy and "accessibility-designer" in models["tiers"]["light"]["jobs"]
    assert {"cos", "brief"} <= {j for t in models["hub_loops"].values() for j in t["jobs"]}


def test_models_shape_matches_runtime():
    out = RN.render_models(example())
    for t in list(out["tiers"].values()) + list(out["hub_loops"].values()):
        assert {"primary", "fallback", "jobs"} <= set(t)
    assert out["tiers"]["heavy"]["primary"] == "claude-sonnet-5-5"
    assert set(R.DEFAULT_JOB_TIERS) == {j for f in ("tiers", "hub_loops") for t in out[f].values() for j in t["jobs"]}


def test_models_without_fallback_and_custom_tier():
    cfg = cfg_from(providers={"anthropic": {}, "routing": {
        "heavy": {"primary": "claude-sonnet-5-5"}, "light": {"primary": "claude-haiku-5-5"},
        "frontier": {"primary": "claude-opus-5-5"}}, "jobs": {"implement": "frontier", "cos": "frontier"}})
    out = RN.render_models(cfg)
    assert "fallback" not in out["tiers"]["heavy"]
    assert out["tiers"]["frontier"]["jobs"] == ["implement"]
    assert out["hub_loops"]["frontier"]["jobs"] == ["cos"]


def test_auth_shape():
    out = RN.render_auth(example())
    assert out["coding_agents_auth"] == "api"
    assert out["fallback_on_capacity_trip"] is False
    assert out["providers"]["openai"]["secret"] == "OPENAI_API_KEY"
    cfg = cfg_from(providers={"openai": {}, "ollama": {"base_url": "http://o:1"}, "bedrock": {"region": "eu-west-2"},
                              "routing": {"heavy": {"primary": "openai/gpt-5"}, "light": {"primary": "ollama/q"}}})
    out = RN.render_auth(cfg)
    assert out["coding_agents_auth"] == "api"
    assert out["providers"]["ollama"]["base_url"] == "http://o:1"
    assert out["providers"]["bedrock"]["region"] == "eu-west-2"


def test_projects_shape_keeps_runtime_keys():
    out = RN.render_projects(example())
    p = out["projects"][0]
    # keys the runtime (router.yml, onboard.py, agent_runner.py) reads today
    legacy_keys = {"name", "repo", "merge_mode", "integration_branch", "frontend_url", "staging_url",
                   "keywords", "markets", "notes"}
    assert legacy_keys <= set(p)
    assert p["merge_mode"] == "approver"
    assert p["frontend_url"] == "https://acme.example"
    assert p["staging_url"] == "https://staging-api.acme.example"
    assert p["staging_frontend_url"] == "https://staging.acme.example"
    assert p["packs"] == ["node-next", "python-fastapi"]
    assert p["pack_roots"] == {"node-next": "web", "python-fastapi": "api"}
    assert p["environments"]["production"]["approval"] == "required"
    assert p["budget_usd"] == 60
    assert p["agents"] == {"enabled": ["growth"], "disabled": []}
    assert p["brief_file"] == "docs/product.md"


def test_projects_merge_modes_and_urls():
    cfg = cfg_from(policies={"merge": {"mode": "risk_based"}}, budgets={"per_project_usd": {"default": 7}},
                   projects=[{"name": "A", "repo": "acme/a", "merge_mode": "auto",
                              "environments": {"staging": {"url": "https://s.example",
                                                           "urls": {"mcp": "https://m.example"}}}},
                             {"name": "B", "repo": "acme/b"}])
    a, b = RN.render_projects(cfg)["projects"]
    assert a["merge_mode"] == "auto" and a["merge"]["mode"] == "auto"
    assert a["staging_url"] == "https://s.example" and a["staging_mcp_url"] == "https://m.example"
    assert b["merge_mode"] == "risk_based" and b["merge"]["risk_based"]["max_risk"] == "low"
    assert b["frontend_url"] == "" and b["budget_usd"] == 7


def test_rewrite_workflow_single_and_multi():
    sched = {"daily_brief": (["30 6 * * 1-5"], "weekdays 07:30"), "cos": (["0,30 6-18 * * *", "0 19 * * *"], "c")}
    text = ('on:\n  schedule:\n    - cron: "0 0 * * *"  # squadhq:schedule:cos\n'
            "    - cron: '1 1 * * *' # squadhq:schedule:cos extra\n"
            "    - cron: \"5 5 * * *\"  # squadhq:schedule:daily_brief\r\n"
            '    - cron: "9 9 * * *"  # squadhq:schedule:unknown_key\n'
            '    - cron: "7 7 * * *"\n')
    new, seen = RN.rewrite_workflow(text, sched)
    assert seen == {"cos", "daily_brief", "unknown_key"}
    lines = new.splitlines(keepends=True)
    assert lines[2] == '    - cron: "0,30 6-18 * * *"  # squadhq:schedule:cos (c)\n'
    assert lines[3] == '    - cron: "0 19 * * *"  # squadhq:schedule:cos (c)\n'
    assert lines[4].endswith("\r\n") and '"30 6 * * 1-5"' in lines[4]
    assert "unknown_key" in lines[5] and '"7 7 * * *"' in lines[6]
    # idempotent
    again, _ = RN.rewrite_workflow(new, sched)
    assert again == new


def test_repo_variables_and_secrets():
    cfg = example()
    v = RN.repo_variables(cfg)
    for k in ("SQUADHQ_HUB_REPO", "SQUADHQ_BOT_LOGIN", "SQUADHQ_TRIGGER", "SQUADHQ_DISABLED_AGENTS",
              "SQUADHQ_MONTHLY_BUDGET_USD", "SQUADHQ_TRUSTED_ASSOCIATIONS", "SQUADHQ_GATES"):
        assert k in v
    assert v["SQUADHQ_DISABLED_AGENTS"] == "growth,seo"
    assert v["SQUADHQ_MONTHLY_BUDGET_USD"] == "100"
    assert "SQUADHQ_APP_CLIENT_ID" not in v
    names = [n for n, _ in RN.required_secrets(cfg)]
    assert names == ["SQUADHQ_APP_PRIVATE_KEY", "SQUADHQ_AGENT_APP_PRIVATE_KEY", "ANTHROPIC_API_KEY"]
    cfg.providers.configs["anthropic"].auth = "subscription"
    cfg.providers.configs["anthropic"].secret = "CLAUDE_CODE_OAUTH_TOKEN"
    assert [n for n, _ in RN.required_secrets(cfg)] == ["SQUADHQ_APP_PRIVATE_KEY", "SQUADHQ_AGENT_APP_PRIVATE_KEY",
                                                        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
    cfg2 = cfg_from(hub={"repo": "acme/hq", "bot": {"app": "x", "client_id": "Iv1"}}, budgets={"monthly_usd": 12.5},
                    providers={"anthropic": {}, "openai": {}, "routing": {
                        "heavy": {"primary": "anthropic/claude-sonnet-5-5", "fallback": "openai/gpt-5"},
                        "light": {"primary": "claude-haiku-5-5"}}})
    v2 = RN.repo_variables(cfg2)
    assert v2["SQUADHQ_APP_CLIENT_ID"] == "Iv1" and v2["SQUADHQ_MONTHLY_BUDGET_USD"] == "12.5"
    assert [n for n, _ in RN.required_secrets(cfg2)] == ["SQUADHQ_APP_PRIVATE_KEY", "SQUADHQ_AGENT_APP_PRIVATE_KEY",
                                                         "ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    cmds = RN.variable_commands({"A": "x y"}, "acme/hq")
    assert cmds == ["gh variable set A -R acme/hq --body 'x y'"]


def test_plan_write_check_roundtrip(tmp_path):
    root = make_hub(tmp_path, workflows={"daily-brief.yml": WORKFLOW, "notes.txt": "x",
                                         "other.yml": "on: push\n"})
    cfg = cfg_from()
    rp = RN.plan(cfg, root, REF)
    assert {f.path for f in rp.stale} == {"config/models.json", "config/auth.json", "config/projects.json",
                                          ".github/workflows/daily-brief.yml"}
    assert any("no `# squadhq:schedule" in w for w in rp.warnings)
    assert any(w.startswith("DST") for w in rp.warnings)
    assert "+++ b/config/auth.json" in rp.files[1].diff()
    written = RN.write(rp)
    assert len(written) == 4
    with open(os.path.join(root, ".github/workflows/daily-brief.yml")) as f:
        assert '"30 6 * * 1-5"  # squadhq:schedule:daily_brief (weekdays 07:30' in f.read()
    rp2 = RN.plan(cfg, root, REF)
    assert rp2.stale == []


def test_plan_reports_orphan_marker_and_no_dst(tmp_path):
    wf = 'on:\n  schedule:\n    - cron: "1 1 * * *"  # squadhq:schedule:nobody\n'
    root = make_hub(tmp_path, workflows={"x.yml": wf})
    cfg = cfg_from(org={"name": "A", "timezone": "Asia/Tokyo", "approvers": ["@a"]})
    rp = RN.plan(cfg, root, REF)
    assert any("squadhq:schedule:nobody" in w for w in rp.warnings)
    assert not any(w.startswith("DST") for w in rp.warnings)


def test_plan_without_workflow_dir(tmp_path):
    rp = RN.plan(cfg_from(), str(tmp_path), REF)
    assert len(rp.files) == 3


def test_plan_schedule_error(tmp_path):
    d = yaml.safe_load(MIN_YAML)
    d["org"]["timezone"] = "Asia/Tokyo"
    d["schedules"] = {"architect_quarterly": "quarterly 1st 01:00"}
    cfg, issues = parse(d)
    assert cfg is None and any(i.path == "schedules.architect_quarterly" for i in issues)
    cfg = cfg_from(org={"name": "A", "timezone": "Asia/Tokyo", "approvers": ["@a"]})
    cfg.schedules["architect_quarterly"] = "quarterly 1st 01:00"
    with pytest.raises(ValueError, match="previous month"):
        RN.plan(cfg, str(tmp_path), REF)


def test_model_refs():
    refs = RN.model_refs(example())
    assert ("providers.routing.heavy.primary", "anthropic/claude-sonnet-5-5") in [(p, str(m)) for p, m in refs]


def test_skill_scheduler_multiple_keys_in_one_file(tmp_path):
    wf = ("on:\n  schedule:\n"
          '    - cron: "0 0 * * *"  # squadhq:schedule:uptime_watch\n'
          '    - cron: "0 0 * * *"  # squadhq:schedule:weekly_dast\n'
          '    - cron: "0 0 * * *"  # squadhq:schedule:docs_gardener\n')
    root = make_hub(tmp_path, workflows={"skill-scheduler.yml": wf})
    rp = RN.plan(cfg_from(), root, REF)
    new = next(f.new for f in rp.files if f.path.endswith("skill-scheduler.yml")).splitlines()
    assert new[2].startswith('    - cron: "*/15 * * * *"  # squadhq:schedule:uptime_watch')
    assert new[3].startswith('    - cron: "0 2 * * 6"  # squadhq:schedule:weekly_dast')
    assert new[4].startswith('    - cron: "0 4 * * 3"  # squadhq:schedule:docs_gardener')


def test_projects_quality_rendered():
    out = RN.render_projects(example())
    p = out["projects"][0]
    assert p["quality"] == {"arch": "auto", "lint": "auto", "changed_coverage": "report"}
    assert "quality" in out["_docs"]
    cfg, _ = parse(yaml.safe_load(MIN_YAML.replace(
        "    pack_roots: {node-next: web}\n",
        "    pack_roots: {node-next: web}\n    quality: {arch: required, changed_coverage: enforce}\n")))
    q = RN.render_projects(cfg)["projects"][0]["quality"]
    assert q == {"arch": "required", "lint": "auto", "changed_coverage": "enforce"}
