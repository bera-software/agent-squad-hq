"""squadhq.flow_reader: validation on mutated copies of the real
config/flows.yml, mirror-drift warnings, caching, lookups and the CLI."""
import copy
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

from squadhq import flow_reader as fr

REAL = fr.load()


def write(tmp, data, name="flows.yml"):
    path = os.path.join(tmp, name)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return path


ROUTER_TMPL = {"jobs": {"detect-changes": {"steps": [
    {"uses": "x"}, {"with": {"filters": None}}]}}}


def router(tmp, patterns):
    data = copy.deepcopy(ROUTER_TMPL)
    data["jobs"]["detect-changes"]["steps"][1]["with"]["filters"] = yaml.safe_dump({"trivial": patterns})
    return write(tmp, data, "router.yml")


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.router = router(self.tmp, list(REAL["merge_policy"]["trivial_paths"]))
        p = mock.patch.object(fr, "ROUTER_PATH", self.router)
        p.start()
        self.addCleanup(p.stop)

    def check(self, mutate):
        data = copy.deepcopy(REAL)
        mutate(data)
        return fr.validate(write(self.tmp, data))

    def test_shipped_file_is_valid(self):
        self.assertEqual(fr.validate(), [])
        self.assertEqual(fr.warnings(), [])

    def test_unparseable_and_missing_keys(self):
        bad = os.path.join(self.tmp, "bad.yml")
        with open(bad, "w") as f:
            f.write("- just\n- a list\n")
        self.assertIn("failed to load", fr.validate(bad)[0])
        probs = self.check(lambda d: d.pop("roles"))
        self.assertEqual(probs, ["missing top-level key: 'roles'"])

    def test_transition_problems(self):
        def mutate(d):
            flow = d["flows"]["ticket-lifecycle"]
            flow["transitions"].append({"from": "nowhere", "to": None, "labels_add": ["no-such-label"],
                                        "actor": "someone"})
            flow["transitions"].append({"to": "also-nowhere"})
            flow["states"].append("orphan_state")
        probs = "\n".join(self.check(mutate))
        for needle in ("from-state 'nowhere' not declared", "missing 'to'", "missing 'from'",
                       "to-state 'also-nowhere' not declared", "unknown label 'no-such-label'",
                       "'orphan_state' is declared but never used"):
            self.assertIn(needle, probs)

    def test_role_idempotency_reference(self):
        probs = self.check(lambda d: d["roles"]["reviewer"].__setitem__("idempotency", ["nope"]))
        self.assertIn("references unknown rule 'nope'", "\n".join(probs))

    def test_label_ownership_problems(self):
        def mutate(d):
            d["labels"]["dept"]["dept:backend"].pop("applied_by")
            d["labels"]["dept"]["dept:frontend"]["applied_by"] = ["ghost"]
            d["label_role_groups"]["reporter"].append("phantom")
        probs = "\n".join(self.check(mutate))
        self.assertIn("missing applied_by", probs)
        self.assertIn("unknown role 'ghost'", probs)
        self.assertIn("label_role_groups.reporter: unknown role 'phantom'", probs)

    def test_label_states_problems(self):
        def mutate(d):
            ls = d["label_states"]["issue"]
            ls["state_labels"].append("not-a-label")
            ls["exclusive"] = [["P0"], ["needs-human", "P1"]]
            ls["modifiers"].append({"label": "dept:backend"})
            ls["combos"].append({"labels": ["needs-design", "ghost"], "state": "nope.state"})
            ls["combos"].append(copy.deepcopy(ls["combos"][0]))
            ls["repairs"].append({"groups": [["ghost-2"]]})
            ls["repairs"].append({"groups": [["needs-human"]], "reason": "x"})
            ls["repairs"].append({"groups": [["needs-human"]], "reason": "x", "add": ["needs-design", "agent:implement"]})
        probs = "\n".join(self.check(mutate))
        for needle in ("state_labels: unknown label 'not-a-label'", "needs at least two labels",
                       "are state labels", "is not in state_labels", "same label set as combos",
                       "is not a declared flows", "unknown label 'ghost-2'", "needs groups and reason",
                       "neither escalates nor changes", "which is not a legal combo"):
            self.assertIn(needle, probs)

    def test_missing_label_states(self):
        probs = self.check(lambda d: d.pop("label_states"))
        self.assertIn("missing label_states.issue", "\n".join(probs))

    def test_static_label_mismatch_detected(self):
        data = copy.deepcopy(REAL)
        path = write(self.tmp, data)
        with mock.patch.object(fr, "static_label_names", return_value=[("extra", "fff")]):
            probs = fr.validate(path)
        self.assertIn("does not match the registry", "\n".join(probs))


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def warn(self, patterns, declared=None, router_present=True):
        data = copy.deepcopy(REAL)
        if declared is not None:
            data["merge_policy"]["trivial_paths"] = declared
        path = write(self.tmp, data)
        rp = router(self.tmp, patterns) if router_present else os.path.join(self.tmp, "absent.yml")
        with mock.patch.object(fr, "ROUTER_PATH", rp):
            return path, fr.warnings(path), fr.validate(path), fr.validate(path, strict=True)

    def test_router_wider_is_a_warning_and_strict_error(self):
        shipped = REAL["merge_policy"]["trivial_paths"]
        _, w, v, s = self.warn(list(shipped) + ["**/package-lock.json"])
        self.assertIn("in router.yml but not flows.yml", w[0])
        self.assertEqual(v, [])
        self.assertEqual(s, w)

    def test_flows_wider_and_reordered(self):
        shipped = list(REAL["merge_policy"]["trivial_paths"])
        _, w, _, _ = self.warn(shipped[:1])
        self.assertIn("in flows.yml but not router.yml", w[0])
        _, w, _, _ = self.warn(list(reversed(shipped)))
        self.assertIn("different order", w[0])

    def test_missing_trivial_paths_and_filter(self):
        data = copy.deepcopy(REAL)
        data["merge_policy"].pop("trivial_paths")
        path = write(self.tmp, data)
        with mock.patch.object(fr, "ROUTER_PATH", router(self.tmp, ["a"])):
            self.assertIn("trivial_paths is missing", fr.warnings(path)[0])
        broken = write(self.tmp, {"jobs": {}}, "router.yml")
        with mock.patch.object(fr, "ROUTER_PATH", broken):
            self.assertIn("has no detect-changes", fr.warnings(fr.default_path())[0])
        _, w, _, _ = self.warn([], router_present=False)
        self.assertEqual(w, [])

    def test_unreadable_flows_has_no_warnings(self):
        self.assertEqual(fr.warnings(os.path.join(self.tmp, "nope.yml")), [])

    def test_router_parse_error_is_none(self):
        bad = os.path.join(self.tmp, "r.yml")
        with open(bad, "w") as f:
            f.write("jobs: [unclosed\n")
        self.assertIsNone(fr._router_trivial_filter(bad))


class CacheTests(unittest.TestCase):
    def test_copies_are_independent_and_mtime_reloads(self):
        tmp = tempfile.mkdtemp()
        path = write(tmp, {"meta": {"version": 1}})
        a = fr.load(path)
        a["meta"]["version"] = 99
        self.assertEqual(fr.load(path)["meta"]["version"], 1)
        time.sleep(0.01)
        write(tmp, {"meta": {"version": 2}, "x": 1})
        os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        self.assertEqual(fr.load(path)["meta"]["version"], 2)

    def test_non_mapping_raises(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "l.yml")
        with open(path, "w") as f:
            f.write("[1, 2]\n")
        with self.assertRaises(ValueError):
            fr.load(path)


class LookupTests(unittest.TestCase):
    def test_lookups_and_errors(self):
        self.assertIn("dept:backend", fr.get_labels("dept"))
        self.assertIn("dept", fr.get_labels())
        with self.assertRaises(KeyError):
            fr.get_labels("nope")
        t = fr.get_transition("ticket-lifecycle", "dispatched", "needs_human")
        self.assertEqual(t["to"], "needs_human")
        with self.assertRaises(KeyError):
            fr.get_transition("nope", "a", "b")
        with self.assertRaises(KeyError):
            fr.get_transition("ticket-lifecycle", "a", "b")
        with self.assertRaises(KeyError):
            fr.get_role("nope")
        with self.assertRaises(KeyError):
            fr.get_flood_guard("nope")
        self.assertIn("ready-to-merge", fr.label_owners())
        self.assertIn("project:*", fr.label_owners())

    def test_new_ssdlc_labels_and_roles_present(self):
        names = {n for n, _ in fr.static_label_names()}
        for lb in ("needs-threat-model", "security:review-required", "security:approved",
                   "gate:sast", "gate:sca", "gate:secrets", "needs-approver-merge"):
            self.assertIn(lb, names)
        # gate:* only ever blocks: just its triage role may raise it, never a
        # reviewer/author, and no model may apply security:approved but the
        # independent security reviewer.
        self.assertEqual(fr.label_owners()["gate:sast"], {"sast-triage"})
        self.assertEqual(fr.label_owners()["security:approved"], {"security-reviewer"})
        for role in ("threat-modeler", "security-reviewer", "release-manager"):
            self.assertIn("authority", fr.get_role(role))

    def test_context_slice_for_role(self):
        sl = fr.context_slice_for_role("chief-of-staff")
        self.assertEqual(sl["role"]["badge"], "[Chief of Staff] ")
        self.assertNotIn("deprecated", sl["labels"])
        self.assertIsInstance(sl["idempotency_rules"], dict)

    def test_state_helpers(self):
        cfg = fr.get_label_states("issue")
        self.assertEqual(fr.exclusive_violations({"x"}, cfg), [])
        combo, _ = fr.resolve_state({"agent:implement"}, cfg)
        self.assertIsNotNone(combo)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fr._cli(argv)
        return rc, out.getvalue()

    def test_usage(self):
        self.assertEqual(self.run_cli([])[0], 2)
        self.assertEqual(self.run_cli(["nope"])[0], 2)

    def test_ok_warn_and_strict(self):
        tmp = tempfile.mkdtemp()
        rp = router(tmp, ["graphify-out/**", "docs/FLOW-DIAGRAM.md", "**/x.lock"])
        with mock.patch.object(fr, "ROUTER_PATH", rp):
            rc, out = self.run_cli(["validate"])
            self.assertEqual(rc, 0)
            self.assertIn("warning:", out)
            rc, out = self.run_cli(["validate", "--strict", fr.default_path()])
            self.assertEqual(rc, 1)
            self.assertIn("problem(s)", out)


if __name__ == "__main__":
    unittest.main()


class TrivialLaneValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def check(self, mutate):
        data = copy.deepcopy(REAL)
        mutate(data["merge_policy"])
        return fr.validate(write(self.tmp, data))

    def test_shipped_is_valid(self):
        self.assertEqual(self.check(lambda mp: None), [])

    def test_union_mismatch_glob_and_malformed(self):
        p = self.check(lambda mp: mp["trivial_paths"].append("x.md"))
        self.assertTrue(any("not the union" in x for x in p))
        p = self.check(lambda mp: mp["trivial_lanes"][0]["paths"].append("graphify-out/**"))
        self.assertTrue(any("no globs" in x for x in p))
        p = self.check(lambda mp: mp["trivial_lanes"].append({"head_branch": "x-*"}))
        self.assertTrue(any("needs head_branch and paths" in x for x in p))
        p = self.check(lambda mp: mp.__setitem__("trivial_lanes", {"a": 1}))
        self.assertTrue(any("must be a list" in x for x in p))
        self.assertEqual(self.check(lambda mp: mp.pop("trivial_lanes")), [])


def test_cli_help_is_argparse_usage(capsys):
    assert fr._cli(["-h"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: python -m squadhq.flow_reader") and "Load and validate" not in out
    assert fr._cli(["validate", "--bogus"]) == 2
