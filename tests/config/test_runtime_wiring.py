"""squadhq.yml settings reach the code that enforces them."""
import json

import yaml

from squadhq import merge_policy as MP
from squadhq import stuck_sweeper
from squadhq.config import parse
from squadhq.config import render as RN
from squadhq.providers import configured_kwargs, get_provider

from .hubfix import MIN_YAML


def cfg_from(**over):
    d = yaml.safe_load(MIN_YAML)
    d.update(over)
    cfg, issues = parse(d)
    assert cfg, [str(i) for i in issues]
    return cfg, issues


# ---------------------------------------------------------------- models / providers
def test_non_anthropic_routes_are_provider_qualified():
    cfg, issues = cfg_from(providers={"anthropic": {}, "openai": {}, "ollama": {}, "routing": {
        "heavy": {"primary": "anthropic/claude-sonnet-5-5", "fallback": "openai/gpt-5"},
        "light": {"primary": "ollama/qwen3:8b"}}})
    m = RN.render_models(cfg)
    assert m["tiers"]["heavy"]["primary"] == "claude-sonnet-5-5"  # bare: claude-code-action + resolve default
    assert m["tiers"]["heavy"]["fallback"] == "openai/gpt-5"
    assert m["hub_loops"]["light"]["primary"] == "ollama/qwen3:8b"
    from squadhq.providers import resolve
    assert resolve(m["hub_loops"]["light"]["primary"]) == ("ollama", "qwen3:8b")
    # light serves coding jobs (scan, okr-track...) on claude-code: warned
    assert any(i.path == "providers.routing.light.primary" and "Anthropic models only" in i.message
               for i in issues)


def test_vertex_rejected():
    d = yaml.safe_load(MIN_YAML)
    d["providers"] = {"vertex": {}, "anthropic": {}}
    cfg, issues = parse(d)
    assert cfg is None and any(i.path == "providers.vertex" and "unsupported" in i.message for i in issues)


def test_configured_kwargs_from_auth_json(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"providers": {
        "openai": {"auth": "api", "secret": "MY_OPENAI", "base_url": "https://proxy.example/v1"},
        "azure_openai": {"base_url": "https://acme.openai.azure.com"},
        "bedrock": {"region": "eu-west-2", "secret": "BR_TOKEN", "base_url": "https://vpce.example"},
        "ollama": "junk"}}))
    monkeypatch.setenv("MY_OPENAI", "sk-test")
    monkeypatch.setenv("BR_TOKEN", "bt")
    assert configured_kwargs("openai", str(auth)) == {"base_url": "https://proxy.example/v1", "api_key": "sk-test"}
    assert configured_kwargs("azure", str(auth)) == {"endpoint": "https://acme.openai.azure.com"}
    assert configured_kwargs("bedrock", str(auth)) == {"endpoint_url": "https://vpce.example", "region": "eu-west-2",
                                                       "bearer_token": "bt"}
    assert configured_kwargs("ollama", str(auth)) == {}
    assert configured_kwargs("gemini", str(tmp_path / "missing.json")) == {}
    monkeypatch.delenv("MY_OPENAI")
    assert "api_key" not in configured_kwargs("openai", str(auth))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "auth.json").write_text(auth.read_text())
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path))
    p = get_provider("openai")
    assert p.base_url == "https://proxy.example/v1"
    assert get_provider("openai", base_url="https://explicit.example").base_url == "https://explicit.example"


# ---------------------------------------------------------------- disabled agents
def test_disabled_agents_expand_to_job_keys():
    cfg, _ = cfg_from(agents={"engineer": False, "product_owner": {"enabled": False}, "growth": {"enabled": True}},
                      skills={"threat-modeler": False, "dast-triage": {"enabled": True}})
    keys = RN.repo_variables(cfg)["SQUADHQ_DISABLED_AGENTS"].split(",")
    for k in ("engineer", "implement", "review-fix", "product_owner", "product-owner", "po-shape", "po-continue",
              "team-thread", "threat-modeler"):
        assert k in keys, k
    assert "growth" not in keys and "dast-triage" not in keys and keys == sorted(keys)


def test_skills_and_edition_keys():
    cfg, _ = cfg_from(skills={"a11y": True}, edition="pro",
                      projects=[{"name": "A", "repo": "acme/a", "skills": {"seo-audit": False}}])
    assert cfg.edition == "pro" and cfg.skills["a11y"].enabled
    assert RN.render_projects(cfg)["projects"][0]["agents"] == {"enabled": [], "disabled": ["seo-audit"]}
    d = yaml.safe_load(MIN_YAML)
    d["edition"] = "platinum"
    assert parse(d)[0] is None


# ---------------------------------------------------------------- policies / reserved
def test_policies_and_notify_rendered():
    cfg, _ = cfg_from(policies={"wip_limit": 3, "review_fix_rounds": 4},
                      notifications={"escalations": ["github_issue", "slack:#ops"], "digest": ["github_issue"]})
    assert RN.render_projects(cfg)["policies"] == {"wip_limit": 3, "review_fix_rounds": 4}
    v = RN.repo_variables(cfg)
    assert v["SQUADHQ_REVIEW_FIX_ROUNDS"] == "4"
    assert v["SQUADHQ_NOTIFY_ESCALATIONS"] == "github_issue;slack:#ops" and v["SQUADHQ_NOTIFY_DIGEST"] == "github_issue"


def test_reserved_notes_only_for_non_defaults():
    cfg, _ = cfg_from()
    assert RN.reserved_notes(cfg) == []
    cfg, _ = cfg_from(budgets={"per_project_usd": {"default": 5}, "per_run_usd": {"implement": 1},
                               "on_exceed": "notify"},
                      providers={"anthropic": {}, "executor": "codex"},
                      policies={"release": {"strategy": "release_tag"},
                                "merge": {"mode": "risk_based", "always_human": ["pii"]}},
                      notifications={"digest": ["email:a@b.co"]})
    cfg.org.operating_model = "solo"
    notes = "\n".join(RN.reserved_notes(cfg))
    for needle in ("per_project_usd", "per_run_usd", "on_exceed", "release.strategy", "executor", "operating_model",
                   "require_security_agent", "'pii'", "notifications.digest"):
        assert needle in notes, needle


def test_merge_modes_render_distinctly():
    for mode, rendered in (("approvers", "approver"), ("risk_based", "risk_based"), ("auto", "auto")):
        cfg, _ = cfg_from(policies={"merge": {"mode": mode}})
        p = RN.render_projects(cfg)["projects"][0]
        assert p["merge_mode"] == rendered and p["merge"]["mode"] == mode


FLOWS_AUTO = {"auto_merge": {"enabled": False, "requires_all": [
    {"review_verdict": "approve"}, {"risk_tag": "low"}, {"final_risk": "low"}, {"ci_checks": "passing"},
    {"pr_author_type": "bot"}], "label": "ready-to-merge"}}


def test_apply_project_merge_modes():
    assert MP.apply_project_merge(FLOWS_AUTO, {}) is FLOWS_AUTO
    assert MP.apply_project_merge(FLOWS_AUTO, {"mode": "auto"}) is FLOWS_AUTO
    on = dict(FLOWS_AUTO, auto_merge=dict(FLOWS_AUTO["auto_merge"], enabled=True))
    assert MP.apply_project_merge(on, {"mode": "approvers"})["auto_merge"]["enabled"] is False
    rb = MP.apply_project_merge(FLOWS_AUTO, {"mode": "risk_based", "risk_based": {"max_risk": "medium"},
                                             "always_human": ["auth", "migrations"]})["auto_merge"]
    assert rb["enabled"] is True
    assert {"risk_tag": "medium"} in rb["requires_all"] and {"final_risk": "medium"} in rb["requires_all"]
    assert rb["requires_all"][-1] == {"always_human": ["auth", "migrations"]}
    assert FLOWS_AUTO["auto_merge"]["enabled"] is False  # not mutated
    bare = MP.apply_project_merge({}, {"mode": "risk_based"})["auto_merge"]["requires_all"]
    assert {"risk_tag": "low"} in bare and {"final_risk": "low"} in bare and {"ci_checks": "passing"} in bare


def _ev(labels, files):
    return MP.Evaluator("acme/a", "1", {}, pr_data={"labels": [{"name": n} for n in labels]}, files=files)


def test_risk_tag_at_most_and_always_human():
    assert MP.Evaluator.c_risk_tag(_ev(["risk:low"], []), "medium")[0]
    assert not MP.Evaluator.c_risk_tag(_ev(["risk:high"], []), "medium")[0]
    assert not MP.Evaluator.c_risk_tag(_ev(["risk:low", "risk:high"], []), "high")[0]
    assert not MP.Evaluator.c_risk_tag(_ev([], []), "low")[0]
    ok, why = _ev([], ["src/auth/login.py"]).c_always_human(["auth"])
    assert not ok and "always_human" in why
    assert _ev([], ["README.md"]).c_always_human(["auth", "pii"])[0]
    assert _ev([], ["db/migrations/1.sql"]).c_always_human(["pii"])[0]
    ev = _ev([], [])
    ev.files = lambda: None  # unreadable file list fails closed
    assert not ev.c_always_human(["auth"])[0]


def test_project_merge_reads_projects_json(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"repo": "Acme/A", "merge": {"mode": "risk_based"}}, {"repo": "acme/b"}]}))
    assert MP.project_merge("acme/a", str(p)) == {"mode": "risk_based"}
    assert MP.project_merge("acme/b", str(p)) == {}
    assert MP.project_merge("acme/a", str(tmp_path / "none.json")) == {}


def test_wip_limit_from_projects_json(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("SQUADHQ_HOME", str(tmp_path))
    monkeypatch.setattr(stuck_sweeper.flow_reader, "get_flood_guard", lambda name: {"value": 5})
    assert stuck_sweeper.wip_limit() == 5
    (tmp_path / "config" / "projects.json").write_text(json.dumps({"policies": {"wip_limit": 2}, "projects": []}))
    assert stuck_sweeper.wip_limit() == 2
    (tmp_path / "config" / "projects.json").write_text(json.dumps({"policies": {"wip_limit": True}}))
    assert stuck_sweeper.wip_limit() == 5
