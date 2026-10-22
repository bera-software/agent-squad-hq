#!/usr/bin/env python3
"""
Tests for config/flows.yml label_states and the resolver/validator in
squadhq.flow_reader, plus the stuck sweeper's use of them.

    python3 -m unittest discover tests

The real config/flows.yml is loaded, so a label_states edit that makes a
real-world label set illegal (or an illegal one legal) fails here.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from squadhq import flow_diagram  # noqa: E402
from squadhq import flow_reader  # noqa: E402
from squadhq import stuck_sweeper as sw  # noqa: E402

CFG = flow_reader.get_label_states("issue")
DATA = flow_reader.load()


def state(*labels):
    combo, mods = flow_reader.resolve_state(labels, CFG)
    return (combo["state"] if combo else None), mods


def issue(*labels, n=1):
    return {"number": n, "labels": [{"name": l} for l in labels]}


NCTX = {"events": [], "comments": [], "open_pr_closes": set(), "gate_prs": set(), "merged_gate": set()}


class ResolveTests(unittest.TestCase):
    def test_every_combination_seen_resolves(self):
        # Every open/closed state-label set a live project carried
        # 2026-10-17 that no repair rewrites first.
        seen = [(), ("debt",), ("agent:implement",), ("agent:implement", "status:in-review"),
                ("agent:implement", "debt"), ("needs-human",), ("proposal:pending",),
                ("agent:implement", "debt", "status:in-review"), ("needs-design", "status:in-review"),
                ("debt", "needs-human"), ("triaged:ready",), ("status:in-review",), ("debt-proposal",),
                ("needs-human", "proposal:pending"), ("debt", "status:in-review")]
        for labels in seen:
            self.assertIsNotNone(state(*labels)[0], labels)

    def test_metadata_labels_never_decide_state(self):
        self.assertEqual(state("dept:frontend", "P2", "verdict:proceed", "channel:blog")[0],
                         "ticket-lifecycle.new")
        self.assertEqual(state("triaged:ready", "P1", "dept:backend")[0], "ticket-lifecycle.shaped")

    def test_modifiers_keep_the_underlying_state(self):
        self.assertEqual(state("agent:implement", "status:in-progress", "debt", "needs-human"),
                         ("ticket-lifecycle.in_progress", ["needs-human", "debt"]))
        self.assertEqual(state("debt", "needs-human"), ("debt-intake.accepted", ["needs-human"]))
        self.assertEqual(state("debt")[0], "debt-intake.accepted")

    def test_illegal_sets(self):
        for labels in [("status:in-progress",), ("triaged:ready", "status:in-review"),
                       ("debt-proposal", "status:in-progress"), ("proposal:pending", "status:in-review")]:
            self.assertIsNone(state(*labels)[0], labels)


class RepairTests(unittest.TestCase):
    def test_ready_debt_proposal_becomes_debt(self):
        r = flow_reader.match_repair({"debt-proposal", "triaged:ready", "P2"}, CFG)
        self.assertEqual(flow_reader.apply_repair({"debt-proposal", "triaged:ready"}, r), {"debt"})

    def test_parked_dispatch_escalates_unless_overridden(self):
        self.assertTrue(flow_reader.match_repair({"P3", "agent:implement"}, CFG)["escalate"])
        r = flow_reader.match_repair({"P3", "agent:implement", "verdict:overridden"}, CFG)
        self.assertFalse(r and r.get("escalate"))

    def test_every_repaired_set_is_legal(self):
        for r in CFG["repairs"]:
            if r.get("escalate"):
                continue
            trigger = {g[0] for g in r["groups"]}
            self.assertIsNotNone(flow_reader.resolve_state(flow_reader.apply_repair(trigger, r), CFG)[0], r)


class ValidatorTests(unittest.TestCase):
    def problems(self, mutate):
        data = copy.deepcopy(DATA)
        mutate(data["label_states"]["issue"])
        return flow_reader._validate_label_states(data)

    def test_shipped_config_valid(self):
        self.assertEqual(flow_reader._validate_label_states(DATA), [])

    def test_unknown_label(self):
        self.assertTrue(self.problems(lambda c: c["state_labels"].append("nope:label")))

    def test_unknown_flow_state(self):
        self.assertTrue(self.problems(lambda c: c["combos"][0].update(state="ticket-lifecycle.limbo")))

    def test_duplicate_combo(self):
        self.assertTrue(self.problems(lambda c: c["combos"].append(dict(c["combos"][1]))))

    def test_repair_landing_on_illegal_set(self):
        bad = {"groups": [["triaged:ready"]], "add": ["status:in-review"], "reason": "x"}
        self.assertTrue(self.problems(lambda c: c["repairs"].append(bad)))


class SweeperTests(unittest.TestCase):
    def test_illegal_set_escalates(self):
        out = sw.normalize_issue(issue("triaged:ready", "status:in-review"), NCTX)
        self.assertEqual(out[0], "escalate")
        self.assertIn("label_states", out[1])

    def test_legal_set_untouched(self):
        self.assertIsNone(sw.normalize_issue(issue("agent:implement", "status:in-review", "debt"), NCTX))

    def test_fact_repairs_still_win_over_illegal(self):
        both = issue("agent:implement", "status:in-progress", "status:in-review")
        self.assertEqual(sw.normalize_issue(both, NCTX)[0], {"status:in-review"})

    def test_state_name(self):
        self.assertEqual(sw.state_name(issue("agent:implement", "debt")), "dispatched +debt")
        self.assertEqual(sw.state_name(issue("status:in-progress")), "ILLEGAL label set")


PR_CFG = flow_reader.get_label_states("pr")


def pr_state(*labels):
    combo, _ = flow_reader.resolve_state(labels, PR_CFG)
    return combo["state"] if combo else None


def labeled(label, minute):
    return {"event": "labeled", "label": {"name": label}, "created_at": f"2026-10-17T10:{minute:02d}:00Z"}


class PrStateTests(unittest.TestCase):
    def test_every_combination_seen_resolves(self):
        seen = [(), ("ready-to-merge",), ("ready-to-merge", "risk:low"),
                ("needs-approver-merge", "ready-to-merge", "risk:low"),
                ("needs-approver-merge", "ready-to-merge", "risk:medium"), ("review:changes-requested",),
                ("needs-human", "review:changes-requested"), ("needs-human", "risk:medium"),
                ("needs-human", "ready-to-merge", "risk:low"),
                ("needs-approver-merge", "needs-human", "ready-to-merge", "risk:high"), ("needs-human",)]
        for labels in seen:
            self.assertIsNotNone(pr_state(*labels), labels)

    def test_states(self):
        self.assertEqual(pr_state("ready-to-merge", "needs-approver-merge", "risk:medium"), "review-loop.approved")
        self.assertEqual(pr_state("review:changes-requested", "risk:low"), "review-loop.changes_requested")
        self.assertEqual(pr_state(), "review-loop.review_pending")

    def test_two_risk_labels_illegal(self):
        self.assertIsNone(pr_state("ready-to-merge", "risk:low", "risk:high"))
        self.assertEqual(flow_reader.exclusive_violations({"risk:low", "risk:high", "x"}, PR_CFG),
                         [["risk:high", "risk:low"]])

    def test_illegal_state_sets(self):
        self.assertIsNone(pr_state("needs-approver-merge", "review:changes-requested", "ready-to-merge"))


class PrSweeperTests(unittest.TestCase):
    def ctx(self, *events):
        return {"events": list(events), "comments": []}

    def test_status_labels_stripped_from_prs(self):
        out = sw.normalize_pr(issue("status:in-review", "needs-human"), self.ctx())
        self.assertEqual(out[0], {"status:in-review"})

    def test_newest_risk_label_kept(self):
        pr = issue("ready-to-merge", "risk:low", "risk:high")
        out = sw.normalize_pr(pr, self.ctx(labeled("risk:high", 1), labeled("risk:low", 5)))
        self.assertEqual(out[0], {"risk:high"})

    def test_two_risk_labels_without_history_escalate(self):
        out = sw.normalize_pr(issue("ready-to-merge", "risk:low", "risk:high"), self.ctx())
        self.assertEqual(out[0], "escalate")

    def test_latest_verdict_wins(self):
        pr = issue("ready-to-merge", "review:changes-requested", "needs-approver-merge")
        out = sw.normalize_pr(pr, self.ctx(labeled("ready-to-merge", 1), labeled("review:changes-requested", 5)))
        self.assertEqual(out[0], {"ready-to-merge", "needs-approver-merge"})

    def test_legal_untouched(self):
        self.assertIsNone(sw.normalize_pr(issue("ready-to-merge", "needs-approver-merge", "risk:medium"), self.ctx()))


def ev(kind, label, ts, actor="squadhq-ops[bot]"):
    return {"event": kind, "label": {"name": label}, "created_at": f"2026-10-{ts}Z", "actor": {"login": actor}}


class LostReadyTests(unittest.TestCase):
    """design gate cleared -> triaged:ready (WIP full) -> CoS removed it."""
    def test_cos_removal_is_lost(self):
        events = [ev("labeled", "needs-design", "08T23:50:00"), ev("unlabeled", "needs-design", "08T23:58:00"),
                  ev("labeled", "triaged:ready", "08T23:58:00"), ev("unlabeled", "triaged:ready", "11T12:01:00")]
        lost = sw.lost_ready(issue("dept:frontend", "P2"), events)
        self.assertEqual(lost["at"][:10], "2026-10-11")

    def test_handed_to_design_in_same_edit_not_lost(self):
        events = [ev("labeled", "triaged:ready", "08T19:07:00"), ev("unlabeled", "triaged:ready", "08T23:50:00"),
                  ev("labeled", "needs-design", "08T23:50:00")]
        self.assertIsNone(sw.lost_ready(issue("needs-design", "P2"), events))
        # ...and the approver later clearing needs-design does not make it lost.
        events.append(ev("unlabeled", "needs-design", "09T07:31:00", actor="octocat"))
        self.assertIsNone(sw.lost_ready(issue("P2"), events))

    def test_approver_removal_is_a_decision(self):
        events = [ev("labeled", "triaged:ready", "08T19:07:00"),
                  ev("unlabeled", "triaged:ready", "09T19:07:00", actor="octocat")]
        self.assertIsNone(sw.lost_ready(issue("P2"), events))

    def test_parked_or_requeued_not_lost(self):
        events = [ev("labeled", "triaged:ready", "08T19:07:00"), ev("unlabeled", "triaged:ready", "09T19:07:00")]
        self.assertIsNone(sw.lost_ready(issue("P3"), events))
        self.assertIsNone(sw.lost_ready(issue("P2", "triaged:ready"), events))
        events.append(ev("labeled", "agent:implement", "10T19:07:00"))
        self.assertIsNone(sw.lost_ready(issue("P2"), events + [ev("unlabeled", "agent:implement", "10T19:09:00")]))

    def test_approver_waiting_removal_not_lost(self):
        # CoS removed needs-human and triaged:ready together, question unanswered.
        events = [ev("labeled", "triaged:ready", "01T10:00:00"), ev("labeled", "needs-human", "01T11:00:00"),
                  ev("unlabeled", "needs-human", "02T10:00:00"), ev("unlabeled", "triaged:ready", "02T10:00:00")]
        self.assertIsNone(sw.lost_ready(issue("dept:growth", "P2"), events))

    def test_reconcile_requeues(self):
        events = [ev("labeled", "triaged:ready", "08T19:07:00"), ev("unlabeled", "triaged:ready", "11T12:01:00")]
        facts = {"merged_impl": {}, "open_impl": {}, "closed_impl": {}, "gate_merged": {}, "gate_open": {},
                 "gate_closed": {}, "wip_free": True, "closed_issues": set()}
        fx = sw.reconcile_issue({**issue("dept:frontend", "P2", n=313), "body": ""},
                                {"events": events, "comments": [], "facts": facts})
        self.assertEqual(fx["add"], ["triaged:ready"])


class UmbrellaTests(unittest.TestCase):
    PO = {"body": "[Product Owner] Consensus.\n\nShaped into: #325, #326\n\n<!-- team-thread-complete -->",
          "user": {"login": "squadhq-ops[bot]"}}

    def ctx(self, closed):
        return {"comments": [self.PO], "facts": {"closed_issues": set(closed)}}

    def test_all_children_closed(self):
        self.assertEqual(sw.umbrella_done(issue("proposal:pending", "verdict:proceed"), self.ctx({325, 326})),
                         [325, 326])

    def test_open_child_keeps_it(self):
        self.assertIsNone(sw.umbrella_done(issue("proposal:pending"), self.ctx({325})))

    def test_no_shaped_line_or_label(self):
        self.assertIsNone(sw.umbrella_done(issue("proposal:pending"),
                                           {"comments": [{"body": "[Product Owner] Approver — reply"}],
                                            "facts": {"closed_issues": {1}}}))
        self.assertIsNone(sw.umbrella_done(issue("P2"), self.ctx({325, 326})))


class DeclineExpiryTests(unittest.TestCase):
    NOW = sw.parse_ts("2026-10-17T12:00:00Z")
    EVENTS = [ev("labeled", "verdict:decline", "05T10:00:00", actor="claude[bot]")]

    def expired(self, labels=("verdict:decline", "P3"), events=None, comments=()):
        return sw.park_expired(issue(*labels), self.EVENTS if events is None else events,
                               list(comments), self.NOW, 7)

    def test_old_decline_expires(self):
        self.assertIsNotNone(self.expired())

    def test_recent_decline_kept(self):
        self.assertIsNone(self.expired(events=[ev("labeled", "verdict:decline", "15T10:00:00")]))

    def test_approver_reply_keeps_it(self):
        reply = {"author_association": "OWNER", "created_at": "2026-10-06T09:00:00Z", "user": {"login": "octocat"}}
        self.assertIsNone(self.expired(comments=[reply]))
        bot = {"author_association": "MEMBER", "created_at": "2026-10-06T09:00:00Z", "user": {"login": "x[bot]"}}
        self.assertIsNotNone(self.expired(comments=[bot]))

    def test_overridden_and_backlog_kept(self):
        self.assertIsNone(self.expired(labels=("verdict:decline", "verdict:overridden")))
        self.assertIsNone(self.expired(labels=("verdict:backlog", "P3")))

    def test_backlog_uses_its_own_label_and_window(self):
        events = [ev("labeled", "verdict:backlog", "01T10:00:00")]
        backlog = issue("verdict:backlog", "P3")
        self.assertIsNone(sw.park_expired(backlog, events, [], self.NOW, 30, "verdict:backlog"))
        self.assertIsNotNone(sw.park_expired(backlog, events, [], self.NOW, 14, "verdict:backlog"))

    def test_shipped_config(self):
        guard = flow_reader.get_flood_guard("stuck_sweeper")
        self.assertEqual((guard["decline_close_days"], guard["backlog_close_days"]), (7, 30))
        self.assertEqual(set(sw.PARK_EXPIRY), {"verdict:decline", "verdict:backlog"})


class ValidatorExclusiveTests(unittest.TestCase):
    def test_exclusive_group_must_be_metadata(self):
        data = copy.deepcopy(DATA)
        data["label_states"]["pr"]["exclusive"].append(["ready-to-merge", "needs-human"])
        self.assertTrue(flow_reader._validate_label_states(data))


class DiagramTests(unittest.TestCase):
    def test_doc_renders_tables(self):
        md = "\n".join(flow_diagram.label_states_markdown(DATA))
        self.assertIn("## Label states — issues", md)
        self.assertIn("| `agent:implement` + `status:in-progress` | `ticket-lifecycle.in_progress` |", md)
        self.assertIn("add `debt`", md)
        self.assertIn("## Label states — PRs", md)
        self.assertIn("`risk:low` / `risk:medium` / `risk:high`", md)


if __name__ == "__main__":
    unittest.main()
