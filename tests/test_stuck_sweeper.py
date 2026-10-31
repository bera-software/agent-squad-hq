#!/usr/bin/env python3
"""
Tests for squadhq.stuck_sweeper's pure classifiers and attempt counter.

    python3 -m unittest discover tests

No network: classify_issue / classify_pr / attempts take plain dicts. The
thresholds come from the real config/flows.yml so an edit there that breaks
the sweeper's expectations fails here.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from squadhq import flow_reader  # noqa: E402
from squadhq import stuck_sweeper as sw  # noqa: E402

NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)
TH = flow_reader.get_flood_guard("stuck_sweeper")["thresholds_minutes"]


def ago(minutes):
    return (NOW - datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


WHY = "Do X.\n\n**Why this matters:** Business: a. Technical: b."


def issue(labels=(), n=1, created=ago(600), bot=False, body=WHY):
    return {"number": n, "labels": [{"name": l} for l in labels], "createdAt": created,
            "author": {"login": "app/squadhq-ops" if bot else "x", "is_bot": bot}, "authorAssociation": "NONE" if bot else "MEMBER", "body": body}


def labeled(label, minutes):
    return {"event": "labeled", "label": {"name": label}, "created_at": ago(minutes)}


def ictx(events=(), comments=(), open_pr_closes=(), gate_prs=()):
    return {"events": list(events), "comments": list(comments),
            "open_pr_closes": set(open_pr_closes), "gate_prs": set(gate_prs)}


def pctx(events=(), head=600, checks=()):
    return {"events": list(events), "comments": [], "head_committed_at": sw.parse_ts(ago(head)),
            "review_checks": list(checks)}


class IssueTests(unittest.TestCase):
    def state(self, i, c):
        hit = sw.classify_issue(i, c, NOW, TH)
        return hit[0] if hit else None

    def test_unshaped_human_issue_past_threshold(self):
        self.assertEqual(self.state(issue(), ictx()), "unshaped")

    def test_unshaped_young_issue_waits(self):
        self.assertIsNone(self.state(issue(created=ago(5)), ictx()))

    def test_bot_issue_without_originated_by_is_not_intake(self):
        self.assertIsNone(self.state(issue(bot=True), ictx()))

    def test_self_actuated_bot_issue_is_intake(self):
        self.assertEqual(self.state(issue(bot=True, body="Originated-by: legal (x)"), ictx()), "unshaped")

    def test_triaged_or_verdict_is_not_unshaped(self):
        for l in ("triaged:ready", "verdict:backlog", "dept:backend", "P2", "debt-proposal"):
            self.assertIsNone(self.state(issue([l]), ictx()), l)

    def test_agent_shape_age_counts_from_label(self):
        self.assertIsNone(self.state(issue(["agent:shape"]), ictx([labeled("agent:shape", 5)])))

    def test_dispatched_not_started(self):
        c = ictx([labeled("agent:implement", 90)])
        self.assertEqual(self.state(issue(["agent:implement"]), c), "dispatched_not_started")

    def test_engineer_already_ran_is_stopped_not_refired(self):
        spoke = {"body": "[Engineer] Not implementing this", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(80)}
        c = ictx([labeled("agent:implement", 90)], comments=[spoke])
        self.assertEqual(self.state(issue(["agent:implement"]), c), "implement_stopped")
        self.assertIn("implement_stopped", sw.NO_REFIRE)

    def test_dispatched_with_open_pr_or_in_progress_is_fine(self):
        self.assertIsNone(self.state(issue(["agent:implement"]), ictx([labeled("agent:implement", 90)], open_pr_closes=[1])))
        self.assertIsNone(self.state(issue(["agent:implement", "status:in-progress"]), ictx([labeled("agent:implement", 90)])))

    def test_approver_wait_labels_never_touched(self):
        for l in sw.APPROVER_WAIT_LABELS:
            self.assertIsNone(self.state(issue(["agent:implement", l]), ictx([labeled("agent:implement", 900)])), l)

    def test_design_not_started_unless_gate_pr(self):
        c = ictx([labeled("needs-design", 90)])
        self.assertEqual(self.state(issue(["needs-design"]), c), "design_not_started")
        self.assertIsNone(self.state(issue(["needs-design"]), ictx([labeled("needs-design", 90)], gate_prs=[1])))

    def test_proposal_label_without_po_comment_is_missing(self):
        cos = {"body": "[Chief of Staff] awaits PO shaping", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(60)}
        c = ictx([labeled("proposal:pending", 600)], comments=[cos])
        self.assertEqual(self.state(issue(["proposal:pending", "dept:growth", "P2"]), c), "proposal_missing")

    def test_team_thread_pending(self):
        po = {"body": "[Product Owner] ## Proposal", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(200)}
        trig = {"body": "@team-thread\nTeam-input-scope: architect", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(120)}
        self.assertEqual(self.state(issue(["proposal:pending"]), ictx(comments=[po, trig])), "team_thread")
        done = {"body": sw.TEAM_THREAD_DONE, "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(60)}
        self.assertIsNone(self.state(issue(["proposal:pending"]), ictx(comments=[po, trig, done])))

    def test_proposal_without_team_thread_waits_on_approver(self):
        po = {"body": "[Product Owner] ## Proposal", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(200)}
        self.assertIsNone(self.state(issue(["proposal:pending"]), ictx(comments=[po])))


class PrTests(unittest.TestCase):
    def state(self, labels, c, draft=False):
        hit = sw.classify_pr({"number": 7, "labels": [{"name": l} for l in labels], "isDraft": draft}, c, NOW, TH)
        return hit[0] if hit else None

    def test_review_missing_when_no_check_ran(self):
        self.assertEqual(self.state([], pctx()), "review_missing")

    def test_review_failed_counts_as_missing(self):
        self.assertEqual(self.state([], pctx(checks=[{"status": "completed", "conclusion": "failure"}])), "review_missing")

    def test_review_ran_or_skipped_or_running_is_fine(self):
        for c in ({"status": "completed", "conclusion": "success"},
                  {"status": "completed", "conclusion": "skipped"},
                  {"status": "in_progress", "conclusion": None}):
            self.assertIsNone(self.state([], pctx(checks=[c])), c)

    def test_draft_ignored(self):
        self.assertIsNone(self.state([], pctx(), draft=True))

    def test_merge_decision_missing(self):
        self.assertEqual(self.state(["ready-to-merge"], pctx([labeled("ready-to-merge", 60)])), "merge_decision_missing")
        self.assertIsNone(self.state(["ready-to-merge", "needs-approver-merge"], pctx([labeled("ready-to-merge", 60)])))

    def test_fix_round_not_started_unless_pushed_after(self):
        ev = [labeled("review:changes-requested", 90)]
        self.assertEqual(self.state(["review:changes-requested"], pctx(ev, head=120)), "fix_round_not_started")
        self.assertIsNone(self.state(["review:changes-requested"], pctx(ev, head=30)))


class AttemptTests(unittest.TestCase):
    def c(self, state, minutes, escalated=False):
        tag = f"<!-- stuck-sweeper state={state}{' escalated' if escalated else ''} -->"
        return {"body": f"x\n{tag}", "created_at": ago(minutes), "user": {"login": "squadhq-ops[bot]"}}

    def test_counts_only_same_state_inside_window(self):
        window = NOW - datetime.timedelta(hours=24)
        cs = [self.c("unshaped", 60), self.c("unshaped", 120), self.c("unshaped", 60 * 30), self.c("review_missing", 30)]
        self.assertEqual(sw.attempts(cs, "unshaped", window), (2, False))

    def test_approver_clearing_escalation_resets_count(self):
        window = NOW - datetime.timedelta(hours=24)
        cs = [self.c("unshaped", 300), self.c("unshaped", 240), self.c("unshaped", 200, escalated=True)]
        self.assertEqual(sw.attempts(cs, "unshaped", window), (0, True))


if __name__ == "__main__":
    unittest.main()


class NormalizeTests(unittest.TestCase):
    def ctx(self, events=(), open_pr_closes=(), merged_gate=()):
        return {"events": list(events), "comments": [], "open_pr_closes": set(open_pr_closes),
                "gate_prs": set(merged_gate), "merged_gate": set(merged_gate)}

    def test_ready_debt_proposal_becomes_debt(self):
        # CoS marked a debt finding triaged:ready instead of
        # relabelling it to debt; debt-proposal blocked dispatch for days.
        fx = sw.as_fix(sw.normalize_issue(issue(["debt-proposal", "triaged:ready", "P2"]), self.ctx()))
        self.assertEqual(fx["remove"], {"debt-proposal", "triaged:ready"})
        self.assertEqual(fx["add"], ["debt"])
        parked = issue(["debt-proposal", "triaged:ready", "P3"])
        self.assertNotEqual(sw.normalize_issue(parked, self.ctx())[-1], ["debt"])

    def test_consistent_issue_untouched(self):
        self.assertIsNone(sw.normalize_issue(issue(["agent:implement", "P2", "dept:backend"]), self.ctx()))

    def test_triaged_ready_dropped_once_dispatched(self):
        self.assertEqual(sw.normalize_issue(issue(["triaged:ready", "agent:implement"]), self.ctx())[0], {"triaged:ready"})

    def test_design_wins_until_gate_merged(self):
        both = issue(["agent:implement", "needs-design"])
        self.assertEqual(sw.normalize_issue(both, self.ctx())[0], {"agent:implement"})
        self.assertEqual(sw.normalize_issue(both, self.ctx(merged_gate=[1]))[0], {"needs-design"})

    def test_parked_and_dispatched_escalates(self):
        self.assertEqual(sw.normalize_issue(issue(["verdict:backlog", "P3", "agent:implement"]), self.ctx())[0], "escalate")
        self.assertIsNone(sw.normalize_issue(issue(["verdict:overridden", "agent:implement"]), self.ctx()))

    def test_status_pair_resolved_by_open_pr(self):
        both = issue(["status:in-progress", "status:in-review"])
        self.assertEqual(sw.normalize_issue(both, self.ctx(open_pr_closes=[1]))[0], {"status:in-progress"})
        self.assertEqual(sw.normalize_issue(both, self.ctx())[0], {"status:in-review"})

    def test_pr_latest_verdict_wins(self):
        pr = {"number": 7, "labels": [{"name": "ready-to-merge"}, {"name": "review:changes-requested"},
                                      {"name": "needs-approver-merge"}]}
        newer_cr = self.ctx([labeled("ready-to-merge", 90), labeled("review:changes-requested", 10)])
        self.assertEqual(sw.normalize_pr(pr, newer_cr)[0], {"ready-to-merge", "needs-approver-merge"})
        newer_ok = self.ctx([labeled("review:changes-requested", 90), labeled("ready-to-merge", 10)])
        self.assertEqual(sw.normalize_pr(pr, newer_ok)[0], {"review:changes-requested"})

    def test_stale_approver_merge_label(self):
        pr = {"number": 7, "labels": [{"name": "needs-approver-merge"}]}
        self.assertEqual(sw.normalize_pr(pr, self.ctx())[0], {"needs-approver-merge"})


def human(body, minutes):
    return {"body": body, "author_association": "MEMBER", "user": {"type": "User"}, "created_at": ago(minutes)}


def bot(body, minutes):
    return {"body": body, "author_association": "NONE", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(minutes)}


class ApproverAndMentionTests(unittest.TestCase):
    def state(self, i, comments, events=()):
        hit = sw.classify_issue(i, ictx(events, comments), NOW, TH)
        return hit[0] if hit else None

    def test_approver_reply_without_po_answer(self):
        cs = [bot("[Product Owner] ## Proposal", 300), human("approved, go", 120)]
        self.assertEqual(self.state(issue(["proposal:pending"]), cs), "approver_reply_unanswered")

    def test_approver_reply_answered(self):
        cs = [bot("[Product Owner] ## Proposal", 300), human("approved", 120), bot("[Product Owner] Shaped into #9", 100)]
        self.assertNotEqual(self.state(issue(["proposal:pending"]), cs), "approver_reply_unanswered")

    def test_approver_reply_on_parked_item(self):
        cs = [bot("[Product Owner] BACKLOG", 900), human("do it anyway", 60)]
        self.assertEqual(self.state(issue(["verdict:backlog", "P3"]), cs), "approver_reply_unanswered")

    def test_approver_reply_young_waits(self):
        cs = [bot("[Product Owner] ## Proposal", 300), human("approved", 5)]
        self.assertNotEqual(self.state(issue(["proposal:pending"]), cs), "approver_reply_unanswered")

    def test_approver_reply_elsewhere_ignored(self):
        # po-continue only serves proposal/verdict issues
        cs = [human("looks good", 120)]
        self.assertIsNone(self.state(issue(["agent:implement", "status:in-progress"]), cs))

    def test_mention_unanswered_then_asked_once(self):
        ask = human("@claude can you rebase this", 90)
        self.assertEqual(self.state(issue(["status:in-review"]), [ask]), "mention_unanswered")
        asked = bot(f"{sw.BADGE}reply again <!-- stuck-sweeper state=mention_unanswered -->", 30)
        self.assertIsNone(self.state(issue(["status:in-review"]), [ask, asked]))

    def test_old_mentions_are_left_alone(self):
        # Mentions from a previous setup (weeks old) must not be re-asked about.
        old = human("@claude can you rebase this", sw.MENTION_MAX_AGE_MIN + 60)
        self.assertIsNone(self.state(issue(["status:in-review"]), [old]))

    def test_mention_answered_by_bot(self):
        cs = [human("@claude why?", 90), bot("Because ...", 80)]
        self.assertIsNone(self.state(issue(["status:in-review"]), cs))

    def test_mention_on_pr(self):
        c = pctx()
        c["comments"] = [human("@claude fix the test", 90)]
        hit = sw.classify_pr({"number": 7, "labels": [], "isDraft": False}, c, NOW, TH)
        self.assertEqual(hit[0], "mention_unanswered")


class CapacityTests(unittest.TestCase):
    def run_check(self, paused_until, open_issues=(), dry=False):
        from unittest import mock
        import json as _json
        calls = []

        def fake_gh(args):
            calls.append(args)
            out = _json.dumps(list(open_issues)) if args[:2] == ["issue", "list"] else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")
        cap = {"paused_until": paused_until, "history": [{"a": 1}]}
        with mock.patch.object(sw, "gh", side_effect=fake_gh), \
             mock.patch.object(sw.capacity_gate, "read_hub_json", return_value=(cap, "sha")), \
             mock.patch.object(sw.capacity_gate, "write_capacity", return_value=True) as w:
            res = sw.capacity_check("o/hub", NOW, dry)
        return res, w, calls

    def test_live_pause_skips(self):
        (paused, until, _), w, calls = self.run_check(ago(-30))
        self.assertTrue(paused)
        w.assert_not_called()

    def test_passed_window_clears_and_closes_escalation(self):
        esc = {"number": 67, "title": sw.CAPACITY_ESCALATION_TITLE, "author": {"login": "app/squadhq-ops"}}
        (paused, _, note), w, calls = self.run_check(ago(60), [esc])
        self.assertFalse(paused)
        w.assert_called_once()
        self.assertEqual(w.call_args[0][1]["paused_until"], "")
        self.assertEqual(w.call_args[0][1]["history"], [{"a": 1}])
        self.assertTrue(any(c[:3] == ["issue", "close", "67"] for c in calls))
        self.assertIn("#67", note)

    def test_dry_run_changes_nothing(self):
        esc = {"number": 67, "title": sw.CAPACITY_ESCALATION_TITLE, "author": {"login": "app/squadhq-ops"}}
        _, w, calls = self.run_check(ago(60), [esc], dry=True)
        w.assert_not_called()
        self.assertFalse(any(c[:2] == ["issue", "close"] for c in calls))

    def test_clear_breaker_still_closes_stale_escalation(self):
        esc = {"number": 67, "title": sw.CAPACITY_ESCALATION_TITLE, "author": {"login": "app/squadhq-ops"}}
        (paused, _, _), w, calls = self.run_check("", [esc])
        self.assertFalse(paused)
        w.assert_not_called()
        self.assertTrue(any(c[:3] == ["issue", "close", "67"] for c in calls))


def facts(**kw):
    f = {"open_impl": {}, "merged_impl": {}, "closed_impl": {}, "gate_open": {}, "gate_merged": {},
         "gate_closed": {}, "wip_free": True}
    f.update(kw)
    return f


class ReconcileTests(unittest.TestCase):
    def rec(self, labels, f, events=(), comments=()):
        return sw.reconcile_issue(issue(labels), {"events": list(events), "comments": list(comments), "facts": f})

    def test_merged_design_clears_gate_and_dispatches(self):
        fx = self.rec(["needs-design", "status:in-review"], facts(gate_merged={1: 277}))
        self.assertEqual(fx["remove"], {"needs-design", "status:in-review"})
        self.assertEqual(fx["add"], ["agent:implement"])

    def test_merged_design_at_wip_limit_queues(self):
        fx = self.rec(["needs-design", "status:in-review"], facts(gate_merged={1: 277}, wip_free=False))
        self.assertEqual(fx["add"], ["triaged:ready"])

    def test_open_design_pr_is_in_review(self):
        fx = self.rec(["needs-design", "status:in-progress"], facts(gate_open={1: 9}))
        self.assertEqual((fx["remove"], fx["add"]), ({"status:in-progress"}, ["status:in-review"]))
        self.assertIsNone(self.rec(["needs-design", "status:in-review"], facts(gate_open={1: 9})))

    def test_closed_design_pr_escalates(self):
        fx = self.rec(["needs-design", "status:in-review"], facts(gate_closed={1: 9}))
        self.assertIn("closed without merging", fx["escalate"])
        self.assertEqual(fx["remove"], {"status:in-review"})

    def test_open_impl_pr_moves_to_review(self):
        fx = self.rec(["agent:implement", "status:in-progress"], facts(open_impl={1: 30}))
        self.assertEqual((fx["remove"], fx["add"]), ({"status:in-progress"}, ["status:in-review"]))

    def test_merged_impl_closes_ticket(self):
        fx = self.rec(["agent:implement", "status:in-review"], facts(merged_impl={1: 30}))
        self.assertEqual(fx["close"], 30)

    def test_closed_impl_after_dispatch_escalates(self):
        f = facts(closed_impl={1: (30, sw.parse_ts(ago(10)))})
        fx = self.rec(["agent:implement", "status:in-review"], f, events=[labeled("agent:implement", 100)])
        self.assertIn("closed without merging", fx["escalate"])
        # ...but an older closed PR before a fresh dispatch is history, not news
        self.assertNotIn("escalate", {k for k, v in (self.rec(["agent:implement"], f,
                         events=[labeled("agent:implement", 5)]) or {}).items() if v})

    def test_stale_in_review_removed_but_delivered_report_kept(self):
        fx = self.rec(["agent:implement", "status:in-review"], facts(), events=[labeled("status:in-review", 60)])
        self.assertEqual(fx["remove"], {"status:in-review"})
        delivered = {"body": "[Engineer] result...\nDelivered — moved to review. Approver closes when accepted.",
                     "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(50)}
        self.assertIsNone(self.rec(["agent:implement", "status:in-review"], facts(),
                                   events=[labeled("status:in-review", 60)], comments=[delivered]))

    def test_overridden_undispatched_is_queued(self):
        self.assertEqual(self.rec(["verdict:overridden"], facts())["add"], ["triaged:ready"])
        self.assertIsNone(self.rec(["verdict:overridden", "triaged:ready"], facts()))

    def test_consistent_ticket_untouched(self):
        self.assertIsNone(self.rec(["agent:implement", "status:in-review"], facts(open_impl={1: 30})))


class PrFactsTests(unittest.TestCase):
    def test_classifies_prs(self):
        open_prs = [{"number": 40, "body": "Closes #1", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": [{"number": 1}]},
                    {"number": 41, "body": "Design\nGate-for: #2", "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": []}]
        closed = [{"number": 30, "body": "Gate-for: #3", "author": {"login": "app/squadhq-ops"}, "mergedAt": "2026-09-28T00:00:00Z", "closedAt": "2026-09-28T00:00:00Z"},
                  {"number": 31, "body": "Closes #4", "author": {"login": "app/squadhq-ops"}, "mergedAt": None, "closedAt": "2026-09-27T00:00:00Z"},
                  {"number": 32, "body": "Closes #5", "author": {"login": "app/squadhq-ops"}, "mergedAt": "2026-09-27T00:00:00Z", "closedAt": "2026-09-27T00:00:00Z"},
                  {"number": 20, "body": "Closes #1", "author": {"login": "app/squadhq-ops"}, "mergedAt": None, "closedAt": "2026-09-01T00:00:00Z"}]
        f = sw.pr_facts(open_prs, closed, 5)
        self.assertEqual(f["open_impl"], {1: 40})
        self.assertEqual(f["gate_open"], {2: 41})
        self.assertEqual(f["gate_merged"], {3: 30})
        self.assertEqual(f["closed_impl"][4][0], 31)
        self.assertNotIn(1, f["closed_impl"])        # superseded by open #40
        self.assertEqual(f["merged_impl"], {5: 32})
        self.assertTrue(f["wip_free"])
        self.assertFalse(sw.pr_facts(open_prs * 3, [], 5)["wip_free"])


class StallTests(unittest.TestCase):
    def test_in_progress_without_pr_or_word_is_stalled(self):
        c = ictx([labeled("agent:implement", 300), labeled("status:in-progress", 290)])
        hit = sw.classify_issue(issue(["agent:implement", "status:in-progress"]), c, NOW, TH)
        self.assertEqual(hit[0], "in_progress_stalled")

    def test_in_progress_with_engineer_word_is_not(self):
        c = ictx([labeled("status:in-progress", 290)],
                 comments=[{"body": "[Engineer] blocked on X", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(100)}])
        self.assertIsNone(sw.classify_issue(issue(["agent:implement", "status:in-progress"]), c, NOW, TH))

    def test_design_in_progress_stalled(self):
        c = ictx([labeled("status:in-progress", 290)])
        hit = sw.classify_issue(issue(["needs-design", "status:in-progress"]), c, NOW, TH)
        self.assertEqual(hit[0], "design_stalled")



class ImpactAndDependencyTests(unittest.TestCase):
    def test_impact_missing_triggers_po(self):
        hit = sw.classify_issue(issue(["triaged:ready", "P2"], body="Do X."), ictx(), NOW, TH)
        self.assertEqual(hit[0], "impact_missing")
        self.assertIsNone(sw.classify_issue(issue(["triaged:ready", "P2"]), ictx(), NOW, TH))

    def test_resolved_impact_block_redispatches(self):
        stop = {"body": "[Engineer] Blocked — missing Why this matters", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(80)}
        c = ictx([labeled("agent:implement", 90)], comments=[stop])
        # body now HAS the section -> the stop is stale -> plain re-dispatch
        self.assertEqual(sw.classify_issue(issue(["agent:implement"]), c, NOW, TH)[0], "dispatched_not_started")
        # body still lacks it -> impact_missing (PO adds it first)
        self.assertEqual(sw.classify_issue(issue(["agent:implement"], body="x"), c, NOW, TH)[0], "impact_missing")

    def dep_ctx(self, comments, **facts_kw):
        return {"events": [], "comments": comments, "facts": facts(**facts_kw)}

    def test_dependency_block_lifted_when_dep_has_pr(self):
        body = WHY + "\nAfter: #275"
        block = {"body": "[Engineer] Blocked — dependency ordering. After: #275 has no PR", "user": {"type": "Bot", "login": "squadhq-ops[bot]"},
                 "created_at": ago(300)}
        sweeper = {"body": sw.BADGE + "Stuck ... <!-- stuck-sweeper state=implement_stopped escalated -->",
                   "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(200)}
        it = issue(["agent:implement", "needs-human"], body=body)
        fx = sw.dependency_ready(it, self.dep_ctx([block, sweeper], open_impl={275: 290}))
        self.assertEqual(fx["remove"], {"needs-human", "agent:implement"})
        self.assertEqual(fx["add"], ["agent:implement"])
        self.assertIsNone(sw.dependency_ready(it, self.dep_ctx([block])))  # dep has no PR yet

    def test_dependency_block_kept_if_approver_spoke(self):
        body = WHY + "\nAfter: #275"
        block = {"body": "[Engineer] Blocked — dependency #275", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(300)}
        approver = {"body": "hold this one", "author_association": "MEMBER", "user": {"type": "User"},
                   "created_at": ago(100)}
        it = issue(["agent:implement", "needs-human"], body=body)
        self.assertIsNone(sw.dependency_ready(it, self.dep_ctx([block, approver], merged_impl={275: 290})))


class DispatchTests(unittest.TestCase):
    def plan(self, issues, open_prs=0, max_per_run=2, share=0.2, **fkw):
        f = facts(**fkw)
        f["open_pr_count"] = open_prs
        return [it["number"] for it, _ in sw.plan_dispatch(issues, f, 5, max_per_run, share, NOW)]

    def t(self, n, labels, days=1, body=WHY):
        return issue(labels, n=n, created=ago(days * 1440), body=body)

    def test_accepted_debt_and_ready_tickets_dispatched_by_priority(self):
        issues = [self.t(1, ["debt", "P2"], 3), self.t(2, ["triaged:ready", "P1"], 1), self.t(3, ["triaged:ready", "P2"], 5)]
        self.assertEqual(self.plan(issues), [2, 3])

    def test_wip_limit_respected_counting_inflight(self):
        issues = [self.t(1, ["triaged:ready", "P1"]), self.t(9, ["agent:implement"])]
        self.assertEqual(self.plan(issues, open_prs=4), [])       # 4 PRs + 1 in flight = 5
        self.assertEqual(self.plan(issues, open_prs=3), [1])

    def test_blocked_or_unready_skipped(self):
        issues = [self.t(1, ["triaged:ready", "P3"]), self.t(2, ["triaged:ready", "needs-human"]),
                  self.t(3, ["triaged:ready"], body="no impact"), self.t(4, ["P2"]),
                  self.t(5, ["debt", "needs-design"])]
        self.assertEqual(self.plan(issues), [])

    def test_external_content_never_dispatched(self):
        issues = [self.t(1, ["triaged:ready", "channel:external", "P1"]),
                  self.t(2, ["triaged:ready", "channel:blog", "P2"])]
        self.assertEqual(self.plan(issues), [2])

    def test_after_dependency_order(self):
        dep = self.t(7, ["triaged:ready", "P1"], body=WHY + "\nAfter: #275")
        self.assertEqual(self.plan([dep]), [])
        self.assertEqual(self.plan([dep], open_impl={275: 300}), [7])
        self.assertEqual(self.plan([dep], merged_impl={275: 300}), [7])

    def test_closed_dependency_unblocks(self):
        # the dep was delivered via a `Refs` PR, so
        # only its closed state can ever clear the After: gate.
        dep = self.t(7, ["triaged:ready", "P1"], body=WHY + "\nAfter: #325")
        self.assertEqual(self.plan([dep], closed_issues={325}), [7])

    def test_closed_dependency_lifts_engineer_block(self):
        block = {"body": "[Engineer] Blocked — dependency ordering. After: #325 has no PR", "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(300)}
        it = issue(["agent:implement", "needs-human"], body=WHY + "\nAfter: #325")
        sweeper = {"body": sw.BADGE + "Stuck ... <!-- stuck-sweeper state=implement_stopped escalated -->",
                   "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(200)}
        ctx = {"events": [], "comments": [block, sweeper], "facts": facts(closed_issues={325})}
        fx = sw.dependency_ready(it, ctx)
        self.assertEqual(fx["add"], ["agent:implement"])
        self.assertIn("#325 is closed", fx["reason"])

    def test_block_reason_names_each_blocker(self):
        f = facts()
        self.assertIn("not shaped", sw.block_reason(self.t(1, ["P2"]), f))
        self.assertIn("debt-proposal", sw.block_reason(self.t(2, ["triaged:ready", "debt-proposal"]), f))
        self.assertIn("Why this matters", sw.block_reason(self.t(3, ["triaged:ready"], body="x"), f))
        self.assertIn("#325", sw.block_reason(self.t(4, ["triaged:ready"], body=WHY + "\nAfter: #325"), f))
        self.assertEqual(sw.block_reason(self.t(5, ["debt", "P2"]), f), "")

    def test_po_why_comment_makes_a_ticket_dispatchable(self):
        # The Product Owner posts "Why this matters" as a comment; the author's text stays theirs.
        f = facts()
        it = self.t(6, ["triaged:ready"], body="Remove the leftovers.")
        self.assertIn("Why this matters", sw.block_reason(it, f))
        po = {"body": "[Product Owner] Clear as written.\n\n**Why this matters:**\n Business: a.",
              "user": {"type": "Bot", "login": "squadhq-ops[bot]"}, "created_at": ago(5)}
        stranger = {"body": "Why this matters: trust me", "author_association": "NONE",
                    "user": {"type": "User", "login": "x"}, "created_at": ago(5)}
        self.assertTrue(sw.has_why(it, [po]))
        self.assertFalse(sw.has_why(it, [stranger]))
        calls = []
        orig = sw.gh_list
        sw.gh_list = lambda path: calls.append(path) or [po]
        try:
            sw.enrich_why("acme/web", [it, self.t(7, ["P2"], body="x")])
        finally:
            sw.gh_list = orig
        self.assertEqual(calls, ["repos/acme/web/issues/6/comments"])  # only ready tickets are looked up
        self.assertEqual(sw.block_reason(it, f), "")

    def test_debt_share_prefers_feature_work(self):
        inflight_debt = self.t(9, ["agent:implement", "debt"])
        issues = [inflight_debt, self.t(1, ["debt", "P1"], 5), self.t(2, ["triaged:ready", "P2"], 1)]
        self.assertEqual(self.plan(issues, max_per_run=1), [2])     # debt already 100% of in-flight
        self.assertEqual(self.plan([self.t(1, ["debt", "P1"])], max_per_run=1), [1])  # nothing else waiting

    def test_per_run_cap(self):
        issues = [self.t(i, ["triaged:ready", "P2"], i) for i in range(1, 6)]
        self.assertEqual(len(self.plan(issues, max_per_run=2)), 2)


class IdleAlarmTests(unittest.TestCase):
    def closed_pr(self, hours):
        return {"number": 1, "closedAt": ago(hours * 60)}

    def test_idle_after_quiet_period(self):
        idle, since = sw.idle_state([], [], [self.closed_pr(30)], facts(), [], 24, NOW)
        self.assertTrue(idle)
        self.assertIsNotNone(since)

    def test_not_idle_with_open_pr_inflight_dispatch_or_recent_close(self):
        it = issue(["triaged:ready"])
        self.assertFalse(sw.idle_state([], [{"isDraft": False}], [], facts(), [], 24, NOW)[0])
        self.assertFalse(sw.idle_state([issue(["agent:implement"])], [], [], facts(), [], 24, NOW)[0])
        self.assertFalse(sw.idle_state([it], [], [], facts(), [(it, "r")], 24, NOW)[0])
        self.assertFalse(sw.idle_state([], [], [self.closed_pr(3)], facts(), [], 24, NOW)[0])

    def test_approver_parked_dispatch_does_not_count_as_inflight(self):
        parked = issue(["agent:implement", "needs-human"])
        self.assertTrue(sw.idle_state([parked], [], [self.closed_pr(30)], facts(), [], 24, NOW)[0])

    def test_body_lists_blockers(self):
        a = dict(issue(["triaged:ready"], n=326, body=WHY + "\nAfter: #325"), title="Harden DCR")
        b = dict(issue(["triaged:ready", "debt-proposal"], n=338), title="Settings page")
        c = dict(issue(["proposal:pending"], n=328), title="Claude directory")
        body = sw.idle_body([a, b, c], facts(), None)
        self.assertIn("#326 Harden DCR [shaped] — waits on #325", body)
        self.assertIn("#338 Settings page [ILLEGAL label set] — blocking label `debt-proposal`", body)
        self.assertIn("`proposal:pending` (waiting on the PO): #328", body)

    def test_shipped_threshold(self):
        self.assertEqual(flow_reader.get_flood_guard("stuck_sweeper")["idle_alarm_hours"], 24)


class DebtIntakeTests(unittest.TestCase):
    def prop(self, n, title, labels=("debt-proposal", "dept:backend"), bot=True, body=WHY, minutes=60):
        return issue(list(labels), n=n, created=ago(minutes), bot=bot, body=f"{title}\n\n{body}") | {"title": title}

    def test_bot_proposals_with_why_are_accepted_with_a_priority(self):
        plan = sw.plan_debt_intake([
            self.prop(1, "Split OrderService.java"),
            self.prop(2, "CVE in log4j", labels=("debt-proposal", "severity:high")),
            self.prop(3, "Old P1 item ReportBuilder", labels=("debt-proposal", "P1"))], [])
        self.assertEqual([(i["number"], a, d) for i, a, d in plan],
                         [(1, "accept", "P2"), (2, "accept", "P0"), (3, "accept", "P1")])

    def test_untrusted_or_unjustified_or_held_proposals_wait(self):
        plan = sw.plan_debt_intake([
            self.prop(1, "Split OrderService.java", bot=False),
            self.prop(2, "Split PaymentService.java", body="no section"),
            self.prop(3, "Split RefundService.java", labels=("debt-proposal", "needs-human"))], [])
        self.assertEqual(plan, [])

    def test_repeats_of_open_or_settled_debt_are_duplicates(self):
        open_debt = issue(["debt", "P2"], n=10, body="Split OrderService.java\n\n" + WHY) | {"title": "Split OrderService.java"}
        settled = {"number": 11, "title": "Refactor InvoiceMapper", "body": "", "author": {"login": "app/squadhq-ops", "is_bot": True}}
        plan = sw.plan_debt_intake([
            open_debt,
            self.prop(1, "OrderService.java is too large", minutes=50),
            self.prop(2, "InvoiceMapper duplicates code", minutes=40),
            self.prop(3, "Cache config KeyStoreLoader", minutes=30),
            self.prop(4, "KeyStoreLoader again", minutes=20)], [settled])
        self.assertEqual([(i["number"], a, d if a == "accept" else d["number"]) for i, a, d in plan],
                         [(1, "duplicate", 10), (2, "duplicate", 11), (3, "accept", "P2"), (4, "duplicate", 3)])
