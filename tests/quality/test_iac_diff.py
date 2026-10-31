"""router.yml iac-scan: only findings a PR introduces fail the gate; findings
already on the base branch are listed, not blocking."""
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _script():
    with open(os.path.join(ROOT, ".github", "workflows", "router.yml")) as f:
        steps = yaml.safe_load(f)["jobs"]["iac-scan"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "trivy config")
    body = run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    return body


def _report(*items):
    return {"Results": [{"Target": t, "Misconfigurations": [
        {"AVDID": avd, "Severity": "CRITICAL", "Title": "t", "CauseMetadata": {"Resource": res, "StartLine": line}}]}
        for t, avd, res, line in items]}


def _run(tmp_path, base, head):
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "h.json").write_text(json.dumps(head))
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    return subprocess.run([sys.executable, "-I", "-", str(tmp_path / "b.json"), str(tmp_path / "h.json")],
                          input=_script(), text=True, capture_output=True, env=env)


def test_existing_findings_do_not_block(tmp_path):
    kv = ("infra/kv.tf", "AVD-AZU-0013", "azurerm_key_vault.main", 10)
    moved = ("infra/kv.tf", "AVD-AZU-0013", "azurerm_key_vault.main", 42)  # same finding, lines moved
    r = _run(tmp_path, _report(kv), _report(moved))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 new, 1 already on the base branch" in (tmp_path / "summary.md").read_text()


def test_a_new_finding_fails(tmp_path):
    kv = ("infra/kv.tf", "AVD-AZU-0013", "azurerm_key_vault.main", 10)
    sa = ("infra/sa.tf", "AVD-AZU-0012", "azurerm_storage_account.x", 3)
    r = _run(tmp_path, _report(kv), _report(kv, sa))
    assert r.returncode == 1 and "::error title=iac-scan::AVD-AZU-0012" in r.stdout


def test_unreadable_base_counts_everything_as_new(tmp_path):
    kv = ("infra/kv.tf", "AVD-AZU-0013", "azurerm_key_vault.main", 10)
    r = _run(tmp_path, {}, _report(kv))
    assert r.returncode == 1
