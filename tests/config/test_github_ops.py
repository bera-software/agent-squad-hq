import json

from squadhq.config import github_ops as ops
from squadhq.config.gh import FakeGh, Result

from .hubfix import make_hub


def test_label_registry_and_plan(tmp_path):
    root = make_hub(tmp_path)
    project, hub = ops.label_registry(f"{root}/config/flows.yml")
    assert project == [("dept:backend", "1D76DB", "Backend")]
    assert hub == [("daily-brief", "ededed", "")]
    plan = ops.Plan()
    ops.plan_labels(plan, "acme/a", project + hub)
    assert plan.steps[0].args == ["label", "create", "dept:backend", "-R", "acme/a", "--force", "--color",
                                  "1D76DB", "--description", "Backend"]
    assert "--description" not in plan.steps[1].args


def test_label_registry_default_path(tmp_path, monkeypatch):
    root = make_hub(tmp_path)
    monkeypatch.setenv("SQUADHQ_HOME", root)
    assert ops.label_registry()[0]


def test_ruleset_create_vs_update():
    body = ops.ruleset_body()
    assert body["rules"][-1]["parameters"]["required_status_checks"] == [{"context": "hub / ci-gate"}]
    plan = ops.Plan()
    ops.plan_ruleset(plan, None, "acme/a")
    assert plan.steps[0].args[:3] == ["api", "-X", "POST"]
    assert json.loads(plan.steps[0].input_text)["name"] == "squadhq"
    gh = FakeGh(routes={("api", "repos/acme/a/rulesets"): Result(0, json.dumps([{"id": 9, "name": "squadhq"}]))})
    plan = ops.Plan()
    ops.plan_ruleset(plan, gh, "acme/a")
    assert plan.steps[0].args == ["api", "-X", "PUT", "repos/acme/a/rulesets/9", "--input", "-"]
    assert ops.existing_ruleset_id(FakeGh(), "acme/a") is None
    gh2 = FakeGh(routes={("api",): Result(0, json.dumps([{"id": 1, "name": "other"}]))})
    assert ops.existing_ruleset_id(gh2, "acme/a") is None


def test_ruleset_requires_check():
    rules = [{"type": "pull_request"}, {"type": "required_status_checks",
                                        "parameters": {"required_status_checks": [{"context": "hub / ci-gate"}]}}]
    gh = FakeGh(routes={("api", "repos/acme/a", "--jq"): Result(0, "trunk\n"),
                        ("api", "repos/acme/a/rules/branches/trunk"): Result(0, json.dumps(rules))})
    assert ops.ruleset_requires_check(gh, "acme/a") is True
    gh = FakeGh(routes={("api", "repos/acme/a/rules/branches/main"): Result(0, "[]")})
    assert ops.ruleset_requires_check(gh, "acme/a") is False
    assert ops.ruleset_requires_check(FakeGh(), "acme/a") is None


def test_variables_secrets_and_apply(tmp_path):
    pem = tmp_path / "k.pem"
    pem.write_text("SECRET")
    plan = ops.Plan()
    ops.plan_variables(plan, {"A": "1", "EMPTY": ""}, "acme", "org", "acme/hq")
    ops.plan_secret_from_file(plan, "S", str(pem), "acme", "repo", "acme/hq")
    assert plan.steps[0].args == ["variable", "set", "A", "--body", "1", "--org", "acme", "--visibility", "all"]
    assert len(plan.steps) == 2
    assert plan.steps[1].args == ["secret", "set", "S", "-R", "acme/hq"]
    assert plan.steps[1].display().endswith("< (redacted)") and "SECRET" not in plan.steps[1].display()
    p2 = ops.Plan()
    p2.add("x", ["api", "x"], "body")
    assert p2.steps[0].display().endswith("< (stdin)")
    gh = FakeGh(routes={("variable",): Result(0), ("secret",): Result(1, "", "nope")})
    outcomes = plan.apply(gh)
    assert [o.result.ok for o in outcomes] == [True, False]
    assert gh.inputs[1] == "SECRET"


def test_list_names_merges_org_and_repo():
    gh = FakeGh(routes={
        ("secret", "list", "--org"): Result(0, json.dumps([{"name": "A", "updatedAt": "x"}])),
        ("secret", "list", "-R"): Result(0, json.dumps([{"name": "B"}, "junk"])),
    })
    assert set(ops.list_names(gh, "secret", "acme", "acme/hq")) == {"A", "B"}
    assert ops.list_names(FakeGh(), "variable", "acme", "acme/hq") == {}
