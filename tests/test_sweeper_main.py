"""End-to-end tests for squadhq.stuck_sweeper.main / run_dispatch /
idle_alarm and squadhq.ticket_transition.main, driven through a fake `gh`
seam. No network: every gh call lands in FakeGH, which answers from a small
in-memory repo model and records what was called."""
import datetime
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from squadhq import flow_reader
from squadhq import stuck_sweeper as sw
from squadhq import ticket_transition as tt

NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)
WHY = "Do X.\n\n**Why this matters:** Business: a. Technical: b."
REPO = "o/r"


def ago(minutes):
    return (NOW - datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def issue(n, labels=(), body=WHY, created=600, bot=False, title=None):
    return {"number": n, "title": title or f"Ticket {n}", "body": body, "createdAt": ago(created),
            "labels": [{"name": l} for l in labels], "author": {"login": "app/squadhq-ops" if bot else "x", "is_bot": bot}, "authorAssociation": "NONE" if bot else "MEMBER"}


def labeled(label, minutes, actor="squadhq-ops[bot]"):
    return {"event": "labeled", "label": {"name": label}, "created_at": ago(minutes), "actor": {"login": actor}}


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(err="boom"):
    return subprocess.CompletedProcess([], 1, "", err)


class FakeGH:
    def __init__(self, issues=(), prs=(), closed_prs=(), closed_issues=(), events=None, comments=None,
                 hub_issues=(), fail_on=()):
        self.issues = list(issues)
        self.prs = list(prs)
        self.closed_prs = list(closed_prs)
        self.closed_issues = list(closed_issues)
        self.events = events or {}
        self.comments = comments or {}
        self.hub_issues = list(hub_issues)
        self.fail_on = fail_on  # tuples: arg-prefixes whose call fails
        self.calls = []

    def writes(self):
        return [c for c in self.calls if c[:2] in (["issue", "edit"], ["pr", "edit"], ["issue", "comment"],
                                                    ["issue", "close"], ["issue", "create"], ["pr", "ready"],
                                                    ["label", "create"])]

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        for prefix in self.fail_on:
            if args[:len(prefix)] == list(prefix):
                return fail()
        a0 = args[:2]
        if a0 == ["issue", "list"]:
            repo = args[args.index("-R") + 1]
            state = args[args.index("--state") + 1]
            if "--search" in args and "in:title" in args[args.index("--search") + 1]:
                return ok(json.dumps(self.hub_issues))
            if repo != REPO:
                return ok("[]")
            if state == "closed":
                return ok(json.dumps(self.closed_issues))
            items = self.issues
            if "--label" in args:
                lb = args[args.index("--label") + 1]
                items = [i for i in items if lb in {l["name"] for l in i["labels"]}]
            return ok(json.dumps(items))
        if a0 == ["pr", "list"]:
            state = args[args.index("--state") + 1]
            return ok(json.dumps(self.prs if state == "open" else self.closed_prs))
        if a0 == ["pr", "view"]:
            n = int(args[2])
            for p in self.prs + self.closed_prs:
                if p["number"] == n:
                    return ok(json.dumps(p))
            return ok("{}")
        if a0 == ["issue", "view"]:
            return ok(json.dumps({"number": int(args[2]), "state": "CLOSED",
                                  "labels": [{"name": "status:in-review"}]}))
        if args[0] == "api":
            if "--paginate" in args:
                path = args[2]
                n = int(path.split("/")[-2])
                src = self.events if path.endswith("/events") else self.comments
                return ok("\n".join(json.dumps(x) for x in src.get(n, [])))
            path = args[1]
            if path.endswith("/check-runs?per_page=100"):
                return ok(json.dumps({"check_runs": []}))
            if "/commits/" in path and path.endswith("/pulls"):
                sha = path.split("/")[-2]
                return ok(json.dumps([{"number": int(sha[3:]), "merged_at": ago(1)}]))
            if "/commits/" in path:
                return ok(json.dumps({"commit": {"committer": {"date": ago(600)}}}))
            if "/compare/" in path:
                return ok(json.dumps({"commits": [{"sha": "sha40"}]}))
        return ok("https://github.com/o/r/issues/99")


def run_main(fake, argv=("--repo", REPO), cap=None, guard_overrides=None):
    real_guard = flow_reader.get_flood_guard

    def guard(name, path=None):
        g = real_guard(name, path)
        if name == "stuck_sweeper" and guard_overrides:
            g = dict(g, **guard_overrides)
        return g

    out = io.StringIO()
    with mock.patch.object(sw, "gh", side_effect=fake), \
            mock.patch.object(sw, "utcnow", return_value=NOW), \
            mock.patch.object(sw.flow_reader, "get_flood_guard", side_effect=guard), \
            mock.patch.object(sw.capacity_gate, "read_hub_json", return_value=(cap or {"paused_until": ""}, "s")), \
            mock.patch.object(sw.capacity_gate, "write_capacity", return_value=True) as w, \
            mock.patch.dict(os.environ, {"SQUADHQ_HUB_REPO": "o/hub", "GITHUB_STEP_SUMMARY": ""}), \
            redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = sw.main(list(argv))
    return rc, out.getvalue(), w


class SweeperMainTests(unittest.TestCase):
    def test_paused_capacity_skips_everything(self):
        fake = FakeGH()
        rc, out, _ = run_main(fake, cap={"paused_until": ago(-30)})
        self.assertEqual(rc, 0)
        self.assertIn("sweep skipped", out)
        self.assertFalse(any(c[:2] == ["issue", "list"] and REPO in c for c in fake.calls))

    def test_unparseable_pause_skips(self):
        rc, out, _ = run_main(FakeGH(), cap={"paused_until": "soon"})
        self.assertIn("unparseable", out)

    def test_missing_hub_repo_is_an_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
            self.assertEqual(sw.main(["--repo", REPO]), 2)

    def scenario(self):
        issues = [
            issue(1),                                             # unshaped -> refire agent:shape
            issue(2, ["agent:implement"]),                        # merged PR -> close
            issue(3, ["agent:implement", "needs-design"]),        # normalize: design first
            issue(4, ["needs-human"]),                            # approver's: skipped
            issue(5, ["triaged:ready", "P1"]),                    # dispatch
            issue(6, ["agent:implement"]),                        # implement_stopped -> escalate
            issue(7, ["triaged:ready"], body="no section"),       # impact_missing
            issue(8, ["status:in-review"]),                       # stale in-review, no PR -> remove
        ]
        events = {6: [labeled("agent:implement", 120)], 7: [labeled("triaged:ready", 600)],
                  10: [labeled("ready-to-merge", 120)], 8: [labeled("status:in-review", 600)]}
        comments = {6: [{"body": "[Engineer] Blocked — unclear spec", "created_at": ago(60),
                         "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "author_association": "NONE"}],
                    1: [{"body": "@squad please look", "created_at": ago(90), "user": {"type": "User"},
                         "author_association": "OWNER"}]}
        prs = [{"number": 10, "title": "PR", "body": "Closes #99", "labels": [{"name": "ready-to-merge"}],
                "isDraft": False, "headRefOid": "abc", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": []}]
        closed = [{"number": 20, "body": "Closes #2", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": [], "mergedAt": ago(30),
                   "closedAt": ago(30)}]
        return FakeGH(issues, prs, closed, [{"number": 50}], events, comments)

    def test_live_sweep_acts_on_every_kind(self):
        fake = self.scenario()
        rc, out, _ = run_main(fake)
        self.assertEqual(rc, 0)
        self.assertIn("o/r#2 | reconcile", out)
        self.assertIn("close (PR #20 merged)", out)
        self.assertIn("o/r#3 | normalize", out)
        self.assertIn("o/r#6 | implement_stopped", out)
        self.assertIn("o/r#7 | impact_missing", out)
        self.assertIn("o/r#1 | mention_unanswered", out)
        self.assertIn("o/r#10 | merge_decision_missing", out)
        self.assertIn("o/r#5 | dispatch", out)
        self.assertNotIn("o/r#4 |", out)
        w = fake.writes()
        self.assertIn(["issue", "close", "2", "-R", REPO, "--reason", "completed", "--comment"], [c[:8] for c in w])
        self.assertTrue(any(c[:3] == ["issue", "edit", "6"] and "needs-human" in c for c in w))
        self.assertTrue(any(c[:3] == ["issue", "edit", "5"] and "agent:implement" in c for c in w))
        self.assertTrue(any(c[:3] == ["pr", "edit", "10"] and "--add-label" in c for c in w))

    def test_dry_run_writes_nothing(self):
        fake = self.scenario()
        rc, out, w = run_main(fake, argv=("--repo", REPO, "--dry-run"))
        self.assertIn("(dry run)", out)
        self.assertEqual(fake.writes(), [])

    def test_action_budget_defers(self):
        fake = self.scenario()
        rc, out, _ = run_main(fake, guard_overrides={"actions_per_run_max": 1})
        self.assertIn("deferred (action cap reached)", out)

    def test_failures_are_reported(self):
        fake = self.scenario()
        fake.fail_on = (("issue", "edit"), ("pr", "edit"), ("issue", "close"))
        rc, out, _ = run_main(fake)
        self.assertIn("FAILED", out)

    def test_repeated_refires_escalate(self):
        markers = [{"body": f"{sw.BADGE}x\n\n<!-- stuck-sweeper state=dispatched_not_started -->",
                    "created_at": ago(m), "user": {"type": "Bot", "login": "squadhq-ops[bot]"}} for m in (200, 100)]
        fake = FakeGH([issue(9, ["agent:implement"])], events={9: [labeled("agent:implement", 90)]},
                      comments={9: markers})
        rc, out, _ = run_main(fake)
        self.assertIn("o/r#9 | dispatched_not_started", out)
        self.assertIn("| escalate", out)

    def test_already_escalated_waits(self):
        esc = [{"body": f"{sw.BADGE}x\n\n<!-- stuck-sweeper state=dispatched_not_started escalated -->",
                "created_at": ago(30), "user": {"type": "Bot", "login": "squadhq-ops[bot]"}}]
        fake = FakeGH([issue(9, ["agent:implement"])], events={9: [labeled("agent:implement", 90)]},
                      comments={9: esc})
        rc, out, _ = run_main(fake)
        self.assertIn("already escalated, waiting on approver", out)

    def test_broken_repo_is_reported_not_fatal(self):
        fake = FakeGH()
        fake.fail_on = (("issue", "list", "-R", REPO),)
        rc, out, _ = run_main(fake)
        self.assertEqual(rc, 0)
        self.assertIn("| o/r | error |", out)

    def test_cap_warning_in_summary(self):
        fake = FakeGH(closed_issues=[{"number": i} for i in range(300)])
        rc, out, _ = run_main(fake)
        self.assertIn("300-item cap", out)

    def test_step_summary_written(self):
        import tempfile
        with tempfile.NamedTemporaryFile("r", suffix=".md", delete=False) as f:
            path = f.name
        fake = FakeGH()
        with mock.patch.object(sw, "gh", side_effect=fake), mock.patch.object(sw, "utcnow", return_value=NOW), \
                mock.patch.object(sw.capacity_gate, "read_hub_json", return_value=({}, None)), \
                mock.patch.object(sw, "project_repos", return_value=[REPO]), \
                mock.patch.dict(os.environ, {"SQUADHQ_HUB_REPO": "o/hub", "GITHUB_STEP_SUMMARY": path}), \
                redirect_stdout(io.StringIO()):
            sw.main([])
        self.assertIn("Stuck sweeper", open(path).read())
        os.unlink(path)


class IdleAlarmTests(unittest.TestCase):
    GUARD = {"idle_alarm_hours": 24}

    def call(self, fake, issues, prs=(), closed=(), plan=(), dry=False):
        facts = sw.pr_facts(list(prs), list(closed), 5, issues)
        facts["closed_issues"] = set()
        with mock.patch.object(sw, "gh", side_effect=fake):
            return sw.idle_alarm(REPO, issues, list(prs), list(closed), facts, list(plan), self.GUARD, dry, NOW)

    def test_opens_alarm_when_idle(self):
        fake = FakeGH()
        rows = self.call(fake, [issue(1, ["triaged:ready"], body="none")])
        self.assertIn("opened escalation", rows[0])
        self.assertTrue(any(c[:2] == ["issue", "create"] for c in fake.calls))

    def test_refreshes_existing_alarm(self):
        fake = FakeGH()
        alarm = issue(5, ["escalation"], title=sw.IDLE_TITLE, bot=True)
        rows = self.call(fake, [alarm, issue(1, ["proposal:pending"])])
        self.assertIn("refreshed", rows[0])
        self.assertTrue(any(c[:3] == ["issue", "edit", "5"] for c in fake.calls))

    def test_closes_every_alarm_once_busy(self):
        fake = FakeGH()
        alarms = [issue(5, title=sw.IDLE_TITLE, bot=True), issue(6, title=sw.IDLE_TITLE, bot=True)]
        prs = [{"number": 10, "isDraft": False, "body": ""}]
        rows = self.call(fake, alarms, prs=prs)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("resolved — closed" in r for r in rows))

    def test_close_failure_reported(self):
        fake = FakeGH(fail_on=(("issue", "close"),))
        rows = self.call(fake, [issue(5, title=sw.IDLE_TITLE, bot=True)], prs=[{"number": 1, "isDraft": False, "body": ""}])
        self.assertIn("FAILED", rows[0])

    def test_recent_close_is_not_idle(self):
        rows = self.call(FakeGH(), [], closed=[{"number": 1, "body": "", "closedAt": ago(60)}])
        self.assertEqual(rows, [])

    def test_disabled(self):
        with mock.patch.object(sw, "gh", side_effect=FakeGH()):
            self.assertEqual(sw.idle_alarm(REPO, [], [], [], {}, [], {}, False, NOW), [])

    def test_capped_issue_list_queries_alarm_directly(self):
        fake = FakeGH(hub_issues=[{"number": 77, "title": sw.IDLE_TITLE, "author": {"login": "app/squadhq-ops"}}])
        many = [issue(i, ["proposal:pending"]) for i in range(1, 201)]
        rows = self.call(fake, many, dry=True)
        self.assertIn("#77", rows[0])


class WipTests(unittest.TestCase):
    def test_wip_slots_counts_prs_and_inflight(self):
        prs = [{"number": 1, "body": "Closes #3", "isDraft": False, "author": {"login": "app/squadhq-ops"}}, {"number": 2, "body": "", "isDraft": True}]
        issues = [issue(3, ["agent:implement"]), issue(4, ["agent:implement"]),
                  issue(5, ["agent:implement", "needs-human"])]
        self.assertEqual(sw.wip_slots(prs, issues, 5), 3)   # 5 - 1 open PR - #4 in flight
        self.assertEqual(sw.wip_slots(prs, issues, 1), 0)
        self.assertFalse(sw.pr_facts(prs, [], 2, issues)["wip_free"])
        self.assertTrue(sw.pr_facts(prs, [], 2)["wip_free"])


class SeamTests(unittest.TestCase):
    def test_gh_json_and_list_errors(self):
        with mock.patch.object(sw, "gh", return_value=fail("nope")):
            with self.assertRaises(RuntimeError):
                sw.gh_json(["x"])
            with self.assertRaises(RuntimeError):
                sw.gh_list("repos/o/r/issues/1/events")

    def test_gh_delegates_to_client(self):
        client = mock.Mock()
        client.run.return_value = ok("[]")
        with mock.patch.object(sw.github, "client", return_value=client):
            self.assertEqual(sw.gh(["issue", "list"]).stdout, "[]")
        client.run.assert_called_once_with(["issue", "list"])

    def test_relabel_remove_failure_stops(self):
        calls = []

        def g(args):
            calls.append(args)
            return fail("HTTP 500") if "--remove-label" in args else ok()
        with mock.patch.object(sw, "gh", side_effect=g):
            self.assertEqual(sw.relabel(REPO, 1, "x", False).returncode, 1)
        self.assertEqual(len(calls), 1)

    def test_relabel_absent_label_is_fine(self):
        def g(args):
            return fail("label not found") if "--remove-label" in args else ok()
        with mock.patch.object(sw, "gh", side_effect=g):
            self.assertEqual(sw.relabel(REPO, 1, "x", True).returncode, 0)

    def test_open_issues_titled_exact_and_bad_json(self):
        b = {"login": "app/squadhq-ops"}
        hits = [{"number": 1, "title": "T", "author": b}, {"number": 2, "title": "T (old)", "author": b},
                {"number": 3, "title": "T", "author": b}, {"number": 4, "title": "T", "author": {"login": "mallory"}}]
        with mock.patch.object(sw, "gh", return_value=ok(json.dumps(hits))):
            self.assertEqual(sw.open_issues_titled("o/h", "T"), [1, 3])
        with mock.patch.object(sw, "gh", return_value=ok("not json")):
            self.assertEqual(sw.open_issues_titled("o/h", "T"), [])

    def test_project_repos_from_hub_config(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "config"))
            with open(os.path.join(d, "config", "projects.json"), "w") as f:
                json.dump({"projects": [{"repo": "a/b"}]}, f)
            with mock.patch.dict(os.environ, {"SQUADHQ_HOME": d}):
                self.assertEqual(sw.project_repos(), ["a/b"])

    def test_mention_phrases_include_legacy(self):
        with mock.patch.dict(os.environ, {"SQUADHQ_TRIGGER": "@crew"}):
            self.assertEqual(sw.mention_phrases(), {"@crew", "@claude"})

    def test_safe_ts(self):
        self.assertIsNone(sw.safe_ts("garbage"))
        self.assertIsNone(sw.safe_ts(None))
        self.assertEqual(sw.safe_ts("2026-01-01T00:00:00").tzinfo, datetime.timezone.utc)


class RefireTests(unittest.TestCase):
    STATES = ["unshaped", "impact_missing", "proposal_missing", "approver_reply_unanswered", "in_progress_stalled",
              "design_stalled", "dispatched_not_started", "design_not_started", "fix_round_not_started",
              "merge_decision_missing", "review_missing", "team_thread"]

    def test_every_state_fires_and_comments(self):
        ctx = {"comments": [{"body": "@team-thread\nTeam-input-scope: architect"}]}
        for state in self.STATES:
            fake = FakeGH()
            with mock.patch.object(sw, "gh", side_effect=fake):
                r = sw.refire(REPO, {"number": 3}, state, state in ("fix_round_not_started",
                                                                    "merge_decision_missing", "review_missing"),
                              ctx, set())
            self.assertEqual(r.returncode, 0, state)
            self.assertTrue(any("stuck-sweeper state=" + state in " ".join(c) for c in fake.calls), state)

    def test_unknown_state_raises(self):
        with mock.patch.object(sw, "gh", side_effect=FakeGH()), self.assertRaises(ValueError):
            sw.refire(REPO, {"number": 3}, "nope", False, {}, set())

    def test_removal_failure_skips_comment(self):
        fake = FakeGH(fail_on=(("pr", "ready"),))
        with mock.patch.object(sw, "gh", side_effect=fake):
            r = sw.refire(REPO, {"number": 3}, "review_missing", True, {}, set())
        self.assertEqual(r.returncode, 1)
        self.assertFalse(any(c[:2] == ["issue", "comment"] for c in fake.calls))

    def test_escalate_add_failure_returns_it(self):
        fake = FakeGH(fail_on=(("issue", "edit"),))
        with mock.patch.object(sw, "gh", side_effect=fake):
            self.assertEqual(sw.escalate(REPO, {"number": 3}, "x", False, "why").returncode, 1)
        self.assertFalse(any(c[:2] == ["issue", "comment"] for c in fake.calls))


class HousekeepingTests(unittest.TestCase):
    def test_clean_closed_and_failure(self):
        fake = FakeGH(closed_issues=[{"number": 4, "labels": [{"name": "status:in-review"}]},
                                     {"number": 5, "labels": []}], fail_on=(("issue", "edit"),))
        with mock.patch.object(sw, "gh", side_effect=fake), mock.patch.object(sw, "utcnow", return_value=NOW):
            rows = sw.clean_closed(REPO, False)
        self.assertEqual(len(rows), 1)
        self.assertIn("FAILED", rows[0])

    def test_close_parked(self):
        it = issue(4, ["verdict:decline", "P3"])
        fake = FakeGH([it], events={4: [labeled("verdict:decline", 60 * 24 * 10)]})
        guard = {"decline_close_days": 7, "backlog_close_days": 0}
        with mock.patch.object(sw, "gh", side_effect=fake):
            rows = sw.close_parked(REPO, False, NOW, guard)
            dry = sw.close_parked(REPO, True, NOW, guard)
        self.assertIn("close (declined", rows[0])
        self.assertEqual(len(dry), 1)
        self.assertTrue(any(c[:3] == ["issue", "close", "4"] for c in fake.calls))

    def test_close_parked_cap(self):
        its = [issue(n, ["verdict:decline"]) for n in (1, 2)]
        fake = FakeGH(its, events={n: [labeled("verdict:decline", 60 * 24 * 10)] for n in (1, 2)})
        with mock.patch.object(sw, "gh", side_effect=fake):
            rows = sw.close_parked(REPO, True, NOW, {"decline_close_days": 7}, cap=1)
        self.assertEqual(len(rows), 1)


class CapacityCheckTests(unittest.TestCase):
    def test_close_failure_noted(self):
        fake = FakeGH(hub_issues=[{"number": 67, "title": sw.CAPACITY_ESCALATION_TITLE, "author": {"login": "app/squadhq-ops"}}],
                      fail_on=(("issue", "close"),))
        with mock.patch.object(sw, "gh", side_effect=fake), \
                mock.patch.object(sw.capacity_gate, "read_hub_json", return_value=({"paused_until": ""}, None)):
            paused, _, note = sw.capacity_check("o/hub", NOW, False)
        self.assertFalse(paused)
        self.assertIn("FAILED", note)


def tt_run(fake, argv, cap=None, env=None):
    out = io.StringIO()
    with mock.patch.object(sw, "gh", side_effect=fake), mock.patch.object(sw, "utcnow", return_value=NOW), \
            mock.patch.object(tt.capacity_gate, "read_hub_json", return_value=(cap or {}, None)), \
            mock.patch.dict(os.environ, env if env is not None else {"SQUADHQ_HUB_REPO": "o/hub"}, clear=True), \
            redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = tt.main(argv)
    return rc, out.getvalue()


class TicketTransitionMainTests(unittest.TestCase):
    def fake(self):
        merged = {"number": 40, "title": "impl", "body": "Closes #3", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": [{"number": 3}],
                  "mergedAt": ago(1), "closedAt": ago(1)}
        design = {"number": 41, "title": "design", "body": "Gate-for: #6", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": [],
                  "mergedAt": ago(1), "closedAt": ago(1)}
        issues = [issue(3, ["agent:implement", "status:in-review"]), issue(5, ["triaged:ready", "P0"]),
                  issue(6, ["needs-design"]), issue(7, ["agent:implement", "needs-human"], body=WHY + "\nAfter: #3")]
        f = FakeGH(issues, [], [merged, design], [{"number": 2}])
        return f

    def test_merged_push_moves_tickets_and_dispatches(self):
        fake = self.fake()
        rc, out = tt_run(fake, ["--repo", REPO, "--before", "a" * 40, "--after", "sha40"])
        self.assertEqual(rc, 0)
        self.assertIn("Merged PRs in this push: #40", out)
        self.assertIn("| #3 | PR #40 |", out)
        self.assertIn("freed WIP slot", out)

    def test_design_gate_and_direct_push(self):
        fake = self.fake()
        rc, out = tt_run(fake, ["--repo", REPO, "--before", "0" * 40, "--after", "sha41", "--dry-run"])
        self.assertIn("#6", out)
        self.assertIn("(dry run)", out)
        self.assertEqual(fake.writes(), [])

    def test_closed_pr_path(self):
        fake = self.fake()
        fake.prs = [{"number": 45, "title": "x", "body": "Closes #3", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": [{"number": 3}],
                     "isDraft": False}]
        rc, out = tt_run(fake, ["--repo", REPO, "--closed-pr", "45"])
        self.assertIn("PR #45 closed without merging", out)

    def test_already_closed_ticket_loses_work_labels(self):
        fake = self.fake()
        fake.issues = []
        rc, out = tt_run(fake, ["--repo", REPO, "--before", "a" * 40, "--after", "sha40"])
        self.assertIn("closed — removed status:in-review", out)

    def test_capacity_paused_skips_dispatch(self):
        rc, out = tt_run(self.fake(), ["--repo", REPO, "--after", "sha40"], cap={"paused_until": ago(-30)})
        self.assertIn("dispatch skipped: capacity paused", out)

    def test_no_hub_repo_skips_breaker(self):
        rc, out = tt_run(self.fake(), ["--repo", REPO, "--after", "sha40"], env={})
        self.assertNotIn("capacity paused", out)


if __name__ == "__main__":
    unittest.main()
