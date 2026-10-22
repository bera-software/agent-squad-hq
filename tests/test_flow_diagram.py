"""squadhq.flow_diagram: Mermaid rendering and the generated doc."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import yaml

from squadhq import flow_diagram as fd
from squadhq import flow_reader


class RenderTests(unittest.TestCase):
    def test_edge_labels_are_sanitised_and_truncated(self):
        t = {"actor": "po", "trigger": 'a "quoted" | piped\nmultiline ' + "x" * 80}
        label = fd._edge_label(t)
        self.assertTrue(label.startswith("po: a 'quoted' / piped multiline"))
        self.assertTrue(label.endswith("..."))
        self.assertEqual(fd._edge_label({}), "")
        self.assertEqual(fd._mermaid_id("a-b c"), "a_b_c")

    def test_flow_to_mermaid_handles_lists_and_gaps(self):
        flow = {"states": ["a", "b", "c"], "transitions": [
            {"from": ["a", None], "to": "b", "actor": "x"},
            {"from": "b", "to": None},
            {"from": "b", "to": "c"}]}
        out = fd.flow_to_mermaid("f", flow)
        self.assertIn("a --> b : x", out)
        self.assertIn("    b --> c", out)
        self.assertNotIn("None", out)

    def test_generate_matches_committed_doc(self):
        committed = os.path.join(os.path.dirname(flow_reader.default_path()), "..", "docs", "FLOW-DIAGRAM.md")
        text = fd.generate()
        self.assertIn("## Label states — PRs", text)
        self.assertIn("```mermaid", text)
        with open(committed) as f:
            self.assertEqual(f.read(), text, "docs/FLOW-DIAGRAM.md is stale: run python -m squadhq.flow_diagram")

    def test_label_states_markdown_variants(self):
        data = {"label_states": {"pr": {
            "state_labels": ["x"], "combos": [{"labels": [], "state": "f.s"}],
            "exclusive": [["a", "b"]], "modifiers": [{"label": "m", "meaning": "mm"}],
            "repairs": [{"groups": [["a"]], "escalate": True, "reason": "r"},
                        {"groups": [["a"], ["b"]], "unless": ["c"], "remove": ["a"], "add": ["b"], "reason": "r2"}],
            "fact_repairs": [{"when": "w", "action": "act"}]}}}
        out = "\n".join(fd.label_states_markdown(data))
        for needle in ("## Label states — PRs", "*(none)*", "**Exclusive**", "`m` — mm",
                       "escalate (`needs-human`)", "remove `a`; add `b`", "normalize_pr", "- w: act"):
            self.assertIn(needle, out)

    def test_main_writes_file(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "flows.yml")
        with open(src, "w") as f:
            yaml.safe_dump({"flows": {"demo": {"description": "D", "states": ["a", "b"],
                                               "transitions": [{"from": "a", "to": "b"}]}}}, f)
        out = os.path.join(tmp, "sub", "FLOW.md")
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd.main([src, out])
        self.assertIn("1 flows", buf.getvalue())
        self.assertIn("## demo", open(out).read())


if __name__ == "__main__":
    unittest.main()
