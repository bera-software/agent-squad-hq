import pytest
import yaml

from squadhq.config import loader
from squadhq.config.loader import ConfigError, Issue, load, load_text, parse

from .hubfix import EXAMPLE, MIN_YAML, make_hub


def doc(**over):
    d = yaml.safe_load(MIN_YAML)
    for k, v in over.items():
        d[k] = v
    return d


def errors(data, **kw):
    cfg, issues = parse(data, **kw)
    return cfg, [i for i in issues if i.level == "error"], [i for i in issues if i.level == "warning"]


def paths(issues):
    return {i.path for i in issues}


def test_example_is_valid():
    with open(EXAMPLE) as f:
        cfg, issues = load_text(f.read())
    assert cfg is not None, [str(i) for i in issues]
    assert not [i for i in issues if i.level == "error"]
    assert cfg.org.name == "Acme"
    assert cfg.project("ACME/storefront").packs == ["node-next", "python-fastapi"]
    assert cfg.project("acme/nope") is None
    assert cfg.disabled_agents() == ["growth", "seo"]
    assert cfg.providers.configs["anthropic"].secret == "ANTHROPIC_API_KEY"
    assert cfg.providers.fallback_on_capacity_trip is False
    assert cfg.owner == "acme"
    assert str(cfg.notifications.escalations[1]) == "slack:#eng-alerts"
    assert str(cfg.providers.routing["heavy"].primary) == "anthropic/claude-sonnet-5-5"


def test_minimal_defaults():
    cfg, errs, _w = errors(doc())
    assert not errs
    assert cfg.hub.bot.login == "squadhq-ops[bot]"
    assert cfg.providers.default == "anthropic"
    assert set(cfg.providers.routing) == {"heavy", "light"}
    assert cfg.providers.jobs["implement"] == "heavy"
    assert cfg.policies.merge.mode == "approvers"
    assert cfg.schedules["daily_brief"] == "weekdays 07:30"
    assert cfg.notifications.digest[0].kind == "github_issue"


def test_required_top_level():
    _c, errs, _w = errors({})
    assert {"version", "org", "hub", "org.name", "org.approvers", "hub.repo"} <= paths(errs)


def test_not_a_mapping():
    _c, errs, _w = errors(["x"])
    assert errs[0].message.startswith("expected a mapping")


def test_unknown_key_suggestion():
    d = doc()
    d["org"]["timezon"] = "UTC"
    _c, errs, _w = errors(d)
    e = next(i for i in errs if i.path == "org.timezon")
    assert "did you mean 'timezone'" in e.hint


def test_bad_version_and_types():
    d = doc(version=2)
    d["org"]["working_hours"] = 450  # unquoted 07:30 style
    d["org"]["operating_model"] = "swarm"
    d["org"]["timezone"] = "Mars/Base"
    _c, errs, _w = errors(d)
    by = {i.path: i for i in errs}
    assert "version" in by
    assert "quote" in by["org.working_hours"].hint
    assert "allowed" in by["org.operating_model"].hint or "did you mean" in by["org.operating_model"].hint
    assert "org.timezone" in by


def test_boolean_string_hint():
    d = doc()
    d["hub"]["ref"] = True
    _c, errs, _w = errors(d)
    assert "booleans" in errs[0].hint


def test_working_hours_and_days_errors():
    d = doc()
    d["org"]["working_hours"] = "20:00-07:00"
    d["org"]["working_days"] = "someday"
    _c, errs, _w = errors(d)
    assert {"org.working_hours", "org.working_days"} <= paths(errs)


def test_approvers():
    d = doc()
    d["org"]["approvers"] = []
    _c, errs, _w = errors(d)
    assert "org.approvers" in paths(errs)
    d["org"]["approvers"] = ["not a login!"]
    _c, errs, _w = errors(d)
    assert "org.approvers[0]" in paths(errs)
    d["org"]["approvers"] = "@solo"
    cfg, errs, _w = errors(d)
    assert cfg.org.approvers == ["@solo"]
    d["org"]["approvers"] = {"a": 1}
    _c, errs, _w = errors(d)
    assert "org.approvers" in paths(errs)
    d["org"]["approvers"] = ["@a", "@a", 3]
    _c, errs, warns = errors(d)
    assert "org.approvers[2]" in paths(errs) and "org.approvers[1]" in paths(warns)


def test_hub_bot_string_and_invalid():
    d = doc()
    d["hub"]["bot"] = "acme-bot"
    cfg, _e, _w = errors(d)
    assert cfg.hub.bot.login == "acme-bot[bot]"
    d["hub"]["bot"] = {"app": "Bad_Slug"}
    d["hub"]["repo"] = "nope"
    d["hub"]["trigger"] = "squad"
    _c, errs, _w = errors(d)
    assert {"hub.bot.app", "hub.repo", "hub.trigger"} <= paths(errs)


def test_providers_routing_and_jobs():
    d = doc(providers={
        "default": "anthropic",
        "anthropic": {"auth": "subscription"},
        "ollama": {"base_url": "http://ollama:11434"},
        "routing": {"heavy": {"primary": "anthropic/claude-sonnet-5-5", "fallback": "ollama/qwen"},
                    "light": {"primary": "claude-haiku-5-5"},
                    "frontier": {"primary": "anthropic/claude-opus-5-5"}},
        "jobs": {"implement": "frontier", "made-up": "light"},
        "executor": "codex",
    })
    cfg, errs, warns = errors(d)
    assert not errs, errs
    assert cfg.providers.jobs["implement"] == "frontier"
    assert cfg.providers.routing["light"].primary.provider == "anthropic"
    assert cfg.providers.configs["ollama"].auth == "none"
    assert "providers.jobs.made-up" in paths(warns)


def test_providers_errors():
    d = doc(providers={
        "default": "openai",
        "anthropic": {"auth": "oauth"},
        "bogus": {},
        "routing": {"heavy": {"primary": "openai/gpt-5"}, "extra": {"primary": ""}},
        "jobs": {"review": "nope"},
    })
    _c, errs, _w = errors(d)
    p = paths(errs)
    assert {"providers.anthropic.auth", "providers.bogus", "providers.default", "providers.routing.light",
            "providers.routing.heavy.primary", "providers.jobs.review", "providers.routing.extra.primary"} <= p
    assert any(i.path.startswith("providers.jobs.") and "default tier" in i.message for i in errs)


def test_providers_empty_mapping_error():
    _c, errs, _w = errors(doc(providers={"executor": "claude-code"}))
    assert "providers" in paths(errs)


def test_model_ref_empty_model():
    _c, errs, _w = errors(doc(providers={"anthropic": {}, "routing": {"heavy": {"primary": "anthropic/"},
                                                                     "light": {"primary": "x"}}}))
    assert "providers.routing.heavy.primary" in paths(errs)


def test_budgets():
    d = doc(budgets={"monthly_usd": 50, "per_project_usd": {"default": 80, "Ghost": 5},
                     "per_run_usd": {"implement": 2, "nojob": 1, "default": 1}, "on_exceed": "explode"})
    _c, errs, warns = errors(d)
    assert "budgets.on_exceed" in paths(errs)
    assert {"budgets.per_project_usd.default", "budgets.per_run_usd.nojob", "budgets.per_project_usd.Ghost"} <= paths(warns)
    _c, errs, _w = errors(doc(budgets={"monthly_usd": -1, "per_run_usd": {"implement": "x"}}))
    assert {"budgets.monthly_usd", "budgets.per_run_usd.implement"} <= paths(errs)


def test_trackers():
    cfg, errs, _w = errors(doc(trackers={"default": "jira", "jira": {"site": "a.atlassian.net", "project": "ENG"}}))
    assert not errs and cfg.trackers.options["jira"]["project"] == "ENG"
    _c, errs, _w = errors(doc(trackers={"default": "linear"}))
    assert "trackers.default" in paths(errs)
    _c, errs, _w = errors(doc(trackers={"trello": {}}))
    assert "trackers.trello.board" in paths(errs)


def test_notifications():
    cfg, errs, _w = errors(doc(notifications={"escalations": "github_issue", "digest": ["email:a@b.co"]}))
    assert not errs and cfg.notifications.digest[0].target == "a@b.co"
    _c, errs, _w = errors(doc(notifications={"escalations": ["pager:x", "email", 5, "email:nope"], "digest": 3}))
    assert {"notifications.escalations[0]", "notifications.escalations[1]", "notifications.escalations[2]",
            "notifications.escalations[3]", "notifications.digest"} <= paths(errs)
    cfg, errs, _w = errors(doc(notifications={"escalations": ["slack", "pagerduty", "expo_push",
                                                              "discord:https://discord.com/api/webhooks/1/x"]}))
    assert not errs and [c.kind for c in cfg.notifications.escalations] == ["slack", "pagerduty", "expo_push",
                                                                           "discord"]


def test_schedules():
    cfg, errs, warns = errors(doc(schedules={"daily_brief": "weekdays 08:00", "made_up": "daily 01:00"}))
    assert not errs and cfg.schedules["daily_brief"] == "weekdays 08:00"
    assert "schedules.made_up" in paths(warns)
    _c, errs, _w = errors(doc(schedules={"daily_brief": "whenever", "debt_scan": 450}))
    assert {"schedules.daily_brief", "schedules.debt_scan"} <= paths(errs)


def test_agents():
    cfg, errs, warns = errors(doc(agents={"growth": False, "legal": {"enabled": True, "jurisdictions": ["UK"]},
                                          "wizard": {"enabled": False}, "seo": {"enabled": "no"}}))
    assert cfg is None
    assert "agents.seo.enabled" in paths(errs)
    assert "agents.wizard" in paths(warns)
    cfg, errs, _w = errors(doc(agents={"growth": False, "legal": {"jurisdictions": ["UK"]}}))
    assert cfg.agents["legal"].options == {"jurisdictions": ["UK"]}
    assert cfg.disabled_agents() == ["growth"]


def test_policies():
    cfg, errs, warns = errors(doc(policies={
        "wip_limit": 3, "review_fix_rounds": 0,
        "triggers": {"allowed_associations": ["OWNER", "NONE"]},
        "merge": {"mode": "auto", "risk_based": {"max_risk": "medium", "require_checks": ["sast"]},
                  "always_human": ["auth"]},
        "release": {"strategy": "release_tag"},
    }))
    assert not errs
    assert cfg.policies.merge.risk_based.max_risk == "medium"
    assert {"policies.triggers.allowed_associations", "policies.merge.mode"} <= paths(warns)
    _c, errs, _w = errors(doc(policies={"wip_limit": 0, "review_fix_rounds": "2",
                                        "triggers": {"allowed_associations": ["ADMIN"]},
                                        "merge": {"mode": "yolo", "always_human": ["everything"]},
                                        "release": {"strategy": "promote_branch"}}))
    assert {"policies.wip_limit", "policies.review_fix_rounds", "policies.triggers.allowed_associations[0]",
            "policies.merge.mode", "policies.merge.always_human[0]", "policies.release.promote_branch"} <= paths(errs)


def test_security():
    cfg, errs, _w = errors(doc(security={"gates": ["sast"], "show_agent_transcripts": True}))
    assert not errs and cfg.security.show_agent_transcripts
    _c, errs, _w = errors(doc(security={"gates": ["magic"], "show_agent_transcripts": "yes"}))
    assert {"security.gates[0]", "security.show_agent_transcripts"} <= paths(errs)


def test_projects_full():
    cfg, errs, _w = errors(doc(projects=[{
        "name": "Storefront", "repo": "acme/storefront", "branch": "develop",
        "packs": ["node-next"], "pack_roots": {"node-next": "web/"},
        "environments": {"staging": {"url": "https://s.acme.example", "api_url": "https://api.s.acme.example",
                                     "urls": {"mcp": "https://mcp.acme.example"}, "deploy": "fly"},
                         "production": {"url": "https://acme.example", "approval": "required"}},
        "product": {"brief_file": "docs/p.md", "markets": ["UK"], "keywords": ["k"], "notes": "n"},
        "agents": {"growth": {"enabled": True}}, "merge_mode": "risk_based", "budget_usd": 12,
    }]))
    assert not errs, errs
    p = cfg.projects[0]
    assert p.pack_roots == {"node-next": "web"}
    assert p.environments["staging"].urls == {"mcp": "https://mcp.acme.example"}
    assert p.budget_usd == 12.0


def test_projects_errors():
    _c, errs, warns = errors(doc(projects=[
        {"name": "A", "repo": "acme/a", "packs": ["Bad Pack", "ghost"], "pack_roots": {"other": "x", "ghost": "../up"},
         "environments": {"staging": {"url": "ftp://x", "urls": {"Bad": "https://x.example", "ok": "nope"},
                                      "deploy": "mainframe", "approval": "maybe"}},
         "merge_mode": "never", "budget_usd": "lots", "colour": "red"},
        {"name": "a", "repo": "ACME/A"},
        {"repo": "x"},
    ]), known_packs={"node-next"})
    p = paths(errs)
    assert {"projects[0].packs[0]", "projects[0].pack_roots.other", "projects[0].pack_roots.ghost",
            "projects[0].environments.staging.url", "projects[0].environments.staging.urls.Bad",
            "projects[0].environments.staging.urls.ok", "projects[0].environments.staging.deploy",
            "projects[0].environments.staging.approval", "projects[0].merge_mode", "projects[0].budget_usd",
            "projects[0].colour", "projects[1].name", "projects[1].repo", "projects[2].name",
            "projects[2].repo"} <= p
    unknown = next(i for i in errs if i.path == "projects[0].packs[1]")
    assert "unknown pack 'ghost'" in unknown.message


def test_projects_not_list_and_none():
    _c, errs, _w = errors(doc(projects={"a": 1}))
    assert "projects" in paths(errs)
    cfg, errs, _w = errors(doc(projects=None))
    assert cfg.projects == []


def test_unknown_pack_with_no_packs_is_error():
    cfg, errs, _w = errors(doc(), known_packs=set())
    assert cfg is None and any("no packs/ directory" in i.hint for i in errs)
    cfg, errs, _w = errors(doc(), known_packs={"node-next", "python-fastapi"})
    assert cfg is not None


def test_non_string_key():
    _c, errs, _w = errors({**doc(), 5: "x"})
    assert any("keys must be strings" in i.message for i in errs)


def test_yaml_syntax_error():
    cfg, issues = load_text("org: [unclosed\n")
    assert cfg is None and "YAML syntax error at line" in issues[0].message


def test_issue_str():
    s = str(Issue("a.b", "bad", "fix it"))
    assert s == "error: a.b: bad\n    hint: fix it"
    assert str(Issue("", "x", level="warning")).startswith("warning: <root>")


def test_load_file(tmp_path):
    root = make_hub(tmp_path, config_text=MIN_YAML)
    cfg = load(f"{root}/squadhq.yml", hub_root=root)
    assert cfg.projects[0].name == "Storefront"
    with pytest.raises(ConfigError) as e:
        load(f"{root}/missing.yml")
    assert "not found" in str(e.value)
    (tmp_path / "bad.yml").write_text("version: 1\n")
    with pytest.raises(ConfigError) as e:
        load(str(tmp_path / "bad.yml"))
    assert "org" in str(e.value)


def test_known_pack_ids(tmp_path):
    root = make_hub(tmp_path, packs=("node-next",))
    assert loader.known_pack_ids(root) == {"node-next"}
    assert loader.known_pack_ids(str(tmp_path / "nowhere")) is None


def test_deprecated_schedule_aliases():
    cfg, errs, warns = errors(doc(schedules={"seo_weekly": "mon 05:00", "growth_nightly": "daily 02:00",
                                             "legal_weekly": "wed 06:00", "legal": "thu 06:00"}))
    assert not errs
    assert cfg.schedules["seo"] == "mon 05:00" and cfg.schedules["growth"] == "daily 02:00"
    assert cfg.schedules["legal"] == "thu 06:00"  # canonical key wins over the alias
    assert "seo_weekly" not in cfg.schedules
    w = next(i for i in warns if i.path == "schedules.seo_weekly")
    assert "deprecated" in w.message and "schedules.seo" in w.hint


def test_canonical_schedule_keys():
    from squadhq.config import registry as R
    assert set(R.CORE_SCHEDULE_KEYS) == {"chief_of_staff", "daily_brief", "finance", "growth", "legal", "seo",
                                         "weekly_rollup", "stuck_sweeper", "hub_janitor", "debt_scan",
                                         "architect_quarterly", "ecosystem_watch", "lessons_harvester",
                                         "okr_tracker"}
    assert set(R.SKILL_SCHEDULE_KEYS) == {
        "uptime_watch", "weekly_dependencies", "weekly_dast", "weekly_test_health", "release_train",
        "finops_weekly", "quarterly_okr_draft", "weekly_feedback", "monthly_competitive", "weekly_prompt_eval",
        "monthly_model_routing", "compliance_monthly", "analytics_weekly", "docs_gardener", "weekly_localisation"}
    assert all(R.SCHEDULES[k][0] == "skill-scheduler.yml" for k in R.SKILL_SCHEDULE_KEYS)
    assert set(R.SCHEDULES) == set(R.CORE_SCHEDULE_KEYS) | set(R.SKILL_SCHEDULE_KEYS)
    assert not set(R.SCHEDULE_ALIASES) & set(R.SCHEDULES)
    assert set(R.SCHEDULE_ALIASES.values()) <= set(R.SCHEDULES)


@pytest.mark.parametrize("tz", ["Pacific/Pago_Pago", "America/Los_Angeles", "UTC", "Asia/Kolkata", "Asia/Tokyo",
                                "Australia/Sydney", "Pacific/Auckland", "Pacific/Kiritimati"])
def test_default_schedules_convert_in_every_zone(tz):
    d = doc()
    d["org"]["timezone"] = tz
    cfg, errs, _w = errors(d)
    assert cfg is not None and not errs, errs


def test_project_quality():
    cfg, errs, _w = errors(doc(projects=[
        {"name": "A", "repo": "acme/a", "quality": {"arch": "required", "lint": "default", "changed_coverage": "enforce"}},
        {"name": "B", "repo": "acme/b"},
    ]))
    assert not errs, errs
    a, b = cfg.projects
    assert (a.quality.arch, a.quality.lint, a.quality.changed_coverage) == ("required", "default", "enforce")
    assert (b.quality.arch, b.quality.lint, b.quality.changed_coverage) == ("auto", "auto", "report")
    _c, errs, _w = errors(doc(projects=[
        {"name": "A", "repo": "acme/a", "quality": {"arch": "always", "lint": True, "coverage": "x"}}]))
    assert {"projects[0].quality.arch", "projects[0].quality.lint", "projects[0].quality.coverage"} <= paths(errs)
