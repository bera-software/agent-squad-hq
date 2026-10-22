#!/usr/bin/env python3
"""
Tests for squadhq.label_guard — label ownership (config/flows.yml
labels.*.applied_by) and the gh-argv parser the agent-job wrapper relies on.

    python -m pytest tests/test_label_guard.py

Loads the real registry, so an ownership edit that breaks a flow fails here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from squadhq import label_guard as lg  # noqa: E402

OWNERS = lg.load_owners()


class OwnershipTests(unittest.TestCase):
    def allowed(self, role, label):
        return lg.may_add(role, label, OWNERS)

    def test_po_outcomes_are_po_only(self):
        for l in ("proposal:pending", "verdict:backlog", "verdict:decline", "verdict:proceed", "verdict:overridden"):
            self.assertTrue(self.allowed("product-owner", l), l)
            for r in ("chief-of-staff", "engineer", "reviewer", "growth"):
                self.assertFalse(self.allowed(r, l), (r, l))

    def test_flow_critical_labels_have_their_owner(self):
        # One per real transition in config/flows.yml — if any of these flips,
        # a flow silently stops.
        for role, label in [("stuck-sweeper", "agent:implement"), ("product-owner", "agent:implement"),
                            ("chief-of-staff", "needs-design"), ("chief-of-staff", "triaged:ready"),
                            ("product-owner", "triaged:ready"), ("engineer", "status:in-progress"),
                            ("engineer", "status:in-review"), ("architect", "status:in-review"),
                            ("stuck-sweeper", "ready-to-merge"), ("stuck-sweeper", "review:changes-requested"),
                            ("debt-scanner", "debt-proposal"), ("debt-scanner", "severity:high"),
                            ("legal", "severity:high"), ("chief-of-staff", "debt"), ("stuck-sweeper", "debt"), ("growth", "awaiting-approval"),
                            ("product-owner", "awaiting-approval"), ("growth", "channel:blog"),
                            ("growth", "channel:external"), ("seo", "channel:blog"),
                            ("daily-brief", "daily-brief"), ("weekly-rollup", "weekly-rollup"),
                            ("seo", "project:example"), ("engineer", "needs-human"), ("finance", "escalation")]:
            self.assertTrue(self.allowed(role, label), (role, label))

    def test_engineer_cannot_self_approve_or_dispatch(self):
        for l in ("ready-to-merge", "risk:low", "agent:implement", "needs-approver-merge", "P0"):
            self.assertFalse(self.allowed("engineer", l), l)

    def test_only_growth_marks_external_content(self):
        for role in ("seo", "product-owner", "engineer", "chief-of-staff"):
            self.assertFalse(self.allowed(role, "channel:external"), role)

    def test_cos_cannot_dispatch(self):
        self.assertFalse(self.allowed("chief-of-staff", "agent:implement"))

    def test_architect_cannot_clear_its_own_gate(self):
        self.assertFalse(self.allowed("architect", "agent:implement"))

    def test_unknown_labels_pass(self):
        self.assertTrue(self.allowed("engineer", "some-free-form-label"))


class ParseTests(unittest.TestCase):
    def labels(self, argv, stdin=None):
        return lg.added_labels(argv, stdin)

    def test_edit_add_label_forms(self):
        self.assertEqual(self.labels(["issue", "edit", "5", "--add-label", "a,b", "--add-label=c"])[0], ["a", "b", "c"])
        self.assertEqual(self.labels(["pr", "edit", "5", "--remove-label", "x"])[0], [])

    def test_create_label_forms(self):
        self.assertEqual(self.labels(["issue", "create", "-t", "t", "-l", "P1", "--label", "dept:growth"])[0],
                         ["P1", "dept:growth"])

    def test_api_labels_endpoint(self):
        got = self.labels(["api", "-X", "POST", "repos/o/r/issues/5/labels", "-f", "labels[]=ready-to-merge"])[0]
        self.assertEqual(got, ["ready-to-merge"])

    def test_api_input_stdin(self):
        got = self.labels(["api", "repos/o/r/issues/5/labels", "--input", "-"], '{"labels": ["P0"]}')[0]
        self.assertEqual(got, ["P0"])

    def test_api_flag_value_not_taken_as_path(self):
        got = self.labels(["api", "-H", "Accept: a/b", "repos/o/r/issues/5/labels", "-f", "labels[]=x"])[0]
        self.assertEqual(got, ["x"])

    def test_api_read_and_delete_pass(self):
        self.assertEqual(self.labels(["api", "repos/o/r/issues/5/labels"])[0], [])
        self.assertEqual(self.labels(["api", "-X", "DELETE", "repos/o/r/issues/5/labels/P0"])[0], [])

    def test_graphql_add_labels_refused(self):
        _, why = self.labels(["api", "graphql", "-f", "query=mutation{addLabelsToLabelable(input:{})}"])
        self.assertIsNotNone(why)

    def test_unrelated_commands_pass(self):
        self.assertEqual(self.labels(["pr", "view", "5"]), ([], None))
        self.assertEqual(self.labels(["issue", "comment", "5", "-b", "--add-label x"]), ([], None))


class ShimTests(unittest.TestCase):
    """Install the wrapper against a fake gh and run it end to end."""

    def test_shim_blocks_and_passes(self):
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        fake = os.path.join(d, "real-gh")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho REAL \"$@\"\n")
        os.chmod(fake, 0o755)
        src = os.path.dirname(os.path.dirname(lg.__file__))
        gp = os.path.join(d, "github_path")
        env = dict(os.environ, RUNNER_TEMP=d, PATH=d + os.pathsep + os.environ["PATH"], PYTHONPATH=src,
                   GITHUB_PATH=gp)
        env.pop("GITHUB_STEP_SUMMARY", None)
        os.rename(fake, os.path.join(d, "gh"))
        subprocess.run([sys.executable, "-m", "squadhq.label_guard", "install", "--role", "engineer"], env=env, check=True,
                       capture_output=True)
        guard_dir = open(gp).read().strip()
        self.assertTrue(os.path.basename(guard_dir).startswith("label-guard-"))
        self.assertEqual(os.stat(guard_dir).st_mode & 0o777, 0o700)
        shim = os.path.join(guard_dir, "gh")
        ok = subprocess.run([shim, "issue", "edit", "5", "--add-label", "status:in-progress"],
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0)
        self.assertIn("REAL issue edit 5", ok.stdout)
        bad = subprocess.run([shim, "pr", "edit", "5", "--add-label", "ready-to-merge"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("may not add ['ready-to-merge']", bad.stderr)

        stdin_ok = subprocess.run([shim, "api", "repos/o/r/issues/5/labels", "--input", "-"],
                                  input='{"labels": ["status:in-progress"]}', capture_output=True, text=True)
        self.assertEqual(stdin_ok.returncode, 0)
        self.assertIn("REAL api", stdin_ok.stdout)


class MoreParseTests(unittest.TestCase):
    def test_attached_and_equals_forms(self):
        self.assertEqual(lg.added_labels(["issue", "create", "-lP1", "--label=P2", "-l=P0"])[0], ["P1", "P2", "P0"])
        self.assertEqual(lg.added_labels(["pr", "edit", "5", "--add-label=a,b"])[0], ["a", "b"])

    def test_api_attached_method(self):
        self.assertEqual(lg.added_labels(["api", "-XPOST", "repos/o/r/issues/5/labels", "-f", "labels[]=x"])[0], ["x"])
        self.assertEqual(lg.added_labels(["api", "--method=PATCH", "repos/o/r/issues/5", "-flabels[]=y"])[0], ["y"])
        self.assertEqual(lg.added_labels(["api", "-XDELETE", "repos/o/r/issues/5/labels/x"])[0], [])

    def test_api_non_label_path_and_input_file(self):
        import tempfile
        self.assertEqual(lg.added_labels(["api", "-X", "POST", "repos/o/r/pulls/5/reviews", "-f", "body=x"])[0], [])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('[{"name": "P0"}, "P1"]')
        self.assertEqual(lg.added_labels(["api", "repos/o/r/issues/5/labels", "--input", f.name])[0], ["P0", "P1"])
        self.assertEqual(lg.added_labels(["api", "repos/o/r/issues/5/labels", "--input", "-"], "not json")[0], [])
        self.assertEqual(lg.added_labels(["api", "graphql", "-f", "query={viewer{login}}"]), ([], None))

    def test_blocked_and_project_wildcard(self):
        owners = {"P0": {"product-owner"}, "project:*": {"growth"}, "x": {"any"}}
        self.assertEqual(lg.blocked_labels("engineer", ["P0", "x", "free"], owners), ["P0"])
        self.assertTrue(lg.may_add("growth", "project:foo", owners))
        self.assertFalse(lg.may_add("engineer", "project:foo", owners))


class InstallTests(unittest.TestCase):
    def test_no_gh_on_path(self):
        from unittest import mock
        with mock.patch.object(lg.shutil, "which", return_value=None):
            self.assertEqual(lg.install("engineer"), 1)

    def test_install_writes_private_shim_and_summary(self):
        import tempfile
        from unittest import mock
        d = tempfile.mkdtemp()
        summary = os.path.join(d, "summary")
        with mock.patch.object(lg.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.dict(os.environ, {"RUNNER_TEMP": d, "GITHUB_STEP_SUMMARY": summary,
                                             "GITHUB_PATH": os.path.join(d, "p")}):
            self.assertEqual(lg.install("reviewer"), 0)
            self.assertEqual(lg.main_args(["install", "--role", "reviewer"]), 0)
        self.assertIn("installed for role `reviewer`", open(summary).read())
        dirs = open(os.path.join(d, "p")).read().split()
        self.assertEqual(len(set(dirs)), 2)  # a fresh directory per install
        shim = os.path.join(dirs[0], "gh")
        self.assertEqual(os.stat(shim).st_mode & 0o777, 0o700)
        self.assertIn("/usr/bin/gh", open(shim).read())



class ForbiddenWriteTests(unittest.TestCase):
    """Agents never push or merge through `gh api` — the deterministic push
    step and the merge-policy job do."""

    def refused(self, argv, stdin=None):
        labels, why = lg.added_labels(argv, stdin)
        return why

    def test_contents_refs_and_merges_refused(self):
        for argv in (
            ["api", "-X", "PUT", "repos/o/r/contents/src/a.py", "-f", "message=x"],
            ["api", "repos/o/r/contents/a.md", "-f", "content=eA=="],          # implicit POST
            ["api", "--method=DELETE", "repos/o/r/contents/a.md"],
            ["api", "-XPATCH", "repos/o/r/git/refs/heads/main", "-F", "force=true"],
            ["api", "-X", "POST", "repos/o/r/git/refs", "--input", "-"],
            ["api", "-X", "DELETE", "/repos/o/r/git/ref/heads/x"],
            ["api", "-X", "POST", "repos/o/r/merges", "-f", "base=main"],
            ["api", "-X", "PUT", "repos/o/r/pulls/7/merge"],
        ):
            self.assertIsNotNone(self.refused(argv, "{}"), argv)

    def test_reads_and_unrelated_writes_pass(self):
        for argv in (
            ["api", "repos/o/r/contents/a.md"],
            ["api", "-X", "GET", "repos/o/r/git/refs/heads/main"],
            ["api", "repos/o/r/pulls/7/merge"],
            ["api", "-X", "POST", "repos/o/r/issues/7/comments", "-f", "body=hi"],
        ):
            self.assertIsNone(self.refused(argv), argv)

    def test_graphql_mutations_refused(self):
        for m in ("mergePullRequest", "enablePullRequestAutoMerge", "createCommitOnBranch",
                  "updateRef", "createRef", "deleteRef"):
            self.assertIsNotNone(self.refused(["api", "graphql", "-f", f"query=mutation {{ {m}(input: {{}}) {{ x }} }}"]), m)
        self.assertIsNotNone(self.refused(["api", "graphql", "--input", "-"], '{"query": "mutation { mergePullRequest }"}'))
        self.assertIsNone(self.refused(["api", "graphql", "-f", "query={ viewer { login } }"]))

    def test_graphql_input_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"query": "mutation { createRef(input: {}) { ref { id } } }"}')
        self.assertIsNotNone(self.refused(["api", "graphql", "--input", f.name]))
        self.assertIsNone(self.refused(["api", "graphql", "--input", "/nonexistent/q.json"]))
        os.unlink(f.name)

    def test_non_api_is_not_a_write(self):
        self.assertIsNone(lg.forbidden_write(["pr", "merge", "1"]))
        self.assertIsNone(lg.forbidden_write([]))


class OutgoingSecretTests(unittest.TestCase):
    TOK = "ghp_" + "A" * 36

    def test_secret_in_args_or_stdin_refused(self):
        why = lg.outgoing_refusal(["issue", "comment", "1", "-b", f"here {self.TOK}"], env={})
        self.assertIn("github-token", why)
        self.assertNotIn(self.TOK, why)
        why = lg.outgoing_refusal(["api", "repos/o/r/issues", "--input", "-"], stdin_text="sk-ant-" + "x" * 30,
                                  env={})
        self.assertIn("anthropic-key", why)
        self.assertIsNone(lg.outgoing_refusal(["issue", "comment", "1", "-b", "all good"], env={}))

    def test_env_value_refused(self):
        env = {"ANTHROPIC_API_KEY": "the-llm-key-value-123", "SSH_PRIVATE": "privatekeymaterial"}
        why = lg.outgoing_refusal(["pr", "create", "-b", "oops the-llm-key-value-123"], env=env)
        self.assertIn("env:ANTHROPIC_API_KEY", why)
        why = lg.outgoing_refusal(["pr", "comment", "-b", "privatekeymaterial"], env=env)
        self.assertIn("env:SSH_PRIVATE", why)

    def test_body_files_scanned_and_confined_to_workspace(self):
        import tempfile
        ws = tempfile.mkdtemp()
        good, bad = os.path.join(ws, "ok.md"), os.path.join(ws, "leak.md")
        open(good, "w").write("fine")
        open(bad, "w").write(f"token {self.TOK}")
        env = {"GITHUB_WORKSPACE": ws}
        self.assertIsNone(lg.outgoing_refusal(["issue", "create", "--body-file", good], env=env))
        self.assertIn("github-token", lg.outgoing_refusal(["issue", "comment", "1", "-F", bad], env=env))
        self.assertIn("github-token", lg.outgoing_refusal(["issue", "comment", f"--body-file={bad}"], env=env))
        self.assertIn("github-token", lg.outgoing_refusal(["api", "x", "-F", f"body=@{bad}"], env=env))
        self.assertIn("github-token", lg.outgoing_refusal(["api", "x", "--input", bad], env=env))
        outside = tempfile.mkdtemp()
        p = os.path.join(outside, "x.md")
        open(p, "w").write("fine")
        self.assertIn("outside the workspace", lg.outgoing_refusal(["issue", "create", "--body-file", p], env=env))
        self.assertIn("outside the workspace",
                      lg.outgoing_refusal(["issue", "create", "--body-file", os.path.join(ws, "..", "x")], env=env))
        # missing file: gh reports it; stdin marker is not a path
        self.assertIsNone(lg.outgoing_refusal(["issue", "create", "--body-file", os.path.join(ws, "nope")], env=env))
        self.assertEqual(lg.referenced_files(["issue", "create", "--body-file", "-"]), [])
        self.assertEqual(lg.referenced_files(["api", "x", "-f", "a=b"]), [])


class VerdictLabelsAreWorkflowOnly(unittest.TestCase):
    """Verdict labels come from review-post (ops identity), never an agent."""

    def setUp(self):
        self.owners = lg.load_owners()

    def test_no_agent_role_applies_verdict_or_fork_labels(self):
        for role in ("reviewer", "engineer", "architect", "product-owner", "concierge", "security-reviewer"):
            for label in ("ready-to-merge", "review:changes-requested", "risk:low", "risk:medium", "fork:approved"):
                self.assertFalse(lg.may_add(role, label, self.owners), (role, label))


class ApprovalRefusal(unittest.TestCase):
    def test_pr_review_approve_refused(self):
        for argv in (["pr", "review", "5", "--approve"], ["pr", "review", "-a", "5"], ["pr", "review", "5", "-ab", "ok"]):
            self.assertIsNotNone(lg.approval_refusal(argv), argv)
        self.assertIsNone(lg.approval_refusal(["pr", "review", "5", "--comment", "-b", "nice"]))
        self.assertIsNone(lg.approval_refusal(["pr", "review", "5", "--request-changes", "-b", "fix"]))

    def test_api_approve_event_refused_any_case(self):
        base = ["api", "repos/o/r/pulls/5/reviews", "--method", "POST"]
        for f in ("event=APPROVE", "event=approve", "event=Approve"):
            self.assertIsNotNone(lg.approval_refusal(base + ["-f", f]), f)
        self.assertIsNotNone(lg.approval_refusal(base + ["--input", "-"], stdin_text='{"event": "APPROVE"}'))
        self.assertIsNone(lg.approval_refusal(base + ["-f", "event=COMMENT", "-f", "body=x"]))

    def test_graphql_approve_refused(self):
        q = 'mutation { addPullRequestReview(input: {pullRequestId: "x", event: APPROVE}) { clientMutationId } }'
        self.assertIsNotNone(lg.approval_refusal(["api", "graphql", "-f", f"query={q}"]))

    def test_added_labels_reports_the_refusal(self):
        labels, why = lg.added_labels(["pr", "review", "5", "--approve"])
        self.assertEqual(labels, [])
        self.assertIn("approve", why)
