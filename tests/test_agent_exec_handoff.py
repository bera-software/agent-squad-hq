"""agent-exec's metrics step: a run that writes no hand-off file (nothing to change, so no
PR title) must not fail the agent job. The step runs under `bash -e`, so a helper that
returns the status of a failed `[ -f ]` used to end it with a bare "exit code 1"."""

import os
import shutil
import subprocess

import pytest
import yaml

from squadhq.paths import _SOURCE_ROOT as ROOT

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "agent-exec.yml")


def _metrics_script() -> str:
    with open(WORKFLOW, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("id") == "metrics":
                return step["run"]
    raise AssertionError("agent-exec.yml has no metrics step")


def _run(tmp_path, deliver: str, files: dict[str, str]) -> subprocess.CompletedProcess:
    out = tmp_path / "out"
    work = tmp_path / "work"
    (work / ".squadhq-out").mkdir(parents=True)
    for name, text in files.items():
        (work / ".squadhq-out" / name).write_text(text)
    gh_out = tmp_path / "gh_output"
    gh_out.touch()
    env = {
        "PATH": "/usr/bin:/bin",
        "DELIVER": deliver,
        "OUTCOME": "success",
        "OUT": str(out),
        "EXECUTION_FILE": str(tmp_path / "missing.json"),
        "GITHUB_OUTPUT": str(gh_out),
    }
    return subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-e", "-c", _metrics_script()],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and os.path.exists("/usr/bin/python3")), reason="needs bash and /usr/bin/python3"
)


@pytest.mark.parametrize("deliver", ["pr", "findings"])
def test_no_handoff_file_does_not_fail_the_step(tmp_path, deliver):
    r = _run(tmp_path, deliver, {})
    assert r.returncode == 0, r.stderr
    assert "is-error=false" in (tmp_path / "gh_output").read_text()


def test_handoff_files_are_copied(tmp_path):
    r = _run(tmp_path, "pr", {"pr-title.txt": "Add a lesson", "pr-body.md": "Why."})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / "pr-title.txt").read_text() == "Add a lesson"
    assert (tmp_path / "out" / "pr-body.md").read_text() == "Why."
