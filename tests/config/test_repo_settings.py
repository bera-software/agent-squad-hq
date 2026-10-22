"""`squadhq repo-settings`: admin repo settings only, for hosted / token-broker installs."""
import json

import pytest

from squadhq import cli
from squadhq.config import github_ops as ops
from squadhq.config.gh import FakeGh, Result

PLAN_LIMIT = Result(1, "", "gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)")


def fake(*, hub_private=True, owner_type="Organization", access="none", merge=False, project_private=False,
         ruleset_id=None, ruleset_post=Result(0, "{}")):
    def repo(name, private, merge_ok):
        return Result(0, json.dumps({"full_name": name, "private": private,
                                     "visibility": "private" if private else "public",
                                     "owner": {"type": owner_type},
                                     "allow_auto_merge": merge_ok, "allow_squash_merge": merge_ok}))
    rulesets = [{"id": ruleset_id, "name": "squadhq"}] if ruleset_id else []
    return FakeGh(routes={
        ("auth", "status"): Result(0),
        ("api", "repos/acme/hub"): repo("acme/hub", hub_private, False),
        ("api", "repos/acme/hub/actions/permissions/access", "--jq"): Result(0, access + "\n"),
        ("api", "repos/acme/shop"): repo("acme/shop", project_private, merge),
        ("api", "repos/acme/shop/rulesets", "--paginate"): Result(0, json.dumps(rulesets)),
        ("api", "-X", "PUT", "repos/acme/hub/actions/permissions/access"): Result(0),
        ("api", "-X", "PATCH", "repos/acme/shop"): Result(0, "{}"),
        ("api", "-X", "POST", "repos/acme/shop/rulesets"): ruleset_post,
        ("api", "-X", "PUT", f"repos/acme/shop/rulesets/{ruleset_id}"): Result(0, "{}"),
    })


def run(capsys, monkeypatch, gh, *argv):
    monkeypatch.setattr(cli, "GH", gh)
    code = cli.main(["repo-settings", "--hub", "acme/hub", "--project", "acme/shop", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def mutations(gh):
    return [c for c in gh.calls if "-X" in c]


def test_dry_run_prints_the_plan_and_changes_nothing(capsys, monkeypatch):
    gh = fake()
    code, out, _ = run(capsys, monkeypatch, gh)
    assert code == 0 and "# dry run: 3 GitHub step(s)" in out
    assert "gh api -X PUT repos/acme/hub/actions/permissions/access -f access_level=organization" in out
    assert "gh api -X PATCH repos/acme/shop -F allow_auto_merge=true -F allow_squash_merge=true" in out
    assert "gh api -X POST repos/acme/shop/rulesets --input - < (stdin)" in out
    assert mutations(gh) == []


def test_apply_sends_the_init_ruleset(capsys, monkeypatch):
    gh = fake(owner_type="User")
    code, out, _ = run(capsys, monkeypatch, gh, "--apply")
    assert code == 0 and "3/3 GitHub steps" in out
    assert ["api", "-X", "PUT", "repos/acme/hub/actions/permissions/access", "-f", "access_level=user"] in gh.calls
    body = json.loads(gh.inputs[gh.calls.index(["api", "-X", "POST", "repos/acme/shop/rulesets", "--input", "-"])])
    assert body == ops.ruleset_body()
    rules = {r["type"]: r.get("parameters", {}) for r in body["rules"]}
    assert rules["pull_request"]["required_approving_review_count"] == 1
    assert rules["pull_request"]["require_last_push_approval"] is True
    assert rules["required_status_checks"]["required_status_checks"] == [{"context": "hub / ci-gate"}]


def test_idempotent(capsys, monkeypatch):
    gh = fake(access="organization", merge=True, ruleset_id=7)
    code, out, _ = run(capsys, monkeypatch, gh, "--apply")
    assert code == 0
    assert "Actions access already `organization`" in out and "already allowed" in out
    # the ruleset is updated in place, never duplicated
    assert mutations(gh) == [["api", "-X", "PUT", "repos/acme/shop/rulesets/7", "--input", "-"]]


def test_public_hub_needs_no_access_change(capsys, monkeypatch):
    gh = fake(hub_private=False, merge=True, ruleset_id=7)
    code, out, _ = run(capsys, monkeypatch, gh)
    assert code == 0 and "acme/hub is public" in out and "access_level" not in out


def test_ruleset_plan_limit_warns_and_the_rest_applies(capsys, monkeypatch):
    gh = fake(project_private=True, ruleset_post=PLAN_LIMIT)
    code, out, _ = run(capsys, monkeypatch, gh, "--apply")
    assert code == 0
    assert "acme/shop is private: rulesets on private repositories need GitHub Pro" in out
    assert "⚠️  create ruleset 'squadhq' on acme/shop (requires `hub / ci-gate`): rulesets on private" in out
    assert "✅ allow auto-merge and squash merging on acme/shop" in out


def test_other_failures_fail(capsys, monkeypatch):
    gh = fake(ruleset_post=Result(1, "", "gh: Not Found (HTTP 404)"))
    code, out, _ = run(capsys, monkeypatch, gh, "--apply")
    assert code == 1 and "❌ create ruleset 'squadhq' on acme/shop" in out


def test_apply_needs_gh_and_valid_names(capsys, monkeypatch):
    code, _o, err = run(capsys, monkeypatch, FakeGh(present=False), "--apply")
    assert code == 1 and "gh auth login" in err
    monkeypatch.setattr(cli, "GH", fake())
    assert cli.main(["repo-settings", "--hub", "hub-only"]) == 2
    assert "not owner/name: hub-only" in capsys.readouterr().err


def test_dry_run_without_gh_lists_every_step(capsys, monkeypatch):
    code, out, _ = run(capsys, monkeypatch, FakeGh(present=False))
    assert code == 0 and "current settings were not read" in out and "3 GitHub step(s)" in out


def test_never_touches_config_secrets_or_variables(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    gh = fake()
    run(capsys, monkeypatch, gh, "--apply")
    assert not list(tmp_path.iterdir())
    assert not [c for c in gh.calls if c[0] in ("secret", "variable", "label")]


@pytest.mark.parametrize("text,limit", [(PLAN_LIMIT.stderr, True), ("gh: Not Found (HTTP 404)", False)])
def test_is_plan_limit(text, limit):
    assert ops.is_plan_limit(Result(1, "", text)) is limit
