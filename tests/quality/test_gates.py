"""squadhq.quality.gates: config markers, the per-entry gate decision and the
quality kit."""
import json
import os

import pytest

from squadhq.quality import gates as G

ARCH = {"tool": "dependency-cruiser", "config": ".dependency-cruiser.cjs", "command": "depcruise .",
        "snippet": "dependency-cruiser.cjs"}
LINT = {"command": "npm run lint", "configs": ["package.json:\"lint\"", "eslint.config.*"], "requires_config": True,
        "snippet": "", "snippet_path": ""}


def entry(root=".", arch=ARCH, lint=LINT, quality=None, pack="node-next"):
    return {"pack": pack, "root": root, "arch": arch, "lint": lint, "quality": quality or {}}


def test_find_marker(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 1\n")
    (tmp_path / "src" / "test" / "java" / "a").mkdir(parents=True)
    (tmp_path / "src" / "test" / "java" / "a" / "HexArchitectureTest.java").write_text("")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "FooArchitectureTest.java").write_text("")
    (tmp_path / "BarArchitectureTest.java").write_text("")
    assert G.find_marker(str(tmp_path), "pyproject.toml:[tool.ruff") == "pyproject.toml"
    assert G.find_marker(str(tmp_path), "pyproject.toml:[tool.black") is None
    assert G.find_marker(str(tmp_path), "./pyproject.toml") == "pyproject.toml"
    assert G.find_marker(str(tmp_path), "**/*ArchitectureTest.java") == "BarArchitectureTest.java"
    (tmp_path / "BarArchitectureTest.java").unlink()
    assert G.find_marker(str(tmp_path), "**/*ArchitectureTest.java") == "src/test/java/a/HexArchitectureTest.java"
    for bad in ("", "/etc/passwd", "../x", "a/../../x"):
        assert G.find_marker(str(tmp_path), bad) is None
    assert G.find_marker(str(tmp_path / "missing"), "x") is None


def test_find_marker_refuses_symlinks(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text("[tool.ruff]")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ruff.toml").symlink_to(outside)
    (repo / "linked").symlink_to(tmp_path, target_is_directory=True)
    assert G.find_marker(str(repo), "ruff.toml") is None
    assert G.find_marker(str(repo), "**/outside.toml") is None


def test_quality_policy_defaults_and_unknown_values():
    assert G.quality_policy(None) == {"arch": "auto", "lint": "auto", "changed_coverage": "report"}
    assert G.quality_policy({"arch": "required", "lint": "bogus", "changed_coverage": "enforce"}) == \
        {"arch": "required", "lint": "auto", "changed_coverage": "enforce"}


def test_probe_arch(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    g = G.probe(entry("web"), str(tmp_path))
    assert g["arch"] == "skip" and "quality.arch: auto" in g["arch_reason"]
    g = G.probe(entry("web", quality={"arch": "required"}), str(tmp_path))
    assert g["arch"] == "missing" and "packs/node-next/snippets/dependency-cruiser.cjs" in g["arch_reason"]
    (web / ".dependency-cruiser.cjs").write_text("module.exports = {}")
    assert G.probe(entry("web"), str(tmp_path))["arch"] == "run"
    assert G.probe(entry("web", quality={"arch": "off"}), str(tmp_path))["arch"] == "skip"
    assert G.probe(entry("web", arch=None), str(tmp_path))["arch_reason"] == "this pack ships no architecture check"
    # A root that escapes the checkout never finds anything.
    assert G.probe(entry("../.."), str(tmp_path))["arch"] == "skip"



def test_probe_arch_waits_for_the_tool_under_auto(tmp_path):
    arch = dict(ARCH, installed='package.json:"dependency-cruiser"',
                requires="npm install --save-dev dependency-cruiser")
    (tmp_path / ".dependency-cruiser.cjs").write_text("module.exports = {}")
    (tmp_path / "package.json").write_text('{"devDependencies": {}}')
    g = G.probe(entry(arch=arch), str(tmp_path))
    assert g["arch"] == "skip" and "not installed" in g["arch_reason"] and "npm install" in g["arch_reason"]
    # required still runs (and fails loudly in the arch step if the tool is absent).
    assert G.probe(entry(arch=arch, quality={"arch": "required"}), str(tmp_path))["arch"] == "run"
    (tmp_path / "package.json").write_text('{"devDependencies": {"dependency-cruiser": "18.5.0"}}')
    assert G.probe(entry(arch=arch), str(tmp_path))["arch"] == "run"

def test_probe_lint(tmp_path):
    g = G.probe(entry(), str(tmp_path))
    assert g["lint"] == "skip" and g["lint_reason"].startswith("no linter config")
    (tmp_path / "package.json").write_text('{"scripts": {"lint": "eslint ."}}')
    assert G.probe(entry(), str(tmp_path))["lint"] == "run"
    assert G.probe(entry(quality={"lint": "off"}), str(tmp_path))["lint"] == "skip"
    no_cfg = dict(LINT, configs=[], requires_config=False)
    assert G.probe(entry(lint=no_cfg), str(tmp_path / "empty"))["lint_reason"] == "the linter needs no config"
    assert G.probe(entry(lint=None), str(tmp_path))["lint_reason"] == "this pack has no separate lint step"


def test_probe_lint_default(tmp_path):
    lint = dict(LINT, configs=["ruff.toml"], snippet="ruff.toml", snippet_path="ruff.toml")
    g = G.probe(entry(lint=lint, pack="python-fastapi"), str(tmp_path))
    assert g["lint"] == "skip" and "quality.lint: default" in g["lint_reason"]
    g = G.probe(entry(lint=lint, pack="python-fastapi", quality={"lint": "default"}), str(tmp_path))
    assert g["lint"] == "default"
    assert g["lint_default"] == {"src": "snippets/python-fastapi/ruff.toml", "dst": "ruff.toml"}
    (tmp_path / "ruff.toml").write_text("")
    assert G.probe(entry(lint=lint, quality={"lint": "default"}), str(tmp_path))["lint"] == "run"


def test_build_kit(tmp_path):
    hub = tmp_path / "hub"
    (hub / "packs" / "python-fastapi" / "snippets").mkdir(parents=True)
    (hub / "packs" / "python-fastapi" / "snippets" / "ruff.toml").write_text("[format]\n")
    plan = [
        {"gates": {"lint_default": {"src": "snippets/python-fastapi/ruff.toml", "dst": "ruff.toml"}}},
        {"gates": {"lint_default": {"src": "snippets/python-fastapi/../../../etc", "dst": "x"}}},
        {"gates": {"lint_default": {"src": "snippets/ghost/x.toml", "dst": "x"}}},
        {"gates": {}},
    ]
    out = tmp_path / "kit"
    assert G.build_kit(plan, str(hub), str(out)) == ["coverage.py", "gates.py", "snippets/python-fastapi/ruff.toml"]
    assert (out / "snippets" / "python-fastapi" / "ruff.toml").read_text() == "[format]\n"
    assert (out / "coverage.py").read_text().startswith('"""Changed-lines coverage')


def test_main_kit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MATRIX", json.dumps({"include": [{"gates": {}}]}))
    assert G.main(["kit", "--out", str(tmp_path / "k"), "--hub", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["coverage.py", "gates.py"]
    assert G.main(["kit", "--out", str(tmp_path / "k"), "--matrix", "{bad"]) == 2


@pytest.mark.parametrize("name", ["coverage.py", "gates.py"])
def test_kit_scripts_are_stdlib_only(name):
    """The kit runs as `python3 -I <file>` without the squadhq package."""
    src = open(os.path.join(os.path.dirname(G.__file__), name)).read()
    assert "import squadhq" not in src and "from squadhq" not in src
