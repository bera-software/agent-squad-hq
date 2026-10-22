"""The router's quality sub-checks: the ci-plan action's gate outputs and the
ci-gate evaluation of the `arch` / `lint` stack-ci steps and the
`changed-coverage` job. The ci-gate script is the one embedded in
router.yml, run against a fake `gh` on PATH (no network)."""
import json
import os
import stat
import subprocess
import sys

import pytest
import yaml

from tests.config.hubfix import REPO_ROOT, make_hub

ROUTER = os.path.join(REPO_ROOT, ".github", "workflows", "router.yml")
PLAN_PY = os.path.join(REPO_ROOT, ".github", "actions", "ci-plan", "plan.py")


def router():
    with open(ROUTER) as f:
        return yaml.safe_load(f)


def gate_script():
    step = next(s for s in router()["jobs"]["ci-gate"]["steps"] if s.get("name") == "Evaluate gate")
    return step["run"].split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]


FAKE_GH = '''#!{python}
import json, os, sys
args = sys.argv[1:]
path = args[1] if len(args) > 1 else ""
if path.endswith("/jobs?per_page=100"):
    for row in json.loads(os.environ.get("FAKE_JOBS", "[]")):
        print(json.dumps(row))
elif "/actions/runs?head_sha=" in path:
    print(json.dumps({{"id": 1, "path": ".github/workflows/agents.yml"}}))
'''


def leg(name, arch="success", lint="success", conclusion=None):
    steps = [{"name": "Install", "conclusion": "success"}]
    steps += [{"name": "arch", "conclusion": arch}, {"name": "lint", "conclusion": lint},
              {"name": "Build, test", "conclusion": "success"}]
    bad = any(c == "failure" for c in (arch, lint))
    return {"name": f"hub / stack-ci ({name})", "status": "completed",
            "conclusion": conclusion or ("failure" if bad else "success"), "started_at": "2026-10-01T00:00:00Z",
            "steps": steps}


def run_gate(tmp_path, jobs, **env):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    summary = tmp_path / "summary.md"
    summary.write_text("")
    base = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(summary),
        "REPO": "acme/app", "SHA": "a" * 40, "RUN_ID": "1", "ACTION": "synchronize", "IS_DRAFT": "false",
        "DC_RESULT": "success", "DC_FORK": "false", "REQ_PLAN": "true", "R_PLAN": "success",
        "REQ_STACK_CI": "true", "R_STACK_CI": "success", "WAIT_TOTAL": "0",
        "WORKFLOW_REF": "acme/app/.github/workflows/agents.yml@refs/pull/1/merge",
        "FAKE_JOBS": json.dumps(jobs),
    }
    base.update(env)
    r = subprocess.run([sys.executable, "-I", "-"], input=gate_script(), env=base, capture_output=True, text=True,
                       timeout=60)
    return r.returncode, summary.read_text()


def test_arch_and_lint_steps_are_sub_checks(tmp_path):
    jobs = [leg("web", lint="failure"), leg("api", arch="skipped", lint="success")]
    rc, text = run_gate(tmp_path, jobs, REQ_ARCH="true", REQ_LINT="true", R_STACK_CI="failure")
    assert rc == 1
    assert "✅ arch: passed" in text and "❌ lint: REQUIRED and failure" in text


def test_all_quality_sub_checks_pass(tmp_path):
    jobs = [leg("web"), leg("api", arch="skipped")]
    rc, text = run_gate(tmp_path, jobs, REQ_ARCH="true", REQ_LINT="true", REQ_CHANGED_COVERAGE="true",
                        R_CHANGED_COVERAGE="success")
    assert rc == 0, text
    assert "✅ arch: passed" in text and "✅ lint: passed" in text and "✅ changed-coverage: passed" in text


def test_required_sub_check_that_never_ran_fails(tmp_path):
    jobs = [leg("web", arch="skipped")]
    rc, text = run_gate(tmp_path, jobs, REQ_ARCH="true")
    assert rc == 1 and "❌ arch: REQUIRED and not_run" in text


def test_changed_coverage_enforced_and_report_only(tmp_path):
    rc, text = run_gate(tmp_path, [leg("web")], REQ_CHANGED_COVERAGE="true", R_CHANGED_COVERAGE="failure")
    assert rc == 1 and "❌ changed-coverage: REQUIRED and failure" in text
    rc, text = run_gate(tmp_path, [leg("web")], CHANGED_COVERAGE_MODE="report", R_CHANGED_COVERAGE="failure")
    assert rc == 0, text
    assert "⏭ changed-coverage: report only (quality.changed_coverage: report" in text
    assert "⏭ arch: no architecture config" in text and "⏭ lint: no linter config" in text


def test_label_event_reads_steps_from_recorded_runs(tmp_path):
    rc, text = run_gate(tmp_path, [leg("web", arch="failure")], ACTION="labeled", REQ_ARCH="true",
                        R_STACK_CI="skipped")
    assert rc == 1 and "❌ arch: REQUIRED and failure" in text


def test_router_wiring():
    jobs = router()["jobs"]
    names = [s.get("name") for s in jobs["stack-ci"]["steps"]]
    # ci-gate matches these step names; build/test must still run after a failing gate.
    assert names.index("Install") < names.index("arch") < names.index("lint") < names.index("Build, test")
    for name in ("arch", "lint", "Build, test"):
        step = next(s for s in jobs["stack-ci"]["steps"] if s.get("name") == name)
        assert step["if"].startswith("!cancelled() && steps.install.outcome == 'success'")
    cc = jobs["changed-coverage"]
    assert cc["permissions"] == {"contents": "read"}
    assert "secrets." not in yaml.safe_dump(cc)
    assert all(s.get("with", {}).get("persist-credentials") is False
               for s in cc["steps"] if "actions/checkout" in str(s.get("uses")))
    run = next(s["run"] for s in cc["steps"] if "run" in s)
    assert run.startswith("python3 -I ") and "coverage.py" in run
    assert "changed-coverage" in jobs["ci-gate"]["needs"]
    assert {"arch", "lint", "changed_coverage"} <= set(jobs["ci-plan"]["outputs"])


def load_plan_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ci_plan_action", PLAN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("plan,expected", [
    ([], {"arch": "false", "lint": "false", "changed_coverage": "off"}),
    ([{"gates": {"arch": "missing", "lint": "skip"}, "coverage": {"format": "lcov", "min_changed": 70},
       "quality": {"changed_coverage": "enforce"}}], {"arch": "true", "lint": "false", "changed_coverage": "enforce"}),
    ([{"gates": {"arch": "skip", "lint": "default"}, "coverage": {"format": "lcov", "min_changed": 0},
       "quality": {"changed_coverage": "enforce"}}], {"arch": "false", "lint": "true", "changed_coverage": "report"}),
    ([{"gates": {}, "coverage": {"format": "none"}, "quality": {"changed_coverage": "enforce"}},
      {"gates": {}, "coverage": {"format": "jacoco"}, "quality": {"changed_coverage": "off"}}],
     {"arch": "false", "lint": "false", "changed_coverage": "off"}),
])
def test_plan_gate_outputs(plan, expected):
    assert load_plan_module().gate_outputs(plan) == expected


def test_plan_action_end_to_end(tmp_path):
    projects = [{"name": "S", "repo": "acme/storefront", "packs": ["node-next", "python-fastapi"],
                 "pack_roots": {"node-next": "web"}, "quality": {"changed_coverage": "enforce"}}]
    hub = make_hub(tmp_path / "hub", projects=projects)
    co = tmp_path / "co"
    (co / "web").mkdir(parents=True)
    (co / "web" / ".dependency-cruiser.cjs").write_text("")
    changed = tmp_path / "changed.txt"
    changed.write_text("web/src/a.ts\n")
    env = dict(os.environ, SQUADHQ_HOME=hub)
    env.pop("GITHUB_STEP_SUMMARY", None)
    r = subprocess.run([sys.executable, PLAN_PY, "--repo", "acme/storefront", "--workdir", str(co),
                        "--changed", str(changed)], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["source"] == "packs" and out["count"] == 1
    (entry,) = out["include"]
    assert entry["id"] == "node-next-web" and entry["gates"]["arch"] == "run" and entry["gates"]["lint"] == "skip"
    assert (out["arch"], out["lint"], out["changed_coverage"]) == ("true", "false", "enforce")


def test_plan_action_auto_detect_has_no_gates(tmp_path):
    co = tmp_path / "co"
    co.mkdir()
    (co / "go.mod").write_text("module x\n")
    env = dict(os.environ, SQUADHQ_HOME=str(tmp_path / "nohub"))
    env.pop("GITHUB_STEP_SUMMARY", None)
    r = subprocess.run([sys.executable, PLAN_PY, "--repo", "acme/x", "--workdir", str(co)], env=env,
                       capture_output=True, text=True, timeout=120)
    out = json.loads(r.stdout)
    assert out["source"] == "auto-detect"
    assert out["include"][0]["gates"]["arch"] == "skip" and out["arch"] == "false"
