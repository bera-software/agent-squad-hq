import json
import os
import runpy

import pytest

from squadhq.skills.evals import cli, harness
from tests.evals.test_harness import Live
from tests.skills.helpers import make_hub, write_skill


@pytest.fixture
def hub(tmp_path):
    hub = make_hub(str(tmp_path))
    write_skill(hub, "alpha")
    with open(os.path.join(hub, "evals_thresholds.yml"), "w") as fh:
        fh.write("kinds: {injection: {min_pass_rate: 1.0}}\npolicy_violations_max: 0\n")
    return hub


def test_dry_run_writes_results(hub, capsys):
    out_dir = os.path.join(hub, "out")
    code = cli.main(["--hub", hub, "run", "--dry-run", "--out-dir", out_dir,
                     "--thresholds", os.path.join(hub, "evals_thresholds.yml")])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "Gate: PASS" in out
    files = sorted(os.listdir(out_dir))
    assert len(files) == 2 and files[0].endswith(".json") and files[1].endswith(".md")


def test_no_scored_cases_fails(hub, capsys):
    assert cli.main(["--hub", hub, "run", "--dry-run", "--no-write", "--kind", "standard"]) == 1
    assert "no cases were scored" in capsys.readouterr().out


def test_live_run_with_baseline_and_light(hub, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, "load_provider", lambda name: Live())
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"totals": {"pass_rate": 1.0}}))
    code = cli.main(["--hub", hub, "run", "--provider-ref", "live/big", "--light-provider-ref", "live/small",
                     "--baseline", str(base), "--no-write", "--skill", "alpha"])
    assert code == 0
    assert cli.main(["--hub", hub, "run", "--provider-ref", "live/big", "--light-provider-ref", "other/x",
                     "--no-write"]) == 2
    assert cli.main(["--hub", hub, "run", "--provider-ref", "bad", "--no-write"]) == 2


def test_list(hub, capsys):
    assert cli.main(["--hub", hub, "list"]) == 0
    out = capsys.readouterr().out
    assert "alpha/b" in out and "2 cases" in out


def test_module_entrypoint(hub, monkeypatch):
    monkeypatch.setattr("sys.argv", ["evals", "--hub", hub, "list"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("squadhq.skills.evals", run_name="__main__")
    assert exc.value.code == 0
