import json
import os

from squadhq.config import init_wizard as W
from squadhq.config.gh import FakeGh, Result
from squadhq.config.loader import load_text

from .hubfix import make_hub


def contents(*names, dirs=()):
    return Result(0, json.dumps([{"name": n, "type": "file"} for n in names] +
                                [{"name": d, "type": "dir"} for d in dirs]))


def gh_for_repo():
    return FakeGh(routes={
        ("repo", "view"): Result(0, "acme\n"),
        ("api", "user"): Result(0, "alice\n"),
        ("api", "repos/acme/shop", "--jq"): Result(0, "trunk\n"),
        ("api", "repos/acme/shop/contents"): contents("pyproject.toml", "README.md", dirs=("web", ".github", "docs")),
        ("api", "repos/acme/shop/contents/web"): contents("package.json"),
        ("api", "users/acme"): Result(0, "Organization\n"),
        ("api", "users/alice"): Result(0, "User\n"),
    })


def test_detect_timezone(tmp_path):
    assert W.detect_timezone({"TZ": ":Europe/Paris"}) == "Europe/Paris"
    link = tmp_path / "localtime"
    target = tmp_path / "zoneinfo" / "Asia" / "Tokyo"
    target.parent.mkdir(parents=True)
    target.write_text("")
    os.symlink(target, link)
    assert W.detect_timezone({}, str(link)) == "Asia/Tokyo"
    assert W.detect_timezone({"TZ": "UTC"}, str(tmp_path / "missing")) == "UTC"


def test_detect_remote_packs(tmp_path):
    root = make_hub(tmp_path)
    packs, roots = W.detect_remote_packs(gh_for_repo(), "acme/shop", root)
    assert packs == ["node-next", "python-fastapi"]
    assert roots == {"node-next": "web"}
    empty = make_hub(tmp_path / "e", packs=())
    assert W.detect_remote_packs(gh_for_repo(), "acme/shop", empty) == ([], {})


def test_owner_helpers():
    gh = gh_for_repo()
    assert W.detect_owner(gh) == "acme" and W.detect_login(gh) == "alice"
    assert W.owner_is_org(gh, "acme") and not W.owner_is_org(gh, "alice")
    assert W.default_branch(gh, "acme/shop") == "trunk"
    assert W.default_branch(FakeGh(), "x/y") == "main"
    assert W.detect_owner(FakeGh()) == ""


def test_interview_and_yaml_roundtrip(tmp_path):
    root = make_hub(tmp_path)
    answers = {"Project repos": "acme/shop", "Approvers": "@acme/leads, @bob", "Monthly": "oops",
               "Timezone": "Europe/London", "Anthropic auth": "subscription"}

    def ask(q, default):
        return next((v for k, v in answers.items() if q.startswith(k)), default)

    a = W.interview(gh_for_repo(), ask, root=root)
    assert a.owner == "acme" and a.hub_repo == "acme/agent-squad-hq"
    assert a.approvers == ["@acme/leads", "@bob"] and a.operating_model == "team"
    assert a.monthly_usd == 100.0 and a.auth == "subscription"
    assert a.projects[0].branch == "trunk" and a.projects[0].pack_roots == {"node-next": "web"}
    text = W.generate_yaml(a)
    cfg, issues = load_text(text)
    assert cfg is not None, [str(i) for i in issues]
    assert cfg.projects[0].packs == ["node-next", "python-fastapi"]
    assert cfg.providers.configs["anthropic"].auth == "subscription"
    assert W.validate_generated(text, root) == []


def test_interview_with_defaults_solo():
    d = W.Answers(org_name="Solo", owner="me", hub_repo="me/hq", approvers=["@me"], monthly_usd=20, app_slug="me-sq",
                  projects=[W.ProjectAnswer(repo="me/app")])
    a = W.interview(FakeGh(), lambda q, default: default, defaults=d)
    assert a.operating_model == "solo" and a.app_slug == "me-sq" and a.monthly_usd == 20.0
    text = W.generate_yaml(a)
    assert "app: me-sq" in text
    assert load_text(text)[0] is not None


def test_generate_yaml_no_projects_and_fraction():
    a = W.Answers(org_name="A", owner="a", hub_repo="a/hq", approvers=["@a"], monthly_usd=12.5)
    text = W.generate_yaml(a)
    assert "projects: []" in text and "monthly_usd: 12.5" in text
    assert load_text(text)[0] is not None
    bad = W.Answers(org_name="A", owner="a", hub_repo="nope", approvers=["@a"])
    assert W.validate_generated(W.generate_yaml(bad))


def test_setup_plan_and_next_steps(tmp_path):
    root = make_hub(tmp_path)
    pem = tmp_path / "k.pem"
    pem.write_text("PEM")
    a = W.Answers(org_name="A", owner="acme", hub_repo="acme/hq", approvers=["@a"], auth="subscription",
                  projects=[W.ProjectAnswer(repo="acme/shop")])
    plan = W.setup_plan(a, None, pem_path=str(pem), client_id="Iv1", flows_path=f"{root}/config/flows.yml",
                        variables={"SQUADHQ_HUB_REPO": "acme/hq"})
    descs = [s.description for s in plan.steps]
    assert descs[0].startswith("secret SQUADHQ_APP_PRIVATE_KEY")
    assert "variable SQUADHQ_APP_CLIENT_ID" in descs
    assert any("label dept:backend on acme/shop" == d for d in descs)
    assert any("label daily-brief on acme/hq" == d for d in descs)
    assert not any("label daily-brief on acme/shop" == d for d in descs)
    assert any(d.startswith("create ruleset") for d in descs)
    plan2 = W.setup_plan(a, None, flows_path=str(tmp_path / "missing.yml"))
    assert all("label" not in s.description for s in plan2.steps)
    steps = W.next_steps(a, "https://github.com/apps/x")
    assert steps[0].endswith("/installations/new")
    assert any("claude setup-token" in s for s in steps)
    assert any(s == "squadhq onboard acme/shop --apply" for s in steps)
    assert steps[-1].startswith("squadhq drill")


def test_generate_yaml_with_an_account_pool():
    a = W.Answers(org_name="acme", owner="acme", hub_repo="acme/hub", approvers=["@octo"], auth="api",
                  pool=[W.PoolAnswer("account-1", "api", "ANTHROPIC_API_KEY"), W.PoolAnswer("account-2", "subscription")],
                  pool_strategy="round_robin")
    text = W.generate_yaml(a)
    assert W.validate_generated(text) == []
    cfg, _ = load_text(text)
    pool = cfg.providers.configs["anthropic"].pool
    assert [(p.id, p.auth, p.secret) for p in pool] == [("account-1", "api", "ANTHROPIC_API_KEY"),
                                                       ("account-2", "subscription", "CLAUDE_CODE_OAUTH_TOKEN_2")]
    assert cfg.providers.configs["anthropic"].pool_strategy == "round_robin"
    assert "pool:" not in W.generate_yaml(W.Answers(org_name="a", owner="a", hub_repo="a/h", approvers=["@a"]))
