import json
import os

import pytest
import yaml

from squadhq.skills import cli
from tests.skills.helpers import make_hub, write_skill


@pytest.fixture
def hub(tmp_path):
    hub = make_hub(str(tmp_path))
    write_skill(hub, "alpha")
    write_skill(hub, "beta", phase="verify", tier="pro", default_enabled=False, requires=["alpha"])
    det = {"deterministic": True, "model_tier": "none", "prompt": None, "untrusted_inputs": False}
    write_skill(hub, "robot", entrypoint="workflow:x.yml#job", cases=[], **det)
    return hub


def run(hub, *args):
    return cli.main(["--root", os.path.join(hub, "skills"), *args])


def test_validate_ok_and_json(hub, capsys):
    assert run(hub, "validate") == 0
    assert "OK: 3 skills" in capsys.readouterr().out
    assert run(hub, "validate", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["skills"] == 3 and data["errors"] == []


def test_validate_failure_and_strict(hub, capsys):
    write_skill(hub, "gamma", outputs=[{"type": "label", "labels": ["unknown-label"]}])
    assert run(hub, "validate") == 0
    assert run(hub, "validate", "--strict") == 1
    out = capsys.readouterr().out
    assert "WARNING gamma: label 'unknown-label'" in out and "FAILED" in out
    write_skill(hub, "delta", tier="gold")
    assert run(hub, "validate") == 1
    assert "ERROR   delta" in capsys.readouterr().out


def test_list_table_filters_and_json(hub, capsys):
    assert run(hub, "list") == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "3 skills" in out
    assert run(hub, "list", "--tier", "pro", "--format", "json") == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in data] == ["beta"]
    assert run(hub, "list", "--phase", "verify") == 0
    assert "alpha" not in capsys.readouterr().out.split("\n", 2)[2]


def test_list_enabled_for(hub, tmp_path, capsys):
    cfg = tmp_path / "squadhq.yml"
    cfg.write_text(yaml.safe_dump({"skills": {"alpha": False, "beta": True},
                                   "projects": [{"name": "web", "skills": {"alpha": True}}]}))
    assert run(hub, "list", "--enabled-for", str(cfg), "--format", "json") == 0
    data = {d["id"]: d for d in json.loads(capsys.readouterr().out)}
    assert data["alpha"]["enabled"] is False and data["beta"]["missing_requires"] == ["alpha"]
    assert run(hub, "list", "--enabled-for", str(cfg), "--only-enabled") == 0
    out = capsys.readouterr().out
    assert "needs alpha" in out and "robot" in out
    assert run(hub, "list", "--enabled-for", str(cfg), "--project", "web", "--only-enabled",
               "--format", "json") == 0
    assert {d["id"] for d in json.loads(capsys.readouterr().out)} == {"alpha", "beta", "robot"}
    assert run(hub, "list", "--enabled-for", str(cfg), "--project", "ghost") == 2


def test_render_prompt(hub, tmp_path, capsys):
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"issue": {"number": 3, "body": "hello", "author": "x"}}))
    assert run(hub, "render-prompt", "alpha", "--context-file", str(ctx)) == 0
    out = capsys.readouterr().out
    assert "HUB PREAMBLE RULES" in out and '<untrusted source="issue#3" author="x"' in out
    assert run(hub, "render-prompt", "alpha", "--json") == 0
    assert json.loads(capsys.readouterr().out)["skill"] == "alpha"
    assert run(hub, "render-prompt", "robot") == 2
    assert run(hub, "render-prompt", "ghost") == 2
    assert "unknown skill" in capsys.readouterr().err


def test_catalogue_md_write_check_stdout(hub, tmp_path, capsys):
    out = tmp_path / "CAT.md"
    assert run(hub, "catalogue-md", "--check", "--output", str(out)) == 1
    assert run(hub, "catalogue-md", "--output", str(out)) == 0
    assert out.read_text().startswith("# Skill catalogue")
    assert run(hub, "catalogue-md", "--check", "--output", str(out)) == 0
    capsys.readouterr()
    assert run(hub, "catalogue-md", "--output", "-") == 0
    assert capsys.readouterr().out.startswith("# Skill catalogue")
    assert run(hub, "catalogue-md") == 0
    assert os.path.isfile(os.path.join(hub, "docs", "skills", "CATALOGUE.md"))


def test_module_entrypoint(hub, monkeypatch):
    import runpy
    monkeypatch.setattr("sys.argv", ["squadhq.skills", "--root", os.path.join(hub, "skills"), "validate"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("squadhq.skills", run_name="__main__")
    assert exc.value.code == 0
