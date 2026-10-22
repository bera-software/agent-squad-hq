#!/usr/bin/env python3
"""
Tests for squadhq.hub_janitor's pure planner, against the real
config/flows.yml flood-guards.hub_janitor rules.

    python3 -m unittest discover tests
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from squadhq import flow_reader  # noqa: E402
from squadhq import hub_janitor as hj  # noqa: E402

NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.timezone.utc)
CFG = flow_reader.get_flood_guard("hub_janitor")
_n = iter(range(1, 10_000))


def issue(title, days_ago, labels=(), human=False, comments=(), body=""):
    return {"number": next(_n), "title": title, "body": body,
            "labels": [{"name": l} for l in labels],
            "author": {"login": "octocat" if human else "app/squadhq-ops", "is_bot": not human},
            "createdAt": (NOW - datetime.timedelta(days=days_ago)).isoformat(),
            "comments": [{"authorAssociation": a} for a in comments], "isPinned": False}


def closed_numbers(actions):
    out = set()
    for a in actions:
        out |= {d["number"] for d in a["close"]} if a["kind"] == "merge" else {a["close"]["number"]}
    return out


class ProjectReportTests(unittest.TestCase):
    """Department reports in project repos: 14-day flat retention, label-only,
    no title merging."""
    PCFG = hj.project_cfg(CFG)

    def test_retention_is_flat_14_days(self):
        g = [issue(f"Growth nightly — d{d} — nothing new", d, labels=["report"]) for d in (1, 10, 13, 15, 30)]
        actions, _ = hj.plan(g, NOW, self.PCFG)
        self.assertEqual(closed_numbers(actions), {g[3]["number"], g[4]["number"]})

    def test_no_merging_in_projects(self):
        same = [issue("SEO weekly — 2026-10-01", d, labels=["report"]) for d in (1, 2)]
        self.assertEqual(hj.plan(same, NOW, self.PCFG)[0], [])

    def test_only_report_labeled_issues_considered(self):
        work = issue("SEO weekly — looks like a report", 40, labels=["dept:growth"])
        rep = issue("SEO weekly — real", 1, labels=["report", "dept:growth"])
        self.assertEqual(hj.only_reports([work, rep], "report"), [rep])

    def test_approver_engaged_report_protected(self):
        g = [issue("Legal weekly — new", 1, labels=["report"]),
             issue("Legal weekly — old", 30, labels=["report"], comments=["OWNER"])]
        self.assertEqual(hj.plan(g, NOW, self.PCFG)[0], [])

    def test_hub_config_untouched(self):
        self.assertTrue(CFG["merge_identical_titles"])
        self.assertEqual({s_["key"]: s_["retain_days"] for s_ in CFG["series"]}["daily-brief"], 0)


class PlanTests(unittest.TestCase):
    def test_only_newest_brief_kept(self):
        briefs = [issue(f"Daily Brief — d{i}", i) for i in (0, 1, 2, 10)]
        actions, _ = hj.plan(briefs, NOW, CFG)
        self.assertEqual(closed_numbers(actions), {i["number"] for i in briefs[1:]})
        self.assertTrue(all(a["superseded_by"] == briefs[0]["number"] for a in actions))

    def test_newest_of_series_always_kept(self):
        only = issue("Lessons harvest — 2026-01-01", 200)
        self.assertEqual(hj.plan([only], NOW, CFG)[0], [])

    def test_weekly_series_keeps_newest(self):
        w = [issue(f"Finance weekly — w{i}", d) for i, d in enumerate((1, 8, 16))]
        actions, _ = hj.plan(w, NOW, CFG)
        self.assertEqual(closed_numbers(actions), {w[1]["number"], w[2]["number"]})

    def test_past_cost_ledgers_close_current_kept(self):
        months = [issue(f"Cost ledger — 2026-{m:02d}", d) for m, d in ((9, 25), (8, 56), (7, 87))]
        actions, _ = hj.plan(months, NOW, CFG)
        self.assertEqual(closed_numbers(actions), {months[1]["number"], months[2]["number"]})

    def test_legacy_hub_dept_report_expires_though_newest(self):
        legal = issue("Legal weekly — 2026-09-01", 25, labels=["escalation"])
        seo = issue("SEO weekly — 2026-09-20", 6)
        actions, _ = hj.plan([legal, seo], NOW, CFG)
        self.assertEqual(closed_numbers(actions), {legal["number"]})

    def test_identical_titles_merge_into_newest(self):
        dups = [issue("ESCALATION [UNKNOWN]: po-shape failed (o/r)", d,
                      body=f"**Run:** https://github.com/o/r/actions/runs/{d}") for d in (1, 5, 9)]
        actions, _ = hj.plan(dups, NOW, CFG)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["keep"], dups[0]["number"])
        self.assertEqual({d["number"] for d in actions[0]["close"]}, {dups[1]["number"], dups[2]["number"]})
        self.assertEqual(hj.run_link(dups[1]), "https://github.com/o/r/actions/runs/5")

    def test_protected_never_touched(self):
        protected = [
            issue("Claude capacity window exhausted", 40), issue("Claude capacity window exhausted", 41),
            issue("Draft: HN — discussed", 40, labels=["awaiting-approval"], comments=["OWNER"]),
            issue("Daily Brief — old but discussed", 40, comments=["NONE", "MEMBER"]),
            issue("growth report — by hand", 40, human=True),
            issue("Quarterly Architecture Review 2026-Q3", 40),
        ]
        newest = issue("Daily Brief — today", 0)
        actions, kept = hj.plan(protected + [newest], NOW, CFG)
        self.assertEqual(actions, [])
        self.assertEqual(len(kept), len(protected))

    def test_unmatched_issue_left_alone(self):
        one_off = issue("OKR refresh due — Q4 2026", 90, labels=["dept:engineering"])
        self.assertEqual(hj.plan([one_off], NOW, CFG), ([], []))

    def test_old_cos_escalation_expires(self):
        old = issue("[Chief of Staff] Triage quality concern: #185", 40, labels=["escalation"])
        fresh = issue("[Chief of Staff] Something new", 3, labels=["escalation"])
        self.assertEqual(closed_numbers(hj.plan([old, fresh], NOW, CFG)[0]), {old["number"]})



class ExpireTests(unittest.TestCase):
    def test_old_run_escalations_expire_recent_kept(self):
        old = issue("ESCALATION: growth run — all actions failed", 8, labels=["escalation"])
        tagged = issue("ESCALATION [TRANSIENT-EXHAUSTED]: review failed (o/r)", 9, labels=["escalation"])
        fresh = issue("ESCALATION: legal run — all actions failed", 2, labels=["escalation"])
        actions, _ = hj.plan([old, tagged, fresh], NOW, CFG)
        self.assertEqual(closed_numbers(actions), {old["number"], tagged["number"]})
        self.assertTrue(all(a["kind"] == "expire" for a in actions))

    def test_legacy_hub_drafts_expire_even_awaiting_approval(self):
        old = issue("Draft: DEV — x", 20, labels=["awaiting-approval", "project:demo"])
        young = issue("Draft: DEV — y", 5, labels=["awaiting-approval"])
        actions, _ = hj.plan([old, young], NOW, CFG)
        self.assertEqual(closed_numbers(actions), {old["number"]})

    def test_approver_engaged_escalation_kept(self):
        it = issue("ESCALATION: brief run — all actions failed", 30, comments=["OWNER"])
        actions, kept = hj.plan([it], NOW, CFG)
        self.assertEqual(actions, [])
        self.assertEqual(len(kept), 1)

    def test_projects_never_expire(self):
        it = issue("ESCALATION: x", 30, labels=["report"])
        actions, _ = hj.plan([it], NOW, hj.project_cfg(CFG))
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
