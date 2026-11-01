import json
from unittest import mock

import pytest

from squadhq import budget_gate as B


def row(cost, repo="o/app", billing="api", **kw):
    return {"body": "```json\n" + json.dumps({"cost_usd": cost, "repo": repo, "billing": billing, **kw}) + "\n```",
            "bot": True}


def by_bot(c):
    return c.get("bot")


def test_ledger_spend_counts_bot_api_rows_only():
    comments = [row(1.5), row(2.0, repo="o/other"), row(9, billing="subscription"),
                row(0, skipped_capacity=True), {**row(50), "bot": False}, {"body": "no json", "bot": True},
                {"body": "```json\n{bad\n```", "bot": True}, row(None)]
    total, per_repo = B.ledger_spend(comments, by_bot)
    assert total == 3.5 and per_repo == {"o/app": 1.5, "o/other": 2.0}


def test_hub_loop_spend_is_this_month_only():
    assert B.hub_loop_spend({"month": "2026-11", "est_cost_usd": 4.0}, "2026-11") == 4.0
    assert B.hub_loop_spend({"month": "2026-10", "est_cost_usd": 4.0}, "2026-11") == 0.0
    assert B.hub_loop_spend({}, "2026-11") == 0.0


def test_load_budgets(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"budgets": {"monthly_usd": 100, "per_run_usd": {"implement": 3, "x": "no"},
                                         "on_exceed": "notify"},
                             "projects": [{"repo": "O/App", "budget_usd": 40}, {"repo": "o/b"}]}))
    b = B.load_budgets(str(p), env={})
    assert (b.monthly_usd, b.per_run_usd, b.on_exceed, b.project_usd) == (100, {"implement": 3}, "notify",
                                                                           {"o/app": 40})
    assert B.load_budgets(str(p), env={"SQUADHQ_MONTHLY_BUDGET_USD": "25"}).monthly_usd == 25
    p.write_text(json.dumps({"budgets": {"on_exceed": "explode"}}))
    assert B.load_budgets(str(p), env={}).on_exceed == "pause"
    assert B.load_budgets(str(tmp_path / "missing.json"), env={}).monthly_usd is None


@pytest.mark.parametrize("spent,project,action", [(10, 1, "run"), (100, 1, "pause"), (10, 40, "pause")])
def test_decide_caps(spent, project, action):
    b = B.Budgets(monthly_usd=100, per_run_usd={"default": 2, "implement": 3}, project_usd={"o/app": 40})
    d = B.decide(b, "implement", "O/App", spent, project)
    assert d.action == action and d.max_run_usd == 3
    assert B.decide(b, "review", "o/app", 0, 0).max_run_usd == 2


def test_decide_on_exceed_modes():
    b = B.Budgets(monthly_usd=10, on_exceed="downgrade")
    assert B.decide(b, "j", "r", 10, 0, "claude-haiku-5-5").model == "claude-haiku-5-5"
    d = B.decide(b, "j", "r", 10, 0, "")
    assert d.action == "pause" and "no cheaper model" in d.reason
    b.on_exceed = "notify"
    assert B.decide(b, "j", "r", 10, 0).action == "notify"


def test_cheapest_model():
    models = {"tiers": {"heavy": {"primary": "big", "fallback": "mid"}, "light": {"primary": "small"},
                        "odd": "x"}}
    prices = {"big": 15.0, "mid": 3.0, "small": 1.0}
    assert B.cheapest_model(models, prices.get) == "small"
    assert B.cheapest_model({}, prices.get) == ""


def _check(tmp_path, env, comments, usage=None):
    cfg = tmp_path / "projects.json"
    cfg.write_text(json.dumps({"budgets": {"monthly_usd": 10, "per_run_usd": {"implement": 3}},
                               "projects": [{"repo": "o/app", "budget_usd": 5}]}))
    out, summary = tmp_path / "out", tmp_path / "summary"
    gh = mock.MagicMock()
    gh.find_issue_by_title.return_value = 9
    gh.paginate.return_value = comments
    base = {"JOB": "implement", "REPO": "o/app", "HUB_REPO": "o/hub", "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary)}
    with mock.patch.object(B, "hub_path", side_effect=lambda *p: str(tmp_path / p[-1])), \
            mock.patch("squadhq.github.client", return_value=gh), \
            mock.patch("squadhq.untrusted.by_bot", side_effect=by_bot), \
            mock.patch("squadhq.capacity_gate.read_hub_json", return_value=(usage or {}, None)), \
            mock.patch("squadhq.capacity_gate.dedupe_comment") as comment:
        d = B.check({**base, **env})
    return d, out.read_text(), gh, comment


def test_check_under_budget_sets_run_cap(tmp_path):
    d, out, gh, _ = _check(tmp_path, {"AUTH_MODE": "api"}, [row(1.0)])
    assert d.action == "run" and "auth_mode=api" in out and "max_budget_usd=3" in out
    gh.find_or_create_issue_by_title.assert_not_called()


def test_check_project_cap_pauses_and_notifies(tmp_path):
    d, out, gh, comment = _check(tmp_path, {"AUTH_MODE": "api", "ISSUE_OR_PR": "4", "COMMENT_ON_SKIP": "true"},
                                 [row(6.0)])
    assert d.action == "pause" and "auth_mode=skip" in out
    assert gh.find_or_create_issue_by_title.call_args.args[1].startswith("BUDGET CAP HIT")
    comment.assert_called_once()


def test_check_monthly_includes_hub_loops(tmp_path):
    d, _out, _gh, _c = _check(tmp_path, {"AUTH_MODE": "api"}, [row(1.0, repo="o/x")],
                              usage={"month": B.month(), "est_cost_usd": 9.5})
    assert d.action == "pause" and "monthly" in d.reason


def test_check_subscription_is_not_metered(tmp_path):
    d, out, gh, _ = _check(tmp_path, {"AUTH_MODE": "subscription"}, [row(100.0)])
    assert d.action == "run" and "auth_mode=subscription" in out and "max_budget_usd=\n" in out
    gh.paginate.assert_not_called()


def test_check_fails_open(tmp_path):
    cfg = tmp_path / "projects.json"
    cfg.write_text("{}")
    out = tmp_path / "out"
    with mock.patch.object(B, "hub_path", side_effect=lambda *p: str(tmp_path / p[-1])), \
            mock.patch("squadhq.capacity_gate.read_hub_json", side_effect=RuntimeError("down")):
        d = B.check({"AUTH_MODE": "api", "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(tmp_path / "s")})
    assert d.action == "run" and "auth_mode=api" in out.read_text()
    assert "not checked" in (tmp_path / "s").read_text()


def test_main_usage():
    assert B.main([]) == 2
