"""Token counts reach the usage report: each agent job's metrics step puts them in its outputs,
and the post job's rebuilt execution result carries them to normalize (and on to the broker)."""

import json
import os
import shutil
import subprocess

import pytest
import yaml

from squadhq.executors.normalize import normalize_claude_code
from squadhq.paths import _SOURCE_ROOT as ROOT

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and os.path.exists("/usr/bin/python3")), reason="needs bash and /usr/bin/python3"
)

USAGE = {"input_tokens": 1200, "output_tokens": 340, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 50}


def _step(workflow: str, step_id: str) -> str:
    with open(os.path.join(ROOT, ".github", "workflows", workflow), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("id") == step_id:
                return step["run"]
    raise AssertionError(f"{workflow} has no {step_id} step")


def _bash(script: str, cwd, env: dict[str, str]) -> dict[str, str]:
    gh_out = cwd / "gh_output"
    gh_out.write_text("")
    bin_dir = cwd / "bin"  # the post jobs call `python` (setup-python's)
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "python"
    shim.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
    shim.chmod(0o755)
    full = {"PATH": f"{bin_dir}:/usr/bin:/bin", "RUNNER_TEMP": str(cwd), "GITHUB_OUTPUT": str(gh_out), **env}
    r = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-e", "-c", script],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in gh_out.read_text().splitlines() if "=" in line)


@pytest.mark.parametrize("workflow", ["router.yml", "squadhq-agent.yml", "agent-exec.yml"])
def test_metrics_put_tokens_in_the_outputs(tmp_path, workflow):
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps([{"type": "result", "total_cost_usd": 0.5, "num_turns": 3, "usage": USAGE}]))
    (tmp_path / "out").mkdir()
    (tmp_path / "plan.json").write_text("{}")
    out = _bash(
        _step(workflow, "metrics"),
        tmp_path,
        {
            "EXECUTION_FILE": str(execution),
            "OUT": str(tmp_path / "out"),
            "DELIVER": "",
            "OUTCOME": "success",
            "PLAN": str(tmp_path / "plan.json"),
        },
    )
    assert out["tokens"] == "1200,340,9000,50"


@pytest.mark.parametrize("workflow", ["router.yml", "squadhq-agent.yml", "agent-run.yml"])
def test_the_rebuilt_result_carries_tokens(tmp_path, workflow):
    out = _bash(_step(workflow, "file"), tmp_path, {"COST": "0.5", "TURNS": "3", "TOKENS": "1200,340,9000,50"})
    data = json.loads(open(out["path"], encoding="utf-8").read())
    assert data[0]["usage"] == USAGE
    result = normalize_claude_code(data, role="implement")
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (1200, 340, 0.5)
    # Nothing reported (an older agent job, or no usage): no usage block, as before.
    out = _bash(_step(workflow, "file"), tmp_path, {"COST": "0.5", "TOKENS": ""})
    assert "usage" not in json.loads(open(out["path"], encoding="utf-8").read())[0]
