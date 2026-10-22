import json
from datetime import datetime, timezone

from squadhq import doctor
from squadhq.config import load_text
from squadhq.config import render as RN
from squadhq.config.gh import FakeGh, Result

from .hubfix import EXAMPLE, MIN_YAML, make_hub

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def example():
    return load_text(open(EXAMPLE).read())[0]


def statuses(checks):
    return {c.name: c.status for c in checks}


def full_gh(cfg, *, token_updated="2026-09-01T00:00:00Z", vars_override=None, selection="all",
            repos=None, rules_ok=True, installs_ok=True, approvals=1, last_push=True, agent_app=True):
    variables = [{"name": k, "value": v} for k, v in RN.repo_variables(cfg).items() if v]
    if agent_app:
        variables.append({"name": "SQUADHQ_AGENT_APP_CLIENT_ID", "value": "Iv-agent"})
    for k, v in (vars_override or {}).items():
        variables = [x for x in variables if x["name"] != k] + ([{"name": k, "value": v}] if v is not None else [])
    secrets = [{"name": "SQUADHQ_APP_PRIVATE_KEY", "updatedAt": token_updated},
               {"name": "CLAUDE_CODE_OAUTH_TOKEN", "updatedAt": token_updated}]
    if agent_app:
        secrets.append({"name": "SQUADHQ_AGENT_APP_PRIVATE_KEY", "updatedAt": token_updated})
    rules = [{"type": "required_status_checks",
              "parameters": {"required_status_checks": [{"context": "hub / ci-gate" if rules_ok else "x"}]}},
             {"type": "pull_request", "parameters": {"required_approving_review_count": approvals,
                                                     "require_last_push_approval": last_push}}]
    routes = {
        ("auth", "status"): Result(0),
        ("secret", "list", "--org"): Result(0, json.dumps(secrets)),
        ("variable", "list", "--org"): Result(0, json.dumps(variables)),
        ("api", f"orgs/{cfg.owner}/installations"): Result(0, json.dumps({"installations": [
            {"id": 5, "app_slug": cfg.hub.bot.app, "repository_selection": selection},
            {"id": 6, "app_slug": cfg.hub.agent_bot.app, "repository_selection": "all"}]})) if installs_ok else Result(1),
        ("api", "user/installations/5/repositories"): Result(0, json.dumps({"repositories": [
            {"full_name": r} for r in (repos or [])]})),
    }
    for p in cfg.projects:
        routes[("api", f"repos/{p.repo}/rules/branches/main")] = Result(0, json.dumps(rules))
    return FakeGh(routes=routes)


def test_all_green_except_missing_api_key():
    cfg = example()
    checks = doctor.check_github(cfg, full_gh(cfg), NOW)
    st = statuses(checks)
    assert st["gh authenticated"] == "ok"
    assert st["secret SQUADHQ_APP_PRIVATE_KEY — ops GitHub App private key (PEM): routing, state, merges"] == "ok"
    assert st["secret SQUADHQ_AGENT_APP_PRIVATE_KEY — agent GitHub App private key (PEM): coding agents"] == "ok"
    assert st["GitHub App acme-squad-agent installed on acme"] == "ok"
    assert st["ruleset on acme/storefront requires an approval after the last push"] == "ok"
    missing = [c for c in checks if c.name.startswith("secret ANTHROPIC_API_KEY missing")]
    assert missing and "gh secret set ANTHROPIC_API_KEY --org acme" in missing[0].fix
    assert any(c.name.startswith("CLAUDE_CODE_OAUTH_TOKEN set 50 days ago") and c.status == "ok" for c in checks)
    assert st["App acme-squad has access to all repositories"] == "ok"
    assert st["ruleset on acme/storefront requires `hub / ci-gate`"] == "ok"
    assert all(s == "ok" for n, s in st.items() if n.startswith("variable"))


def test_failures_and_fixes():
    cfg = example()
    gh = full_gh(cfg, token_updated="2025-10-01T00:00:00Z", vars_override={"SQUADHQ_TRIGGER": "@bot",
                                                                         "SQUADHQ_GATES": None},
                 selection="selected", repos=["acme/agent-squad-hq"], rules_ok=False)
    checks = doctor.check_github(cfg, gh, NOW)
    bad = {c.name: c for c in checks if c.status == "fail"}
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in n and "expiry" in n for n in bad)
    assert "variable SQUADHQ_TRIGGER = '@bot', squadhq.yml says '@squad'" in bad
    assert "variable SQUADHQ_GATES missing" in bad
    assert "App acme-squad cannot access acme/storefront" in bad
    assert "ruleset on acme/storefront does not require `hub / ci-gate`" in bad
    text, code = doctor.report(checks)
    assert code == 1 and "❌" in text and "fix:" in text


def test_token_near_expiry_warns_and_empty_var_ok():
    cfg = load_text(MIN_YAML)[0]
    checks = doctor.check_github(cfg, full_gh(cfg, token_updated="2025-11-01T00:00:00Z"), NOW)
    assert any("CLAUDE_CODE_OAUTH_TOKEN set" in c.name and c.status == "warn" for c in checks)
    assert any(c.name == "variable SQUADHQ_DISABLED_AGENTS (empty, not needed)" for c in checks)
    bad_ts = doctor._age_days("not a date", NOW)
    assert bad_ts is None


def test_app_not_installed_and_unreadable():
    cfg = example()
    gh = full_gh(cfg, installs_ok=False)
    assert any(c.status == "warn" and "installation" in c.name for c in doctor.check_github(cfg, gh, NOW))
    gh.routes[("api", "orgs/acme/installations")] = Result(0, json.dumps({"installations": []}))
    assert any(c.status == "fail" and "installed" in c.name for c in doctor.check_app_installation(cfg, gh))
    gh.routes[("api", "orgs/acme/installations")] = Result(0, json.dumps({"installations": [
        {"id": 5, "app_slug": "acme-squad", "repository_selection": "selected"}]}))
    gh.routes[("api", "user/installations/5/repositories")] = Result(1)
    assert any(c.status == "warn" and c.name.startswith("App access") for c in doctor.check_app_installation(cfg, gh))


def test_gh_missing_or_unauthenticated():
    cfg = example()
    assert doctor.check_github(cfg, FakeGh(present=False))[0].status == "skip"
    assert doctor.check_github(cfg, FakeGh())[0].status == "fail"


def test_ruleset_unreadable_warns():
    cfg = example()
    gh = full_gh(cfg)
    del gh.routes[("api", "repos/acme/storefront/rules/branches/main")]
    assert any(c.status == "warn" and c.name.startswith("ruleset") for c in doctor.check_github(cfg, gh, NOW))


def test_models_check():
    cfg = load_text(MIN_YAML.replace("projects:", "providers:\n  anthropic: {}\n  ollama: {}\n  routing:\n"
                                     "    heavy: {primary: anthropic/claude-sonnet-9, fallback: ollama/qwen}\n"
                                     "    light: {primary: claude-haiku-5-5}\nprojects:"))[0]
    st = statuses(doctor.check_models(cfg))
    assert st["model anthropic/claude-sonnet-9 (providers.routing.heavy.primary) is not a known anthropic model"] == "fail"
    assert st["model ollama/qwen (providers.routing.heavy.fallback)"] == "warn"
    assert st["model anthropic/claude-haiku-5-5 (providers.routing.light.primary)"] == "ok"


def test_run_offline(tmp_path):
    root = make_hub(tmp_path, config_text=MIN_YAML)
    checks = doctor.run(f"{root}/squadhq.yml", root, FakeGh(), offline=True)
    st = statuses(checks)
    assert st[f"config {root}/squadhq.yml valid"] == "ok"
    assert any(n.startswith("rendered files stale") for n in st)
    assert any(n.startswith("DST") for n in st)
    RN.write(RN.plan(load_text(MIN_YAML)[0], root))
    st2 = statuses(doctor.run(f"{root}/squadhq.yml", root, FakeGh(), offline=True))
    assert st2["rendered files fresh"] == "ok"
    st3 = statuses(doctor.run(f"{root}/squadhq.yml", root, FakeGh(present=False)))
    assert st3["gh CLI installed"] == "skip"


def test_run_missing_invalid_and_warning(tmp_path):
    root = make_hub(tmp_path)
    assert doctor.run(f"{root}/nope.yml", root, FakeGh())[0].status == "fail"
    (tmp_path / "bad.yml").write_text("version: 1\n")
    checks = doctor.run(str(tmp_path / "bad.yml"), root, FakeGh())
    assert checks[0].status == "fail" and "squadhq validate" in checks[0].fix
    (tmp_path / "w.yml").write_text(MIN_YAML + "agents:\n  wizard: {enabled: false}\n")
    checks = doctor.run(str(tmp_path / "w.yml"), root, FakeGh(), offline=True)
    assert any(c.status == "warn" and "agents.wizard" in c.name for c in checks)


def test_rendered_schedule_error(tmp_path):
    cfg = load_text(MIN_YAML)[0]
    cfg.org.timezone = "Asia/Tokyo"
    cfg.schedules["architect_quarterly"] = "quarterly 1st 01:00"
    assert doctor.check_rendered(cfg, str(tmp_path))[0].status == "fail"


def test_report_counts():
    text, code = doctor.report([doctor.Check("a", "ok"), doctor.Check("b", "warn", "do x")])
    assert code == 0 and "1 ok, 1 warning(s), 0 problem(s)" in text and "fix: do x" in text


def test_check_ids_cover_every_check(tmp_path):
    root = make_hub(tmp_path, config_text=MIN_YAML + "agents:\n  wizard: {enabled: false}\n")
    cfg = load_text(MIN_YAML)[0]
    checks = doctor.run(f"{root}/squadhq.yml", root, full_gh(cfg, token_updated="2026-09-01T00:00:00Z"))
    ids = {c.id for c in checks}
    assert "" not in ids
    assert ids <= set(doctor.CHECK_IDS)
    assert {"config", "rendered", "schedule-markers", "dst", "models", "schedules", "gh", "secrets",
            "token-expiry", "variables", "app", "rulesets", "agent-app", "pool"} == ids


def test_run_single_check(tmp_path):
    root = make_hub(tmp_path, config_text=MIN_YAML)
    cfg = load_text(MIN_YAML)[0]
    gh = full_gh(cfg)
    only = doctor.run(f"{root}/squadhq.yml", root, gh, only="models")
    assert only and all(c.id == "models" for c in only)
    assert gh.calls == []  # a local check never touches GitHub
    rules = doctor.run(f"{root}/squadhq.yml", root, gh, only="rulesets", offline=True)
    assert rules and all(c.id == "rulesets" for c in rules)
    empty = doctor.run(f"{root}/squadhq.yml", root, gh, only="token-expiry")
    assert all(c.id == "token-expiry" for c in empty)
    nothing = doctor.run(f"{root}/squadhq.yml", root, FakeGh(present=False), only="variables")
    assert nothing[0].status == "ok" and "nothing to check" in nothing[0].name
    (tmp_path / "bad.yml").write_text("version: 1\n")
    bad = doctor.run(str(tmp_path / "bad.yml"), root, gh, only="models")
    assert bad[0].id == "config" and bad[0].status == "fail"
    import pytest
    with pytest.raises(ValueError, match="unknown check"):
        doctor.run(f"{root}/squadhq.yml", root, gh, only="nope")


def test_agent_app_and_ruleset_failures():
    cfg = example()
    checks = doctor.check_github(cfg, full_gh(cfg, agent_app=False, approvals=0, last_push=False), NOW)
    bad = {c.name: c for c in checks if c.status == "fail"}
    assert any("SQUADHQ_AGENT_APP_CLIENT_ID missing" in n for n in bad)
    assert "secret SQUADHQ_AGENT_APP_PRIVATE_KEY missing" in bad
    assert any("without an independent approval (approvals=0" in n for n in bad)
    assert all(c.id for c in checks)


def test_single_app_mode_warns(tmp_path):
    cfg = load_text(MIN_YAML.replace("hub:\n  repo: acme/agent-squad-hq",
                                     "hub:\n  repo: acme/agent-squad-hq\n  single_app: true"))[0]
    assert cfg.hub.single_app
    c = doctor.check_agent_config(cfg)
    assert c.status == "warn" and "auto, risk_based and trivial merges are disabled" in c.name
    checks = doctor.check_github(cfg, full_gh(cfg, agent_app=False), NOW)
    assert not any(c.id == "agent-app" and c.status == "fail" for c in checks)
    assert doctor.check_agent_config(load_text(MIN_YAML)[0]).status == "ok"
