#!/usr/bin/env python3
"""
Tests for squadhq.merge_policy — the shared evaluator behind router.yml's
`self-merge` (auto_merge tier) and `trivial-merge` (trivial_merge tier) jobs.

    python -m pytest tests/test_merge_policy.py

Every test here is about one property: the evaluator FAILS CLOSED. A merge with
no human in the loop is the only thing it can get catastrophically wrong, so
each case that should not merge is asserted individually rather than folded into
one "happy path plus a smoke test".

No network: the gh CLI is stubbed at the module's two shell seams (gh_json,
gh_lines). The real config/flows.yml is loaded, so these also fail if someone
edits the policy in a way the evaluator can no longer satisfy. auto_merge
ships DISABLED (OSS default: approvers merge); tests that exercise the tier
opt in the way a project would, by flipping `enabled`.
"""
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import yaml  # noqa: E402

from squadhq import merge_policy  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOWS = os.path.join(REPO_ROOT, "config", "flows.yml")


_SHIPPED = None


def shipped_root():
    global _SHIPPED
    if _SHIPPED is None:
        with open(FLOWS) as fh:
            _SHIPPED = (yaml.safe_load(fh) or {}).get("merge_policy") or {}
    return copy.deepcopy(_SHIPPED)


def policy_root(opt_in=True):
    root = shipped_root()
    if opt_in:
        root["auto_merge"] = dict(root["auto_merge"], enabled=True)
    return root


OPS = "app/squadhq-ops"
AGENT = "app/squadhq-agent"
_ENV = None


def setUpModule():
    # Two-app mode: agent PRs (auto_merge) are authored by the agent App,
    # trivial lanes by the ops App. Single-app mode is tested separately.
    global _ENV
    _ENV = mock.patch.dict(os.environ, {"SQUADHQ_AGENT_APP_CLIENT_ID": "Iv-agent",
                                        "SQUADHQ_AGENT_BOT_LOGIN": "squadhq-agent[bot]",
                                        "SQUADHQ_BOT_LOGIN": "squadhq-ops[bot]"})
    _ENV.start()
    # Every Evaluator here is built from injected PR data; label provenance
    # (an API read) is covered by tests/test_merge_policy_provenance.py.
    global _REAL_PROVENANCE
    _REAL_PROVENANCE = merge_policy.Evaluator.provenance
    merge_policy.Evaluator.provenance = lambda self: (True, "")


def tearDownModule():
    _ENV.stop()
    merge_policy.Evaluator.provenance = _REAL_PROVENANCE


def pr(labels=("ready-to-merge", "risk:low"), is_bot=True, checks=(("ci", "SUCCESS"),),
       draft=False, login=AGENT, head="graph-refresh-1"):
    return {
        "labels": [{"name": n} for n in labels],
        "author": {"is_bot": is_bot, "login": login},
        "statusCheckRollup": [{"name": n, "conclusion": c} for n, c in checks],
        "isDraft": draft,
        "baseRefName": "main",
        "headRefName": head,
        "title": "t",
    }


def ops_pr(**kw):
    kw.setdefault("login", OPS)
    return pr(**kw)


def evaluator(pr_data, files=(), dc_backend="", dc_frontend=""):
    return merge_policy.Evaluator("o/r", "1", policy_root(), dc_backend, dc_frontend,
                                  pr_data=pr_data, files=files)


class GlobTest(unittest.TestCase):
    def test_double_star_dir(self):
        self.assertTrue(merge_policy.matches_any("graphify-out/graph.json", ["graphify-out/**"]))
        self.assertTrue(merge_policy.matches_any("graphify-out/a/b.json", ["graphify-out/**"]))
        self.assertFalse(merge_policy.matches_any("graphify-outX/g.json", ["graphify-out/**"]))

    def test_double_star_prefix_matches_root_and_nested(self):
        pats = ["**/package-lock.json"]
        self.assertTrue(merge_policy.matches_any("package-lock.json", pats))
        self.assertTrue(merge_policy.matches_any("frontend/package-lock.json", pats))
        self.assertTrue(merge_policy.matches_any("a/b/c/package-lock.json", pats))
        self.assertFalse(merge_policy.matches_any("package-lock.json.bak", pats))

    def test_single_star_does_not_cross_separator(self):
        self.assertTrue(merge_policy.matches_any("a.yml", ["*.yml"]))
        self.assertFalse(merge_policy.matches_any("dir/a.yml", ["*.yml"]))

    def test_exact(self):
        self.assertTrue(merge_policy.matches_any("docs/FLOW-DIAGRAM.md", ["docs/FLOW-DIAGRAM.md"]))
        self.assertFalse(merge_policy.matches_any("docs/FLOW-DIAGRAM.md.orig", ["docs/FLOW-DIAGRAM.md"]))


class AutoMergeTierTest(unittest.TestCase):
    def decide(self, **kw):
        return evaluator(**kw).decide("auto_merge")

    def test_happy_path(self):
        ok, why = self.decide(pr_data=pr(), dc_backend="false", dc_frontend="false")
        self.assertTrue(ok, why)

    def test_label_removed_since_the_event(self):
        ok, why = self.decide(pr_data=pr(labels=("risk:low",)), dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("no longer on the PR", why)

    def test_changes_requested_beats_a_stale_label(self):
        ok, why = self.decide(
            pr_data=pr(labels=("ready-to-merge", "risk:low", "review:changes-requested")),
            dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)

    def test_missing_risk_label(self):
        ok, why = self.decide(pr_data=pr(labels=("ready-to-merge",)), dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("no risk:* label", why)

    def test_two_risk_labels_is_not_risk_low(self):
        ok, _ = self.decide(pr_data=pr(labels=("ready-to-merge", "risk:low", "risk:high")),
                            dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)

    def test_human_authored(self):
        ok, why = self.decide(pr_data=pr(is_bot=False, login="someone"),
                              dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("human-authored", why)

    def test_failed_check(self):
        ok, why = self.decide(pr_data=pr(checks=(("frontend-ci", "FAILURE"),)),
                              dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("frontend-ci", why)

    def test_pending_check_is_not_a_failure(self):
        # gh pr merge --auto holds the PR until required checks pass.
        data = pr()
        data["statusCheckRollup"] = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
        ok, why = self.decide(pr_data=data, dc_backend="false", dc_frontend="false")
        self.assertTrue(ok, why)

    def test_a_superseded_cancelled_run_is_not_a_failure(self):
        # A reopened PR (or a re-run) leaves the older run CANCELLED next to the newer one.
        data = pr()
        data["statusCheckRollup"] = [
            {"name": "ci-gate", "workflowName": "Agents", "conclusion": "CANCELLED", "completedAt": "2026-10-31T12:30:00Z"},
            {"name": "ci-gate", "workflowName": "Agents", "conclusion": "SUCCESS", "completedAt": "2026-10-31T12:40:00Z"}]
        ok, why = self.decide(pr_data=data, dc_backend="false", dc_frontend="false")
        self.assertTrue(ok, why)
        data["statusCheckRollup"] = data["statusCheckRollup"][:1]  # cancelled and nothing newer: still a failure
        ok, why = self.decide(pr_data=data, dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("ci-gate=cancelled", why)

    def test_skipped_check_is_not_a_failure(self):
        ok, why = self.decide(pr_data=pr(checks=(("backend-ci", "SKIPPED"),)),
                              dc_backend="false", dc_frontend="false")
        self.assertTrue(ok, why)

    def test_production_code_merges_when_risk_low(self):
        # 2026-10-14: change scope no longer gates auto_merge — risk:low +
        # bot + no failing check is enough, backend/frontend included.
        for be, fe in (("true", "false"), ("false", "true"), ("true", "true")):
            ok, why = self.decide(pr_data=pr(), dc_backend=be, dc_frontend=fe)
            self.assertTrue(ok, f"backend={be} frontend={fe}: {why}")

    def test_production_code_above_low_risk_waits(self):
        for risk in ("risk:medium", "risk:high"):
            ok, why = self.decide(pr_data=pr(labels=("ready-to-merge", risk)),
                                  dc_backend="true", dc_frontend="false")
            self.assertFalse(ok)
            self.assertIn(risk, why)

    def test_shipped_policy_has_no_change_scope_gate(self):
        conds = [k for e in policy_root()["auto_merge"]["requires_all"] for k in e]
        self.assertNotIn("change_scope_excludes", conds)
        self.assertIn("risk_tag", conds)

    def test_draft(self):
        ok, why = self.decide(pr_data=pr(draft=True), dc_backend="false", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("draft", why)

    def test_unreadable_pr_fails_closed(self):
        ev = merge_policy.Evaluator("o/r", "1", policy_root(), "false", "false",
                                    pr_data=None, files=[])
        ok, why = ev.decide("auto_merge")
        self.assertFalse(ok)

    def test_unknown_condition_fails_closed(self):
        root = policy_root()
        root["auto_merge"] = dict(root["auto_merge"])
        root["auto_merge"]["requires_all"] = list(root["auto_merge"]["requires_all"]) + [{"phase_of_moon": "full"}]
        ev = merge_policy.Evaluator("o/r", "1", root, "false", "false",
                                    pr_data=pr(), files=[])
        ok, why = ev.decide("auto_merge")
        self.assertFalse(ok)
        self.assertIn("unsupported policy condition", why)

    def test_disabled_tier_fails_closed(self):
        root = policy_root()
        root["auto_merge"] = dict(root["auto_merge"], enabled=False)
        ev = merge_policy.Evaluator("o/r", "1", root, "false", "false",
                                    pr_data=pr(), files=[])
        ok, _ = ev.decide("auto_merge")
        self.assertFalse(ok)

    def test_missing_tier_fails_closed(self):
        ev = merge_policy.Evaluator("o/r", "1", {}, "false", "false",
                                    pr_data=pr(), files=[])
        ok, why = ev.decide("auto_merge")
        self.assertFalse(ok)

class TrivialMergeTierTest(unittest.TestCase):
    def decide(self, **kw):
        return evaluator(**kw).decide("trivial_merge")

    def test_generated_graph_only(self):
        ok, why = self.decide(pr_data=ops_pr(labels=()), files=(
            "graphify-out/graph.json", "graphify-out/GRAPH_REPORT.md", "graphify-out/manifest.json"))
        self.assertTrue(ok, why)

    def test_no_risk_label_needed(self):
        # The trivial tier deliberately does not ask for a risk tag — no
        # reviewer runs, so nobody would apply one.
        ok, why = self.decide(pr_data=ops_pr(labels=(), head="flow-diagram-7"), files=("docs/FLOW-DIAGRAM.md",))
        self.assertTrue(ok, why)

    def test_one_stray_file_disqualifies(self):
        ok, why = self.decide(pr_data=ops_pr(labels=()), files=(
            "graphify-out/graph.json", "backend/src/main/java/A.java"))
        self.assertFalse(ok)
        self.assertIn("A.java", why)

    def test_human_authored_never_trivial(self):
        ok, why = self.decide(pr_data=ops_pr(labels=(), is_bot=False, login="someone"),
                              files=("graphify-out/graph.json",))
        self.assertFalse(ok)
        self.assertIn("not this hub's bot", why)

    def test_failed_check_disqualifies(self):
        ok, _ = self.decide(pr_data=ops_pr(labels=(), checks=(("backend-ci", "FAILURE"),)),
                            files=("graphify-out/graph.json",))
        self.assertFalse(ok)

    def test_empty_file_list_fails_closed(self):
        ok, why = self.decide(pr_data=ops_pr(labels=()), files=())
        self.assertFalse(ok)
        self.assertIn("no changed files", why)

    def test_missing_trivial_paths_fails_closed(self):
        root = policy_root()
        root.pop("trivial_paths", None)
        ev = merge_policy.Evaluator("o/r", "1", root, "", "", pr_data=ops_pr(labels=()),
                                    files=["graphify-out/graph.json"])
        ok, why = ev.decide("trivial_merge")
        self.assertFalse(ok)
        self.assertIn("trivial_paths", why)

    def test_lockfiles_and_manifests_are_never_trivial(self):
        # a lockfile-only diff can repoint a dependency; it takes
        # the reviewed path (SCA + high risk floor), never the trivial one.
        for f in ("frontend/package-lock.json", "gradle.lockfile", "pom.xml.lock", "package.json",
                  "uv.lock", "go.sum"):
            ok, why = self.decide(pr_data=ops_pr(labels=()), files=(f,))
            self.assertFalse(ok, f)
            self.assertIn("lane", why)


class GraphRefreshPRTest(unittest.TestCase):
    """The concrete PR router.yml's graph-refresh job opens, end to end
    through both tiers."""

    FILES = ("graphify-out/graph.json", "graphify-out/GRAPH_REPORT.md",
             "graphify-out/manifest.json")

    def test_clears_trivial_tier(self):
        ok, why = evaluator(ops_pr(labels=()), files=self.FILES).decide("trivial_merge")
        self.assertTrue(ok, why)

    def test_would_also_clear_auto_merge_tier_if_reviewed(self):
        ok, why = evaluator(pr(labels=("ready-to-merge", "risk:low")),
                            files=self.FILES).decide("auto_merge")
        self.assertTrue(ok, why)



class ChangeScopeEvaluatorTest(unittest.TestCase):
    """The change_scope_excludes evaluator is not in the shipped
    auto_merge policy but stays implemented, so a policy that
    re-adds it must still fail closed on production code."""

    def decide(self, pr_data, files=None, dc_backend="", dc_frontend=""):
        root = dict(policy_root())
        tier = dict(root["auto_merge"])
        tier["requires_all"] = list(tier["requires_all"]) + [
            {"change_scope_excludes": ["backend", "frontend"]}]
        root["auto_merge"] = tier
        return merge_policy.Evaluator("o/r", "1", root, dc_backend, dc_frontend,
                                      pr_data=pr_data, files=files).decide("auto_merge")

    def test_touches_backend(self):
        ok, why = self.decide(pr(), files=("backend/src/A.java",), dc_backend="true", dc_frontend="false")
        self.assertFalse(ok)
        self.assertIn("backend", why)

    def test_indeterminate_diff_fails_closed(self):
        ok, why = self.decide(pr(), files=("backend/src/A.java",), dc_backend="true", dc_frontend="true")
        self.assertFalse(ok)

    def test_fallback_file_classification_when_detect_changes_is_absent(self):
        cases = {
            ("docs/x.md",): True,
            ("CLAUDE.md", "docs/adr/1.md"): True,
            ("graphify-out/graph.json",): True,
            ("backend/src/main/java/A.java",): False,
            ("frontend/src/App.tsx",): False,
            ("pom.xml",): False,
            ("config.yml",): False,
        }
        for files, expected in cases.items():
            ok, why = self.decide(pr(), files=files)
            self.assertEqual(ok, expected, f"{files}: {why}")

    def test_unlistable_files_fail_closed(self):
        with mock.patch.object(merge_policy, "gh_lines", return_value=None):
            ok, why = self.decide(pr())
        self.assertFalse(ok)
        self.assertIn("could not list", why)


class ShippedDefaultsTest(unittest.TestCase):
    def test_auto_merge_ships_disabled(self):
        ok, why = merge_policy.Evaluator("o/r", "1", shipped_root(), "false", "false",
                                         pr_data=pr(), files=["docs/x.md"]).decide("auto_merge")
        self.assertFalse(ok)
        self.assertIn("enabled is not true", why)

    def test_shipped_auto_merge_requires_the_risk_floor(self):
        conds = [k for c in shipped_root()["auto_merge"]["requires_all"] for k in c]
        self.assertIn("final_risk", conds)
        self.assertIn("risk_tag", conds)


class FinalRiskTest(unittest.TestCase):
    def decide(self, labels=("ready-to-merge", "risk:low"), files=("docs/x.md",), root=None):
        return merge_policy.Evaluator("o/r", "1", root or policy_root(), "false", "false",
                                      pr_data=pr(labels=labels), files=files).decide("auto_merge")

    def test_docs_only_low_merges(self):
        ok, why = self.decide()
        self.assertTrue(ok, why)

    def test_llm_low_cannot_lower_auth_path(self):
        ok, why = self.decide(files=("docs/x.md", "src/auth/Login.java"))
        self.assertFalse(ok)
        self.assertIn("final risk is high", why)
        self.assertIn("src/auth/Login.java", why)

    def test_more_matches_are_counted(self):
        ok, why = self.decide(files=(".github/workflows/ci.yml", "package.json"))
        self.assertFalse(ok)
        self.assertIn("+1 more", why)

    def test_unlistable_files_are_high(self):
        with mock.patch.object(merge_policy, "gh_lines", return_value=None):
            ok, why = merge_policy.Evaluator("o/r", "1", policy_root(), "false", "false",
                                             pr_data=pr()).decide("auto_merge")
        self.assertFalse(ok)
        self.assertIn("could not list", why)

    def test_policy_risk_paths_and_env_extra_globs(self):
        root = policy_root()
        root["risk_paths"] = ["docs/secret-plans/**"]
        ok, _ = self.decide(files=("docs/secret-plans/a.md",), root=root)
        self.assertFalse(ok)
        with mock.patch.dict(os.environ, {"RISK_EXTRA_GLOBS": "docs/**,\nother/**"}):
            ok, why = self.decide(files=("docs/x.md",))
        self.assertFalse(ok)
        self.assertIn("project risk path", why)

    def test_invalid_expected_level_fails_closed(self):
        root = policy_root()
        root["auto_merge"]["requires_all"] = [{"final_risk": "tiny"}]
        ok, why = self.decide(root=root)
        self.assertFalse(ok)
        self.assertIn("not a risk level", why)

    def test_final_risk_medium_allows_medium(self):
        root = policy_root()
        root["auto_merge"]["requires_all"] = [{"final_risk": "medium"}]
        ok, why = self.decide(labels=("ready-to-merge", "risk:medium"), root=root)
        self.assertTrue(ok, why)
        ok, why = self.decide(labels=("ready-to-merge", "risk:low", "risk:high"), root=root)
        self.assertFalse(ok)
        self.assertIn("missing/ambiguous", why)


class MainTest(unittest.TestCase):
    def test_main_emits_outputs_and_fails_closed(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"REPO": "o/r", "PR": "1", "FLOWS": FLOWS}), \
                mock.patch.object(merge_policy, "gh_json", return_value=None), redirect_stdout(buf):
            self.assertEqual(merge_policy.main(["x", "auto_merge"]), 0)
        out = buf.getvalue().splitlines()
        self.assertEqual(out[0], "auto_merge=false")
        self.assertTrue(out[1].startswith("reason="))

    def test_main_unreadable_flows(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"REPO": "o/r", "PR": "1", "FLOWS": "/nonexistent/flows.yml"}), \
                mock.patch.object(merge_policy, "gh_json", return_value={}), redirect_stdout(buf), \
                redirect_stderr(io.StringIO()):
            merge_policy.main(["x"])
        self.assertIn("no merge_policy.auto_merge section", buf.getvalue())


class DcCodeTest(unittest.TestCase):
    def decide(self, files=("src/A.java",), **dc):
        root = policy_root()
        root["auto_merge"] = dict(root["auto_merge"], requires_all=[{"change_scope_excludes": ["code"]}])
        return merge_policy.Evaluator("o/r", "1", root, dc.get("be", ""), dc.get("fe", ""), pr_data=pr(),
                                      files=files, dc_code=dc.get("code", "")).decide("auto_merge")

    def test_dc_code_false_and_true(self):
        self.assertTrue(self.decide(code="false")[0])
        ok, why = self.decide(code="true")
        self.assertFalse(ok)
        self.assertIn("code", why)

    def test_unknown_value_fails_closed(self):
        self.assertFalse(self.decide(code="maybe")[0])

    def test_backend_frontend_both_code_still_supported(self):
        self.assertTrue(self.decide(be="false", fe="false")[0])
        self.assertFalse(self.decide(be="true", fe="false")[0])

    def test_dc_code_fills_missing_backend(self):
        root = policy_root()
        root["auto_merge"] = dict(root["auto_merge"], requires_all=[{"change_scope_excludes": ["backend"]}])
        ev = merge_policy.Evaluator("o/r", "1", root, "", "false", pr_data=pr(), files=(), dc_code="true")
        self.assertFalse(ev.decide("auto_merge")[0])

    def test_fallback_file_scope_has_code(self):
        self.assertFalse(self.decide(files=("pom.xml",))[0])
        self.assertTrue(self.decide(files=("docs/a.md",))[0])

    def test_main_reads_dc_code(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"REPO": "o/r", "PR": "1", "FLOWS": FLOWS, "DC_CODE": "true"}), \
                mock.patch.object(merge_policy, "Evaluator") as E, redirect_stdout(buf):
            E.return_value.decide.return_value = (False, "x")
            merge_policy.main(["x", "auto_merge"])
        self.assertEqual(E.call_args.kwargs["dc_code"], "true")


class TrivialLaneTest(unittest.TestCase):
    """W6: trivial merge only for OUR bot, on its own regeneration branch,
    touching only that lane's exact files."""

    def decide(self, files, **kw):
        kw.setdefault("login", OPS)
        return evaluator(pr(labels=(), **kw), files=files).decide("trivial_merge")

    def test_graph_lane(self):
        self.assertTrue(self.decide(("graphify-out/graph.json",))[0])
        ok, why = self.decide(("graphify-out/other.json",))
        self.assertFalse(ok)
        self.assertIn("graph-refresh-*", why)

    def test_wrong_branch_for_files(self):
        ok, why = self.decide(("docs/FLOW-DIAGRAM.md",), head="graph-refresh-2")
        self.assertFalse(ok)
        ok, why = self.decide(("graphify-out/graph.json",), head="flow-diagram-2")
        self.assertFalse(ok)
        ok, why = self.decide(("graphify-out/graph.json",), head="feature/x")
        self.assertFalse(ok)
        self.assertIn("matches no", why)
        ok, why = self.decide(("graphify-out/graph.json",), head="")
        self.assertIn("head branch", why)

    def test_other_bot_is_not_ours(self):
        ok, why = self.decide(("graphify-out/graph.json",), login="app/dependabot")
        self.assertFalse(ok)
        self.assertIn("not this hub's bot", why)
        ok, why = self.decide(("graphify-out/graph.json",), login="")
        self.assertIn("could not determine", why)

    def test_lane_edge_cases(self):
        root = policy_root()
        root["trivial_merge"]["requires_all"] = [{"trivial_lane": "nope"}]
        ev = merge_policy.Evaluator("o/r", "1", root, "", "", pr_data=ops_pr(labels=()), files=["x"])
        self.assertIn("missing or empty", ev.decide("trivial_merge")[1])
        root["trivial_merge"]["requires_all"] = [{"trivial_lane": "trivial_lanes"}]
        ev = merge_policy.Evaluator("o/r", "1", root, "", "", pr_data=ops_pr(labels=()), files=[])
        self.assertIn("no changed files", ev.decide("trivial_merge")[1])
        with mock.patch.object(merge_policy, "gh_lines", return_value=None):
            ev = merge_policy.Evaluator("o/r", "1", root, "", "", pr_data=ops_pr(labels=()))
            self.assertIn("could not list", ev.decide("trivial_merge")[1])
        ev = merge_policy.Evaluator("o/r", "1", root, "", "", pr_data=ops_pr(labels=()),
                                    files=["a", "b", "c", "d"])
        self.assertIn("(+1 more)", ev.decide("trivial_merge")[1])
        root["trivial_merge"]["requires_all"] = [{"pr_author_login": "human"}]
        ev = merge_policy.Evaluator("o/r", "1", root, "", "", pr_data=ops_pr(labels=()), files=["a"])
        self.assertIn("only 'bot'", ev.decide("trivial_merge")[1])


class SingleAppModeTest(unittest.TestCase):
    """No agent App: agents share the ops identity, so nothing merges unattended."""

    def test_unattended_tiers_need_a_human(self):
        with mock.patch.dict(os.environ, {"SQUADHQ_AGENT_APP_CLIENT_ID": ""}):
            for tier, data in (("auto_merge", pr(login=OPS)), ("trivial_merge", ops_pr(labels=()))):
                ok, why = evaluator(data, files=("graphify-out/graph.json",)).decide(tier)
                self.assertFalse(ok)
                self.assertIn("single-app mode", why)

    def test_main_reports_single_app(self):
        import io
        from contextlib import redirect_stdout
        for client, want in (("", "single_app=true"), ("Iv-agent", "single_app=false")):
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"REPO": "o/r", "PR": "1", "FLOWS": FLOWS,
                                              "SQUADHQ_AGENT_APP_CLIENT_ID": client}), \
                    mock.patch.object(merge_policy, "gh_json", return_value=None), redirect_stdout(buf):
                merge_policy.main(["x", "auto_merge"])
            self.assertIn(want, buf.getvalue().splitlines())

    def test_other_bot_never_counts_as_agent(self):
        ok, why = evaluator(pr(login="app/renovate"), dc_backend="false", dc_frontend="false").decide("auto_merge")
        self.assertFalse(ok)
        self.assertIn("not this hub's agent", why)

