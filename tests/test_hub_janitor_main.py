"""squadhq.hub_janitor.clean / main with a fake gh seam (no network)."""
import datetime
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from squadhq import hub_janitor as hj

NOW = datetime.datetime.now(datetime.timezone.utc)


def issue(n, title, days, labels=(), human=False, comments=(), body=""):
    return {"number": n, "title": title, "body": body, "labels": [{"name": l} for l in labels],
            "author": {"login": "octocat" if human else "app/squadhq-ops", "is_bot": not human},
            "createdAt": (NOW - datetime.timedelta(days=days)).isoformat(),
            "comments": [{"authorAssociation": a} for a in comments], "isPinned": False}


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


HUB = [
    issue(1, "ESCALATION [INFRA]: x failed (o/r)", 1, body="https://github.com/o/r/actions/runs/1"),
    issue(2, "ESCALATION [INFRA]: x failed (o/r)", 2),
    issue(3, "Daily Brief — 2026-09-01", 3),
    issue(4, "Daily Brief — 2026-09-02", 1),
    issue(5, "ESCALATION [AGENT]: old", 30),
    issue(6, "Human question", 3, human=True),
    issue(7, "Daily Brief — 2026-08-30", 5, comments=["OWNER"]),
]


class FakeGH:
    def __init__(self, hub=HUB, project=(), fail=()):
        self.hub, self.project, self.fail, self.calls = list(hub), list(project), fail, []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["repo", "view"]:
            return cp(out="o/fallback\n")
        if args[:2] == ["issue", "list"]:
            repo = args[args.index("-R") + 1]
            if repo in self.fail:
                return cp(1, err="nope")
            return cp(out=json.dumps(self.hub if repo == "o/hub" else self.project))
        if args[:2] in (["issue", "close"], ["issue", "comment"]) and "close-fails" in self.fail:
            return cp(1, err="denied")
        return cp()


def run(fake, argv, env=None):
    out = io.StringIO()
    with mock.patch.object(hj, "gh", side_effect=fake), \
            mock.patch.dict(os.environ, env if env is not None else {"SQUADHQ_HUB_REPO": "o/hub"}, clear=True), \
            redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = hj.main(argv)
    return rc, out.getvalue()


class JanitorMainTests(unittest.TestCase):
    def test_hub_scope_merges_retires_expires_and_protects(self):
        fake = FakeGH()
        rc, out = run(fake, [])
        self.assertEqual(rc, 0)
        self.assertIn("| merge | #2 → #1 |", out)
        self.assertIn("| retire | #3 → #4 |", out)
        self.assertIn("| expire | #5", out)
        self.assertIn("approver commented", out)
        self.assertIn("human-authored", out)
        closes = [c for c in fake.calls if c[:2] == ["issue", "close"]]
        self.assertEqual(sorted(c[2] for c in closes), ["2", "3", "5"])

    def test_dry_run_and_failures(self):
        fake = FakeGH()
        rc, out = run(fake, ["--dry-run"])
        self.assertIn("(dry run)", out)
        self.assertFalse(any(c[:2] == ["issue", "close"] for c in fake.calls))
        rc, out = run(FakeGH(fail=("close-fails",)), [])
        self.assertIn("FAILED", out)

    def test_list_failure_is_exit_1(self):
        rc, _ = run(FakeGH(fail=("o/hub",)), [])
        self.assertEqual(rc, 1)

    def test_nothing_to_clean(self):
        rc, out = run(FakeGH(hub=[]), [])
        self.assertIn("Nothing to clean", out)

    def test_budget_defers(self):
        cfg = dict(hj.flow_reader.get_flood_guard("hub_janitor"))
        actions, _ = hj.plan(HUB, NOW, cfg)
        with mock.patch.object(hj, "gh", side_effect=FakeGH()):
            text, left = hj.clean("o/hub", HUB, cfg, True, 0)
        self.assertIn("deferred (action cap)", text)
        self.assertEqual(left, 0)
        self.assertTrue(actions)

    def test_projects_scope(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "config"))
            shutil.copy(hj.flow_reader.default_path(), os.path.join(d, "config", "flows.yml"))
            with open(os.path.join(d, "config", "projects.json"), "w") as f:
                json.dump({"projects": [{"repo": "o/p"}, {"repo": "o/broken"}]}, f)
            reports = [issue(10, "SEO weekly — a", 20, labels=["report"]),
                       issue(11, "SEO weekly — b", 1, labels=["report"]),
                       issue(12, "SEO weekly — c", 30)]
            fake = FakeGH(project=reports, fail=("o/broken",))
            rc, out = run(fake, ["--scope", "projects"], env={"SQUADHQ_HUB_REPO": "o/hub", "SQUADHQ_HOME": d})
        self.assertIn("| retire | #10 → #11 |", out)
        self.assertIn("Skipped: could not list issues", out)
        close = [c for c in fake.calls if c[:2] == ["issue", "close"]]
        self.assertEqual(close[0][-2:], ["--reason", "completed"])  # no comment in project repos

    def test_hub_repo_fallback_to_gh_and_summary(self):
        with tempfile.NamedTemporaryFile("r", delete=False) as f:
            path = f.name
        fake = FakeGH(hub=[])
        rc, out = run(fake, [], env={"GITHUB_STEP_SUMMARY": path})
        self.assertIn("o/fallback", out)
        self.assertIn("Hub janitor", open(path).read())
        os.unlink(path)

    def test_list_cap_warns(self):
        big = [issue(i, f"t{i}", 1) for i in range(hj.LIST_LIMIT)]
        err = io.StringIO()
        with mock.patch.object(hj, "gh", return_value=cp(out=json.dumps(big))), redirect_stderr(err):
            self.assertEqual(len(hj.list_issues("o/hub")), hj.LIST_LIMIT)
        self.assertIn("cap", err.getvalue())

    def test_gh_delegates_to_client(self):
        c = mock.Mock()
        c.run.return_value = cp(out="x")
        with mock.patch.object(hj.github, "client", return_value=c):
            self.assertEqual(hj.gh(["a"]).stdout, "x")


if __name__ == "__main__":
    unittest.main()
