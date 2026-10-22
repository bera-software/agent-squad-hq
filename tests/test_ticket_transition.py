#!/usr/bin/env python3
"""
Tests for squadhq.ticket_transition — which tickets a merge moves. The
transitions themselves are stuck_sweeper's reconcile_issue / dependency_ready /
plan_dispatch, tested in tests/test_stuck_sweeper.py.

    python3 -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from squadhq import ticket_transition as tt  # noqa: E402


def pr(n, body, refs=()):
    return {"number": n, "body": body, "author": {"login": "app/squadhq-ops"}, "closingIssuesReferences": [{"number": r} for r in refs]}


class AffectedTests(unittest.TestCase):
    def test_implementation_pr_hits_its_ticket_and_dependents(self):
        issues = [{"number": 276, "body": "x\nAfter: #275", "authorAssociation": "MEMBER"}, {"number": 300, "body": "After: #9"}]
        hit, impl = tt.affected([pr(280, "Closes #275", [275])], issues)
        self.assertEqual(hit, {275: 280, 276: 280})
        self.assertEqual(impl, {275})

    def test_design_pr_hits_gated_ticket_only(self):
        issues = [{"number": 276, "body": "After: #275"}]
        hit, impl = tt.affected([pr(277, "Design\nGate-for: #275")], issues)
        self.assertEqual(hit, {275: 277})     # a design merge does not unblock "After:" dependents
        self.assertEqual(impl, set())

    def test_unrelated_merge_hits_nothing(self):
        self.assertEqual(tt.affected([pr(281, "chore: refresh graphify")], []), ({}, set()))


if __name__ == "__main__":
    unittest.main()
