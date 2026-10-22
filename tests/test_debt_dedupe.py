#!/usr/bin/env python3
"""Tests for squadhq.debt_dedupe — the debt re-proposal guard.

    python3 -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from squadhq import debt_dedupe as d  # noqa: E402

P247 = {"number": 247, "title": "SpendAuthorizationService oversized (627 lines) — "
        "domain/src/main/java/com/example/app/domain/service/SpendAuthorizationService.java", "body": ""}


class DedupeTests(unittest.TestCase):
    def test_real_repeat_is_caught(self):
        f = {"title": "SpendAuthorizationService exceeds recommended size threshold", "body": "", "labels": []}
        self.assertEqual(d.prior_match(f, [P247])["number"], 247)

    def test_same_file_by_path_is_caught(self):
        f = {"title": "Split service", "body": "See domain/service/SpendAuthorizationService.java", "labels": []}
        self.assertEqual(d.prior_match(f, [P247])["number"], 247)

    def test_security_finding_always_passes(self):
        f = {"title": "SpendAuthorizationService CVE", "body": "", "labels": ["severity:high"]}
        self.assertIsNone(d.prior_match(f, [P247]))

    def test_unrelated_finding_passes(self):
        f = {"title": "policy-form.tsx complexity", "body": "frontend/src/policy-form.tsx", "labels": []}
        self.assertIsNone(d.prior_match(f, [P247]))

    def test_spoofed_prior_issue_from_outsider_is_ignored(self):
        f = {"title": "SpendAuthorizationService exceeds recommended size threshold", "body": "", "labels": []}
        fake = dict(P247, author={"login": "mallory"}, authorAssociation="NONE")
        self.assertIsNone(d.prior_match(f, [fake]))
        ours = dict(P247, author={"login": "app/squadhq-ops"})
        self.assertEqual(d.prior_match(f, [ours])["number"], 247)
        member = dict(P247, author={"login": "alice"}, authorAssociation="MEMBER")
        self.assertEqual(d.prior_match(f, [member])["number"], 247)

    def test_no_anchor_no_match(self):
        self.assertIsNone(d.prior_match({"title": "general cleanup", "body": "", "labels": []}, [P247]))


if __name__ == "__main__":
    unittest.main()
