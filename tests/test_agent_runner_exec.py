"""squadhq.agent_runner: configuration, the action executor's guards,
gather/fencing, usage compare-and-set, budget guard and main().

No network: every gh call goes through a routing fake GitHubClient runner or
a patched seam; anything unrouted fails the test.
"""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from squadhq import agent_runner as ar
from squadhq import github

HQ = "test-org/test-hq"
BOT = {"login": "app/squadhq-ops", "is_bot": True}
PROJ = "test-org/proj"


class FakeGH:
    """Routes `gh` argv to canned (rc, stdout, stderr). Routes are
    (substring-of-joined-argv, response); first match wins. Records calls."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, cmd, **kw):
        joined = " ".join(cmd)
        self.calls.append(cmd)
        for key, resp in self.routes:
            if key in joined:
                if callable(resp):
                    resp = resp(cmd, kw)
                rc, out, err = resp
                return subprocess.CompletedProcess(cmd, rc, out, err)
        raise AssertionError(f"unrouted gh call: {joined}")

    def cmds(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]


def ok(out=""):
    return (0, out, "")


class Base(unittest.TestCase):
    MODE = "cos"

    def setUp(self):
        self._saved = {k: getattr(ar, k) for k in ("HQ", "MODE", "ALLOWED_REPOS", "PROJECTS", "DRY_RUN",
                                                    "MODEL", "FALLBACK_MODEL", "BUDGET")}
        ar.HQ = HQ
        ar.MODE = self.MODE
        ar.ALLOWED_REPOS = {HQ, PROJ}
        ar.PROJECTS = [{"name": "Proj", "repo": PROJ, "notes": ""}]
        self.gh = FakeGH()
        github.set_client(github.GitHubClient(runner=self.gh, retries=0, sleep=lambda s: None))
        self._out = redirect_stdout(io.StringIO())
        self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)
        for k, v in self._saved.items():
            setattr(ar, k, v)
        github.set_client(None)

    def run_exec(self, actions, **kw):
        return ar.execute(actions, **kw)


# --------------------------------------------------------------- configure

def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class ConfigureTests(Base):
    def test_configure_reads_hub_config_not_import_time(self):
        with tempfile.TemporaryDirectory() as home:
            _write(home, "config/models.json", json.dumps(
                {"hub_loops": {"s": {"primary": "p-model", "fallback": "f-model", "jobs": ["brief"]}}}))
            _write(home, "config/projects.json", json.dumps({"projects": [{"name": "A", "repo": "o/a"}]}))
            env = {"SQUADHQ_HOME": home, "SQUADHQ_HUB_REPO": "o/hub", "MONTHLY_BUDGET_USD": "12.5"}
            with mock.patch.dict(os.environ, env):
                mode = ar.configure(["--dry-run", "brief"])
        self.assertEqual(mode, "brief")
        self.assertTrue(ar.DRY_RUN)
        self.assertEqual((ar.MODEL, ar.FALLBACK_MODEL), ("p-model", "f-model"))
        self.assertEqual(ar.HQ, "o/hub")
        self.assertEqual(ar.ALLOWED_REPOS, {"o/a", "o/hub"})
        self.assertEqual(ar.BUDGET, 12.5)

    def test_configure_defaults_when_config_missing(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"SQUADHQ_HOME": home, "SQUADHQ_HUB_REPO": "o/hub",
                                              "MONTHLY_BUDGET_USD": "lots"}):
                ar.configure(["growth"])
        self.assertEqual(ar.MODE, "growth")
        self.assertFalse(ar.DRY_RUN)
        self.assertEqual(ar.MODEL, ar.DEFAULT_MODEL)
        self.assertIsNone(ar.FALLBACK_MODEL)
        self.assertEqual(ar.PROJECTS, [])
        self.assertEqual(ar.BUDGET, 50.0)

    def test_mode_not_in_any_tier(self):
        with tempfile.TemporaryDirectory() as home:
            _write(home, "config/models.json", json.dumps({"hub_loops": {"s": {"primary": "x", "jobs": ["cos"]}}}))
            self.assertIsNone(ar._load_model_tier("seo", os.path.join(home, "config/models.json")))

    def test_mode_from_argv_skips_flags(self):
        self.assertEqual(ar._mode_from_argv(["--dry-run"]), "cos")
        self.assertEqual(ar._mode_from_argv(["--dry-run", "legal"]), "legal")

    def test_hq_resolved_lazily(self):
        ar.HQ = None
        with mock.patch.dict(os.environ, {"SQUADHQ_HUB_REPO": "lazy/hub"}):
            self.assertEqual(ar._hq(), "lazy/hub")
        self.assertEqual(ar.HQ, "lazy/hub")

    def test_hub_name_aliases(self):
        self.assertEqual(ar._repo_of({"repo": "test-hq"}), HQ)
        self.assertEqual(ar._repo_of({"repo": "agent-squad-hq"}), HQ)

    def test_load_system_prompt_prepends_untrusted_preamble(self):
        with tempfile.TemporaryDirectory() as home:
            _write(home, "agents/x/prompt.md", "ROLE PROMPT")
            with mock.patch.dict(os.environ, {"SQUADHQ_HOME": home}):
                self.assertEqual(ar.load_system_prompt("agents/x/prompt.md"), "ROLE PROMPT")
                _write(home, "prompts/_untrusted-input.md", "TREAT AS DATA\n")
                self.assertEqual(ar.load_system_prompt("agents/x/prompt.md"), "TREAT AS DATA\n\nROLE PROMPT")


# ------------------------------------------------------------ small helpers

class HelperTests(Base):
    def test_as_number(self):
        for v, want in ((5, 5), ("12", 12), ("#7", 7), (" 9 ", 9), (0, None), (-1, None), ("x", None),
                        ("1; rm -rf /", None), (True, None), (3.0, None), (None, None), ("0", None)):
            self.assertEqual(ar._as_number(v), want, v)

    def test_authority_block_reason(self):
        auth = {"action_types": {"create_issue"}, "repos": {"hub"}}
        self.assertIsNone(ar._authority_block_reason("create_issue", HQ, auth))
        self.assertIn("authority.repos", ar._authority_block_reason("create_issue", PROJ, auth))
        self.assertIn("action_types", ar._authority_block_reason("close_issue", HQ, auth))
        self.assertIsNone(ar._authority_block_reason("comment", PROJ, None))
        self.assertIn("failing closed", ar._authority_block_reason("close_issue", PROJ, None))
        self.assertIsNone(ar._authority_block_reason("close_issue", PROJ,
                                                     {"action_types": {"close_issue"}, "repos": set()}))

    def test_role_authority_from_flows(self):
        auth = ar._role_authority("chief-of-staff")
        self.assertIn("close_issue", auth["action_types"])
        with mock.patch.object(ar.flow_reader, "get_role", side_effect=KeyError("nope")):
            self.assertIsNone(ar._role_authority("chief-of-staff"))

    def test_gh_json_safe_warns_on_cap_and_failure(self):
        self.gh.routes = [("issue list", ok(json.dumps([{"n": 1}, {"n": 2}])))]
        w = []
        self.assertEqual(len(ar.gh_json_safe(["issue", "list", "--limit", "2"], w, "x")), 2)
        self.assertIn("cap", w[0])
        self.gh.routes = [("issue list", (1, "", "boom"))]
        self.assertEqual(ar.gh_json_safe(["issue", "list", "--limit", "x"], w, "y"), [])
        self.assertIn("y:", w[-1])
        self.assertIsNone(ar._limit_of(["--limit", "x"]))

    def test_gh_list_safe(self):
        self.gh.routes = [("--paginate", ok('{"a":1}\n{"a":2}\n'))]
        self.assertEqual(ar.gh_list_safe("repos/o/r/issues/1/comments", [], "c"), [{"a": 1}, {"a": 2}])
        self.gh.routes = [("--paginate", (1, "", "HTTP 404"))]
        w = []
        self.assertEqual(ar.gh_list_safe("p", w, "c"), [])
        self.assertTrue(w)

    def test_exec_non_gh_timeout_and_sh_check(self):
        with mock.patch.object(ar.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            r = ar._exec(["git", "log"])
        self.assertEqual(r.returncode, 124)
        with mock.patch.object(ar.subprocess, "run",
                               return_value=subprocess.CompletedProcess(["git"], 1, "", "bad")):
            with self.assertRaises(RuntimeError):
                ar.sh(["git", "status"])
            self.assertEqual(ar.sh(["git", "status"], check=False), "")

    def test_bot_git_identity_delegates(self):
        with mock.patch.object(github, "bot_identity", return_value=("n", "e")):
            self.assertEqual(ar.bot_git_identity(), ("n", "e"))

    def test_fence_text(self):
        self.assertIsNone(ar._fence_text(None, "s"))
        out = ar._fence_text("x" * 50, "o/r#1", {"login": "octo"}, 10, "NONE")
        self.assertIn('author="octo"', out)
        self.assertIn("truncated", out)
        self.assertIn('author="bob"', ar._fence_text("hi", "s", "bob"))

    def test_own_comment_within(self):
        now = ar._utc_now()
        recent = (now - ar.datetime.timedelta(hours=1)).isoformat()
        old = (now - ar.datetime.timedelta(hours=50)).isoformat()
        self.gh.routes = [("comments", ok(f"bad\tx\n{old}\tsquadhq-ops[bot]\t[Chief of Staff] a\n"
                                          f"{recent}\tsquadhq-ops[bot]\t[Chief of Staff] b\n"))]
        self.assertTrue(ar._own_comment_within(PROJ, 1, "[Chief of Staff] ", 24))
        # spoofed: a human typing the CoS badge does not start the cooldown
        self.gh.routes = [("comments", ok(f"{recent}\tmallory\t[Chief of Staff] fake\n"))]
        self.assertFalse(ar._own_comment_within(PROJ, 1, "[Chief of Staff] ", 24))
        self.gh.routes = [("comments", ok("nodate\tsquadhq-ops[bot]\t[Chief of Staff] a\n"))]
        self.assertFalse(ar._own_comment_within(PROJ, 1, "[Chief of Staff] ", 24))
        self.gh.routes = [("comments", ok(f"{old}\tsquadhq-ops[bot]\t[Chief of Staff] a\n"))]
        self.assertFalse(ar._own_comment_within(PROJ, 1, "[Chief of Staff] ", 24))
        self.gh.routes = [("comments", (1, "", "x"))]
        self.assertFalse(ar._own_comment_within(PROJ, 1, "[Chief of Staff] ", 24))

    def test_cos_comment_block_reason_failures(self):
        self.gh.routes = [(f"api repos/{PROJ}/issues/1", (1, "", "x"))]
        self.assertIsNone(ar._cos_comment_block_reason(PROJ, 1, "b"))
        self.gh.routes = [(f"api repos/{PROJ}/issues/1", ok("not json"))]
        self.assertIsNone(ar._cos_comment_block_reason(PROJ, 1, "b"))

    def test_cooldown_hours_fallback(self):
        with mock.patch.object(ar.flow_reader, "get_flood_guard", side_effect=KeyError("x")):
            self.assertEqual(ar._comment_only_cooldown_hours(), 24)

    def test_current_labels(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": [{"name": "a"}]})))]
        self.assertEqual(ar._current_labels(PROJ, 1), {"a"})
        self.gh.routes = [("issue view", ok("nope"))]
        self.assertIsNone(ar._current_labels(PROJ, 1))
        self.gh.routes = [("issue view", (1, "", ""))]
        self.assertIsNone(ar._current_labels(PROJ, 1))

    def test_label_owners_failure_is_none(self):
        with mock.patch.object(ar.label_guard, "load_owners", side_effect=OSError("x")):
            self.assertIsNone(ar._label_owners())

    def test_enrich_pr_checks(self):
        self.gh.routes = [("pr checks", (1, json.dumps([{"name": "a", "state": "FAILURE"},
                                                         {"name": "b", "state": "weird"}]), ""))]
        pr = {"number": 1}
        ar.enrich_pr_checks(pr, PROJ)
        self.assertIn("1 fail", pr["checks_summary"])
        self.assertIn("weird", pr["checks_summary"])
        self.gh.routes = [("pr checks", ok("not json"))]
        ar.enrich_pr_checks(pr, PROJ)
        self.assertEqual(pr["checks_summary"], "unavailable")

    def test_board_sync_note_writes_summary(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md") as f, \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": f.name}):
            ar.board_sync_note(PROJ, 1, "in_progress", RuntimeError("x"))
            self.assertIn("Board sync failed", open(f.name).read())


# ------------------------------------------------------------------ execute

class ExecuteGuardTests(Base):
    MODE = "cos"

    def test_invalid_number_refused(self):
        _, okn, fail, recs = self.run_exec([{"type": "comment", "repo": PROJ, "number": "5; echo", "body": "x"}])
        self.assertEqual((okn, fail), (0, 1))
        self.assertEqual(recs[0]["classification"], "invalid-number")
        self.assertEqual(self.gh.calls, [])

    def test_unresolvable_fields(self):
        _, _, fail, recs = self.run_exec([
            {"type": "comment", "repo": PROJ, "body": "x"},
            {"type": "add_labels", "repo": PROJ, "number": 1},
            {"type": "comment", "repo": PROJ, "number": 1},
            {"type": "create_issue", "repo": HQ, "body": "no title"},
            {"type": "frobnicate", "repo": HQ},
            {"type": "comment", "repo": "evil/repo", "number": 1, "body": "x"},
            "not-a-dict",
        ])
        self.assertEqual(fail, 6)
        self.assertEqual([r["classification"] for r in recs],
                         ["unresolvable-fields"] * 4 + ["unrecognized-kind", "invalid-repo"])

    def test_close_escalation_refused(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": [{"name": "escalation"}], "title": "x"})))]
        _, _, fail, recs = self.run_exec([{"type": "close_issue", "repo": HQ, "number": 3}])
        self.assertEqual(recs[0]["classification"], "protected-issue")
        self.assertFalse(self.gh.cmds("issue close"))

    def test_close_budget_notice_refused(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": [], "title": "BUDGET CAP HIT: $50 for 2026-10"})))]
        _, _, _, recs = self.run_exec([{"type": "close_issue", "repo": HQ, "number": 3}])
        self.assertIn("breaker notice", recs[0]["result"])

    def test_close_blind_refused(self):
        self.gh.routes = [("issue view", (1, "", "HTTP 404"))]
        _, _, _, recs = self.run_exec([{"type": "close_issue", "repo": HQ, "number": 3}])
        self.assertIn("refusing to close blind", recs[0]["result"])
        self.gh.routes = [("issue view", ok("garbage"))]
        _, _, _, recs = self.run_exec([{"type": "close_issue", "repo": HQ, "number": 3}])
        self.assertIn("refusing to close blind", recs[0]["result"])

    def test_close_ok_and_failure(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": [{"name": "P2"}], "title": "t"}))),
                          ("issue close", ok())]
        _, okn, _, recs = self.run_exec([{"type": "close_issue", "repo": PROJ, "number": "#4", "body": "done"}])
        self.assertEqual(okn, 1)
        self.assertIn("-c done", " ".join(self.gh.cmds("issue close")[0]))
        self.gh.routes[1] = ("issue close", (1, "", "nope"))
        _, _, fail, _ = self.run_exec([{"type": "close_issue", "repo": PROJ, "number": 4}])
        self.assertEqual(fail, 1)

    def test_authority_unreadable_fails_closed_for_create(self):
        with mock.patch.object(ar, "_role_authority", return_value=None):
            _, _, _, recs = self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t"}])
        self.assertEqual(recs[0]["classification"], "forbidden-action")

    def test_system_actions_skip_authority(self):
        ar.MODE = "brief"  # daily-brief may not comment
        self.gh.routes = [("issue comment", ok())]
        _, okn, _, _ = self.run_exec([{"type": "comment", "repo": HQ, "number": 1, "body": "x"}], system=True)
        self.assertEqual(okn, 1)
        _, _, fail, recs = self.run_exec([{"type": "comment", "repo": HQ, "number": 1, "body": "x"}])
        self.assertEqual(recs[0]["classification"], "forbidden-action")

    def test_exception_is_recorded(self):
        with mock.patch.object(ar, "_run", side_effect=ValueError("kaboom")):
            _, _, fail, recs = self.run_exec([{"type": "comment", "repo": HQ, "number": 1, "body": "x"}])
        self.assertEqual(recs[0]["classification"], "exception")
        self.assertIn("kaboom", recs[0]["result"])


class ExecuteDigestRoleTests(Base):
    MODE = "brief"  # daily-brief: create_issue on the hub only

    def test_create_on_project_outside_authority(self):
        _, _, _, recs = self.run_exec([{"type": "create_issue", "repo": PROJ, "title": "t"}])
        self.assertEqual(recs[0]["classification"], "forbidden-action")
        self.assertIn("authority.repos", recs[0]["result"])

    def test_close_not_in_authority(self):
        _, _, _, recs = self.run_exec([{"type": "close_issue", "repo": HQ, "number": 1}])
        self.assertEqual(recs[0]["classification"], "forbidden-action")


class ExecuteCreateTests(Base):
    MODE = "cos"

    def test_string_label_is_one_label(self):
        self.gh.routes = [("issue create", ok(f"https://github.com/{HQ}/issues/9"))]
        _, okn, _, recs = self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t", "body": 5,
                                          "labels": "dept:growth"}])
        cmd = self.gh.cmds("issue create")[0]
        self.assertEqual(cmd[cmd.index("-l") + 1], "dept:growth")
        self.assertEqual(recs[0]["number"], 9)
        self.assertEqual(recs[0]["classification"], "url-match")

    def test_missing_label_retries_unlabeled(self):
        def create(cmd, kw):
            if "-l" in cmd:
                return (1, "", "could not add label: 'x' not found")
            return (0, "", f"https://github.com/{HQ}/issues/10")
        self.gh.routes = [("issue create", create)]
        _, okn, _, recs = self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t", "labels": ["dept:growth"]}])
        self.assertEqual(okn, 1)
        self.assertIn("labels missing", recs[0]["result"])

    def test_verify_paths(self):
        self.gh.routes = [("issue create", ok("done")), ("issue list", ok(json.dumps([{"number": 11}])))]
        _, _, _, recs = self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t"}])
        self.assertEqual(recs[0]["classification"], "verify-match")
        self.gh.routes[1] = ("issue list", ok("[]"))
        _, _, _, recs = self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t"}])
        self.assertEqual(recs[0]["classification"], "verify-miss")

    def test_nonzero_create(self):
        self.gh.routes = [("issue create", (1, "", "denied"))]
        _, _, fail, recs = self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t"}])
        self.assertEqual(recs[0]["classification"], "nonzero-exit")

    def test_label_guard_drops_forbidden_create_labels(self):
        self.gh.routes = [("issue create", ok(f"https://github.com/{HQ}/issues/1"))]
        self.run_exec([{"type": "create_issue", "repo": HQ, "title": "t",
                        "labels": ["dept:growth", "proposal:pending"]}])
        cmd = self.gh.cmds("issue create")[0]
        self.assertEqual(cmd[cmd.index("-l") + 1], "dept:growth")


class ExecuteLabelTests(Base):
    MODE = "cos"

    def test_add_labels_idempotent_skip(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": [{"name": "P1"}]})))]
        _, okn, _, recs = self.run_exec([{"type": "add_labels", "repo": PROJ, "number": 1, "labels": "P1"}])
        self.assertEqual(recs[0]["classification"], "idempotent-skip")

    def test_add_status_label_syncs_board_and_survives_failure(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": []}))), ("issue edit", ok())]
        with mock.patch.object(ar.board_sync, "sync", side_effect=RuntimeError("no board")) as sync, \
                mock.patch.object(ar, "label_guard") as lg:
            lg.blocked_labels.return_value = []
            lg.load_owners.return_value = {}
            _, okn, _, _ = self.run_exec([{"type": "add_labels", "repo": PROJ, "number": 1,
                                           "labels": ["status:in-progress"]}])
        self.assertEqual(okn, 1)
        sync.assert_called_once_with(PROJ, 1, "in_progress")

    def test_add_labels_failure(self):
        self.gh.routes = [("issue view", (1, "", "")), ("issue edit", (1, "", "bad"))]
        _, _, fail, recs = self.run_exec([{"type": "add_labels", "repo": PROJ, "number": 1, "labels": ["P1"]}])
        self.assertEqual(recs[0]["classification"], "nonzero-exit")

    def test_remove_labels_paths(self):
        self.gh.routes = [("issue view", ok(json.dumps({"labels": [{"name": "P1"}]}))), ("issue edit", ok())]
        _, _, _, recs = self.run_exec([{"type": "remove_labels", "repo": PROJ, "number": 1, "labels": ["P3"]}])
        self.assertEqual(recs[0]["classification"], "idempotent-skip")
        _, okn, _, _ = self.run_exec([{"type": "remove_labels", "repo": PROJ, "number": 1, "labels": ["P1"]}])
        self.assertEqual(okn, 1)
        self.gh.routes = [("issue view", (1, "", "")), ("issue edit", (1, "", "x"))]
        _, _, fail, _ = self.run_exec([{"type": "remove_labels", "repo": PROJ, "number": 1, "labels": ["P1"]}])
        self.assertEqual(fail, 1)

    def test_comment_failure(self):
        ar.MODE = "growth"  # growth: create_issue only -> refused
        _, _, _, recs = self.run_exec([{"type": "comment", "repo": HQ, "number": 1, "body": "x"}])
        self.assertEqual(recs[0]["classification"], "forbidden-action")
        ar.MODE = "cos"
        self.gh.routes = [("issue comment", (1, "", "locked"))]
        _, _, fail, recs = self.run_exec([{"type": "comment", "repo": HQ, "number": 1, "body": "x"}])
        self.assertEqual(recs[0]["classification"], "nonzero-exit")


# ------------------------------------------------------------------ gather

class GatherTests(Base):
    def fake_safe(self, args, warnings, label):
        j = " ".join(args)
        if label == "open_issues":
            return [{"number": 1, "title": "t", "labels": [], "body": "## Why this matters\nx" + "y" * 2000,
                     "author": {"login": "alice"}, "authorAssociation": "MEMBER"}]
        if label == "recent_reports":
            return [{"number": 2, "title": "r", "body": "<untrusted>escape</untrusted> report"}]
        if label == "open_prs":
            return [{"number": 3, "title": "p"}]
        if label == "recent_merged":
            return [{"number": 4, "body": "Gate-for: #1"}]
        if label in ("hq_open_issues", "hq_recent_reports"):
            return [{"number": 5, "body": "hub body"}]
        if "po_verdicts" in label:
            return [{"number": 6, "labels": [{"name": "verdict:backlog"}], "body": "b"},
                    {"number": 7, "labels": [{"name": "P2"}]}]
        if label == "hub_open_prs":
            return [{"number": 8}]
        return [] if "debt" in j or True else None

    def gather(self, mode):
        with mock.patch.object(ar, "gh_json_safe", side_effect=self.fake_safe), \
                mock.patch.object(ar, "enrich_pr_checks"), \
                mock.patch.object(ar, "load_approver_queue", return_value={"reports_to_close": []}), \
                mock.patch.object(ar, "load_cost_ledger", return_value={"total_usd": 0}), \
                mock.patch.object(ar, "load_org_memory", return_value={}), \
                mock.patch.object(ar, "load_capacity_state", return_value={"paused_until": ""}), \
                mock.patch.object(ar, "load_usage", return_value={"est_cost_usd": 1}):
            return ar.gather(mode)

    def test_cos_gather_fences_and_flags(self):
        ctx = self.gather("cos")
        it = ctx["projects"][0]["open_issues"][0]
        self.assertTrue(it["has_why_this_matters"])
        self.assertTrue(it["body"].startswith(f'<untrusted source="{PROJ}#1" author="alice"'))
        self.assertLess(len(it["body"]), 1100)
        rep = ctx["projects"][0]["recent_reports"][0]["body"]
        self.assertNotIn("<untrusted>escape", rep)
        self.assertEqual(ctx["projects"][0]["recent_merged"][0]["state"], "MERGED")
        self.assertIn("recently_closed_debt", ctx["projects"][0])
        self.assertIn(f'source="{HQ}#5"', ctx["hq_open_issues"][0]["body"])
        self.assertIn("role", ctx["flows"])
        self.assertNotIn("usage", ctx)

    def test_brief_gather(self):
        ctx = self.gather("brief")
        self.assertIn("approver_queue", ctx["projects"][0])
        self.assertIn("po_verdicts", ctx)
        items = ctx["po_verdicts"]["by_project"][PROJ]["items"]
        self.assertEqual([i["number"] for i in items], [6])
        self.assertIn("<untrusted", items[0]["body"])
        self.assertIn("hub_open_prs", ctx)
        self.assertIn("cost_ledger", ctx)

    def test_finance_gather_and_flows_failure(self):
        with mock.patch.object(ar.flow_reader, "context_slice_for_role", side_effect=KeyError("x")):
            ctx = self.gather("finance")
        self.assertIn("capacity", ctx)
        self.assertTrue(any("flows.yml" in w for w in ctx["warnings"]))

    def test_hub_open_prs(self):
        with mock.patch.object(ar, "gh_json_safe", return_value=[{"number": 1}]):
            self.assertEqual(ar.load_hub_open_prs([])[0]["state"], "OPEN")


class LoaderTests(Base):
    def test_cost_ledger(self):
        month = ar._month()
        title = f"Cost ledger — {month}"
        rows = [
            {"workflow": "w", "job": "review", "cost_usd": 1.5, "billing": "api", "duration_ms": 10, "num_turns": 2,
             "run_url": "u1", "model_fallback": True, "model": "m", "repo": "o/p", "date": "d"},
            {"workflow": "w", "job": "review", "cost_usd": 0.5, "duration_ms": 20},
            {"workflow": "w", "job": "impl", "billing": "subscription"},
            {"skipped_capacity": True},
        ]
        REST_BOT = {"login": "squadhq-ops[bot]", "type": "Bot"}
        comments = [{"body": "```json\n" + json.dumps(r) + "\n```", "user": REST_BOT} for r in rows]
        comments += [{"body": "no json", "user": REST_BOT}, {"body": "```json\n{bad}\n```", "user": REST_BOT}]
        # spoofed ledger line from a human: never counted
        comments += [{"body": "```json\n" + json.dumps({"workflow": "w", "job": "x", "cost_usd": 999,
                                                          "billing": "api"}) + "\n```",
                      "user": {"login": "mallory"}}]
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 9, "title": title, "author": {"login": "mallory"}},
                                                        {"number": 3, "title": title, "author": BOT}]))),
                          ("issues/3/comments", ok("\n".join(json.dumps(c) for c in comments)))]
        res = ar.load_cost_ledger()
        self.assertEqual(res["issue"], 3)
        self.assertEqual(res["run_count"], 4)
        self.assertEqual(res["capacity_skips"], 1)
        self.assertAlmostEqual(res["total_usd"], 2.0)
        self.assertEqual(res["unpriced_runs"], 1)
        self.assertEqual(res["fallback_activations"]["count"], 1)
        self.assertEqual(res["by_job"]["w/review"]["avg_duration_ms"], 15)
        self.assertEqual(res["top_jobs"][0]["job"], "w/review")
        self.assertEqual(res["ignored_untrusted_rows"], 1)

    def test_cost_ledger_missing_or_failing(self):
        self.gh.routes = [("issue list", (1, "", "x"))]
        self.assertIsNone(ar.load_cost_ledger()["issue"])
        self.gh.routes = [("issue list", ok("[]"))]
        self.assertIsNone(ar.load_cost_ledger()["issue"])
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 3, "title": f"Cost ledger — {ar._month()}"}])))]
        self.assertIsNone(ar.load_cost_ledger()["issue"])  # not bot-authored: ignored
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 3, "title": f"Cost ledger — {ar._month()}",
                                                        "author": BOT}]))),
                          ("issues/3/comments", (1, "", "x"))]
        self.assertEqual(ar.load_cost_ledger()["issue"], 3)

    def test_capacity_state(self):
        import base64
        enc = base64.b64encode(json.dumps({"paused_until": "z"}).encode()).decode()
        self.gh.routes = [("contents/state/capacity.json", ok(enc))]
        self.assertEqual(ar.load_capacity_state()["paused_until"], "z")
        self.gh.routes = [("contents/state/capacity.json", ok(""))]
        self.assertEqual(ar.load_capacity_state()["history"], [])
        self.gh.routes = [("contents/state/capacity.json", ok("!!notb64"))]
        self.assertEqual(ar.load_capacity_state()["paused_until"], "")

    def test_org_memory(self):
        with tempfile.TemporaryDirectory() as home:
            _write(home, "org/lessons.md", "# header\n- [2026-01-01] [x] one\n- [2026-01-02] [y] two\n")
            _write(home, "org/strategy/okrs.md",
                   "<!-- ## Q9 2099 example -->\n## Q4 2026 Goals\n- **KR1:** reach $X\n- KR2: 10 users\n"
                   "## Archive\nold")

            def fake_sh(args, check=True):
                if "-p" in args:
                    return "+- [2026-10-20] [x] new\n+other\n"
                return "2026-10-01T00:00:00+00:00\n"
            with mock.patch.dict(os.environ, {"SQUADHQ_HOME": home}), mock.patch.object(ar, "sh", side_effect=fake_sh):
                w = []
                mem = ar.load_org_memory(w)
        self.assertEqual(w, [])
        self.assertEqual(mem["lessons"]["total_entries"], 2)
        self.assertEqual(mem["lessons"]["added_last_7d"], 1)
        self.assertIsNotNone(mem["lessons"]["weeks_since_change"])
        self.assertEqual(mem["okrs"]["current_quarter"], "Q4 2026 Goals")
        self.assertEqual((mem["okrs"]["total_krs"], mem["okrs"]["placeholder_krs"]), (2, 1))
        self.assertNotIn("old", mem["okrs"]["body"])

    def test_org_memory_missing_files(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"SQUADHQ_HOME": home}), \
                mock.patch.object(ar, "sh", side_effect=RuntimeError("no git")):
            w = []
            mem = ar.load_org_memory(w)
        self.assertEqual(len(w), 3)
        self.assertIsNone(mem["okrs"]["body"])

    def test_load_usage(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"SQUADHQ_HOME": home}):
            self.assertEqual(ar.load_usage()["est_cost_usd"], 0.0)
            _write(home, "state/usage.json", "{}")
            self.assertEqual(ar.load_usage()["month"], ar._month())
            _write(home, "state/usage.json", json.dumps({"month": ar._month(), "est_cost_usd": 3.5}))
            self.assertEqual(ar.load_usage()["est_cost_usd"], 3.5)
            _write(home, "state/usage.json", json.dumps({"month": "1999-01", "est_cost_usd": 3.5}))
            self.assertEqual(ar.load_usage()["est_cost_usd"], 0.0)


# --------------------------------------------------------- usage + budget

class FakeStore:
    def __init__(self, remote, results=(True,)):
        self.remote = remote
        self.results = list(results)
        self.written = None

    def update_json_file(self, repo, path, mutate, message, default=None):
        self.written = mutate(self.remote)
        return self.results.pop(0) if self.results else True


class SaveUsageTests(Base):
    def save(self, remote, u, before, results=(True,)):
        store = FakeStore(remote, results)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"SQUADHQ_HOME": home}), \
                mock.patch.object(github, "client", return_value=store):
            okv = ar.save_usage(u, before)
            local = json.load(open(os.path.join(home, "state", "usage.json")))
        return okv, store, local

    def test_delta_added_onto_remote_not_overwritten(self):
        m = ar._month()
        remote = {"month": m, "input_tokens": 100, "output_tokens": 10, "est_cost_usd": 1.0}
        before = {"month": m, "input_tokens": 50, "output_tokens": 5, "est_cost_usd": 0.5}
        u = {"month": m, "input_tokens": 60, "output_tokens": 7, "est_cost_usd": 0.75, "last_model": "x"}
        okv, store, local = self.save(remote, u, before)
        self.assertTrue(okv)
        self.assertEqual(store.written["input_tokens"], 110)
        self.assertEqual(store.written["output_tokens"], 12)
        self.assertAlmostEqual(store.written["est_cost_usd"], 1.25)
        self.assertEqual(store.written["last_model"], "x")
        self.assertEqual(local["month"], m)

    def test_seed_and_month_rollover(self):
        m = ar._month()
        u = {"month": m, "input_tokens": 5, "output_tokens": 1, "est_cost_usd": 0.1}
        _, store, _ = self.save({}, u, None)
        self.assertEqual(store.written["input_tokens"], 5)
        _, store, _ = self.save({"month": "1999-01", "input_tokens": 99}, u, None)
        self.assertEqual(store.written["input_tokens"], 5)

    def test_failure_is_loud(self):
        with tempfile.NamedTemporaryFile("r", suffix=".md") as f, \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": f.name}):
            okv, _, _ = self.save({}, {"month": ar._month(), "est_cost_usd": 1}, None, results=(False,))
            self.assertFalse(okv)
            self.assertIn("USAGE COMMIT LOST", open(f.name).read())

    def test_raise_and_missing_hub(self):
        class Boom:
            def update_json_file(self, *a, **k):
                raise OSError("net")
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"SQUADHQ_HOME": home}):
            with mock.patch.object(github, "client", return_value=Boom()):
                self.assertFalse(ar.save_usage({"month": ar._month()}, None))
            ar.HQ = None
            with mock.patch.dict(os.environ, {"SQUADHQ_HUB_REPO": "", "HQ_REPO": "", "HUB_REPO": "",
                                              "SQUADHQ_ORG": "", "GITHUB_REPOSITORY_OWNER": ""}):
                self.assertFalse(ar.save_usage({"month": ar._month()}, None))


class BudgetGuardTests(Base):
    def test_under_budget_noop(self):
        ar.BUDGET = 50.0
        ar.budget_guard({"est_cost_usd": 1.0, "month": "m"}, "cos")
        self.assertEqual(self.gh.calls, [])

    def test_over_budget_dedups_and_exits(self):
        ar.BUDGET = 1.0
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 7, "title": "BUDGET CAP HIT: $1 for m",
                                                        "author": BOT}])))]
        with tempfile.NamedTemporaryFile("r", suffix=".md") as f, \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": f.name}), self.assertRaises(SystemExit):
            ar.budget_guard({"est_cost_usd": 2.0, "month": "m"}, "cos")
        self.assertFalse(self.gh.cmds("issue create"))

    def test_over_budget_creates_when_absent(self):
        ar.BUDGET = 1.0
        self.gh.routes = [("issue list", ok("[]")), ("issue create", ok(f"https://github.com/{HQ}/issues/8"))]
        with self.assertRaises(SystemExit):
            ar.budget_guard({"est_cost_usd": 2.0, "month": "m"}, "cos")
        self.assertTrue(self.gh.cmds("escalation"))

    def test_find_recent_open_issue_any_age(self):
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 1, "title": "T", "author": BOT}])))]
        self.assertEqual(ar.find_recent_open_issue(HQ, "T"), 1)
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 1, "title": "T", "author": {"login": "mallory"}}])))]
        self.assertIsNone(ar.find_recent_open_issue(HQ, "T"))  # look-alike opened by a human
        self.assertIsNone(ar.find_recent_open_issue(HQ, "Other"))


# ----------------------------------------------------------- summary/main

class SummaryTests(Base):
    def test_write_summary_records_and_failures(self):
        recs = [dict(ar._new_rec("comment", HQ, 1), number=1, success=True, result="ok"),
                dict(ar._new_rec("close_issue", HQ, 2), result="FAILED", classification="nonzero-exit",
                     exit_code=1, cmd="gh x")]
        delta = {"model": "m", "model_fallback": True, "input_tokens": 1, "output_tokens": 2,
                 "cost": 0.1, "month_to_date": 1.0}
        with tempfile.NamedTemporaryFile("r", suffix=".md") as f, \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": f.name}):
            ar.write_summary("cos", delta, [{}, {}], recs, "actions-ok")
            ar.write_summary("cos", delta, [], [], "no-op")
            text = open(f.name).read()
        self.assertIn("Failures — evidence", text)
        self.assertIn("fallback", text)
        self.assertIn("No actions attempted", text)
        self.assertIn("UTC", text)

    def test_no_summary_path(self):
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            self.assertIsNone(ar.write_summary("cos", {}, [], [], "x"))


def _reply(text, stop="end_turn"):
    return ar.LLMReply(text=text, stop_reason=stop, input_tokens=10, output_tokens=5, model="m", raw={})


class MainTests(Base):
    def run_main(self, reply, *, mode="brief", dry=False, ctx=None, exec_result=None, execute_side=None):
        usage = {"month": ar._month(), "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}

        def fake_configure(argv=None):
            ar.MODE, ar.DRY_RUN = mode, dry
            return mode

        def fake_call(sp, uc, u, web):
            u["input_tokens"] += 10
            u["est_cost_usd"] += 0.01
            self.prompt = sp
            return reply

        p = [mock.patch.object(ar, "configure", side_effect=fake_configure),
             mock.patch.object(ar, "load_system_prompt", return_value="SP"),
             mock.patch.object(ar, "load_usage", return_value=usage),
             mock.patch.object(ar, "budget_guard"),
             mock.patch.object(ar, "gather", return_value=ctx or {"projects": [], "warnings": []}),
             mock.patch.object(ar, "call_claude", side_effect=fake_call),
             mock.patch.object(ar, "write_summary"),
             mock.patch.object(ar, "save_usage")]
        if execute_side is not None:
            p.append(mock.patch.object(ar, "execute", side_effect=execute_side))
        else:
            p.append(mock.patch.object(ar, "execute", return_value=exec_result or ([], 0, 0, [])))
        p.append(mock.patch.object(ar, "create_or_comment", return_value=([], 1, 0, [])))
        mocks = [x.start() for x in p]
        try:
            rc = ar.main([])
        finally:
            for x in p:
                x.stop()
        names = ["configure", "prompt", "usage", "budget", "gather", "call", "summary", "save", "execute", "coc"]
        return rc, dict(zip(names, mocks))

    def test_unknown_mode(self):
        with mock.patch.object(ar, "configure", return_value="nope"):
            self.assertEqual(ar.main([]), 2)

    def test_no_inputs_skips_model(self):
        rc, m = self.run_main(_reply(""), ctx={"projects": [], "warnings": ["x"], "hq_open_issues": []})
        self.assertEqual(rc, 0)
        m["call"].assert_not_called()
        m["save"].assert_not_called()

    def test_actions_ok(self):
        rc, m = self.run_main(_reply('[{"type": "comment"}]'), exec_result=(["r"], 1, 0, []))
        self.assertEqual(m["summary"].call_args[0][4], "actions-ok")
        m["save"].assert_called_once()
        self.assertEqual(m["save"].call_args[0][1]["input_tokens"], 0)

    def test_noop(self):
        _, m = self.run_main(_reply("[]"))
        self.assertEqual(m["summary"].call_args[0][4], "no-op")

    def test_dry_run_never_saves(self):
        for text in ("[]", "prose", "", '[{"type":"x"}]'):
            _, m = self.run_main(_reply(text), dry=True)
            self.assertEqual(m["summary"].call_args[0][4], "dry-run")
            m["save"].assert_not_called()
            m["coc"].assert_not_called()

    def test_parse_failure_fallback_delivered(self):
        _, m = self.run_main(_reply("just prose, no json"))
        self.assertEqual(m["summary"].call_args[0][4], "fallback-delivered")
        title = m["coc"].call_args[0][1]
        self.assertIn("PARSE FAILURE", title)
        self.assertEqual(m["coc"].call_args[0][3], ["daily-brief"])

    def test_empty_reply_escalates(self):
        _, m = self.run_main(_reply("", stop="max_tokens"))
        self.assertEqual(m["summary"].call_args[0][4], "escalation-created")
        self.assertIn("max_tokens", m["coc"].call_args[0][1])

    def test_all_failed_escalates(self):
        rec = dict(ar._new_rec("comment", HQ, 1), result="FAILED")
        _, m = self.run_main(_reply('[{"type": "comment"}]'), exec_result=(["x"], 0, 1, [rec]))
        self.assertIn("all actions failed", m["coc"].call_args[0][1])

    def test_usage_saved_even_when_execute_raises(self):
        with self.assertRaises(RuntimeError):
            _, m = self.run_main(_reply("[]"), execute_side=RuntimeError("boom"))
        # run_main re-raises before returning mocks; check through a fresh run
        saves = []
        with mock.patch.object(ar, "save_usage", side_effect=lambda u, b: saves.append(u)), \
                mock.patch.object(ar, "configure", return_value="cos"), \
                mock.patch.object(ar, "load_system_prompt", return_value="SP"), \
                mock.patch.object(ar, "load_usage", return_value={"month": "m", "input_tokens": 0,
                                                                  "output_tokens": 0, "est_cost_usd": 0.0}), \
                mock.patch.object(ar, "budget_guard"), \
                mock.patch.object(ar, "gather", return_value={"projects": [], "warnings": []}), \
                mock.patch.object(ar, "call_claude", return_value=_reply("[]")), \
                mock.patch.object(ar, "execute", side_effect=RuntimeError("boom")):
            ar.DRY_RUN = False
            with self.assertRaises(RuntimeError):
                ar.main([])
        self.assertEqual(len(saves), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class SpoofedBudgetTests(Base):
    def test_human_lookalike_budget_issue_does_not_swallow_escalation(self):
        ar.BUDGET = 1.0
        self.gh.routes = [("issue list", ok(json.dumps([{"number": 7, "title": "BUDGET CAP HIT: $1 for m",
                                                        "author": {"login": "mallory"}}]))),
                          ("issue create", ok("https://github.com/x/y/issues/8"))]
        with self.assertRaises(SystemExit):
            ar.budget_guard({"est_cost_usd": 2.0, "month": "m"}, "cos")
        self.assertTrue(self.gh.cmds("issue create"))

    def test_untrusted_why_section_is_not_impact(self):
        self.assertFalse(ar._body_trusted({"author": {"login": "mallory"}, "authorAssociation": "NONE"}))
        self.assertTrue(ar._body_trusted({"author": {"login": "bob"}, "authorAssociation": "COLLABORATOR"}))
        self.assertTrue(ar._body_trusted({"author": BOT}))


class AssociationTests(Base):
    def test_associations_map_and_failure(self):
        self.gh.routes = [("issues?state=open", ok(json.dumps({"number": 1, "author_association": "member"}) + "\n"
                                                   + json.dumps({"number": 2}) + "\n"))]
        self.assertEqual(ar._associations(PROJ, []), {1: "MEMBER", 2: ""})
        self.gh.routes = [("issues?state=open", (1, "", "boom"))]
        w = []
        self.assertEqual(ar._associations(PROJ, w), {})
        self.assertTrue(w)
