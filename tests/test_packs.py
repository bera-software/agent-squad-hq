import json
import os

import pytest

from squadhq import packs as P
from tests.config.hubfix import FIXTURE_PACKS, REPO_ROOT, make_hub

PROJECTS = [
    {"name": "Storefront", "repo": "acme/storefront", "packs": ["node-next", "python-fastapi"],
     "pack_roots": {"node-next": "web"}},
    {"name": "Bare", "repo": "acme/bare"},
    {"name": "Ghost", "repo": "acme/ghost", "packs": ["nope"]},
]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    root = make_hub(tmp_path, projects=PROJECTS)
    monkeypatch.setenv("SQUADHQ_HOME", root)
    return root


def test_load_all_valid(hub):
    packs, errs = P.load_all()
    assert errs == [] and set(packs) == {"node-next", "python-fastapi", "java-spring"}
    n = packs["node-next"]
    assert n.root == "frontend" and n.ci.kind == "node" and n.ci.version == "20"
    assert n.ci.coverage.min_changed == 70


def test_broken_pack_errors():
    packs, errs = P.load_all(os.path.dirname(FIXTURE_PACKS))  # tests/fixtures as hub root
    assert "broken" not in packs
    text = "\n".join(errs)
    for needle in ("unknown key", "must equal its directory name", "kind: 'cobol'", "format: 'xml'",
                   "min_overall: expected a number", "not an allowedTools entry", "at least one change-detection",
                   "required when format is xml"):
        assert needle in text, needle


@pytest.mark.parametrize("data,needle", [
    ("[1]", "expected a mapping"),
    ("{id: Bad_Id, paths: [a]}", "lowercase"),
    ("{paths: [a]}", "id: is required"),
    ("{id: x, paths: [a], detect: [a]}", "detect: expected a mapping"),
    ("{id: x, paths: [a], root: ../up}", "relative path"),
    ("{id: x, paths: [a], ci: [1]}", "ci: expected a mapping"),
    ("{id: x, paths: [a], ci: {setup: 1}}", "setup: expected a mapping"),
    ("{id: x, paths: [a], ci: {setup: {kind: node}}}", "needs `build` or `test`"),
    ("{id: x, paths: [a], ci: {coverage: 1}}", "coverage: expected a mapping"),
    ("{id: x, paths: [a, 3]}", "list of non-empty strings"),
    ("{id: x, paths: [a], name: [1]}", "expected a string"),
])
def test_parse_pack_errors(data, needle):
    import yaml
    pack, errs = P.parse_pack(yaml.safe_load(data), "/hub/packs/x")
    assert pack is None and any(needle in e for e in errs), errs


def test_yaml_error_reported(tmp_path):
    os.makedirs(tmp_path / "packs" / "bad")
    (tmp_path / "packs" / "bad" / "pack.yml").write_text("id: [\n")
    (tmp_path / "packs" / "nomanifest").mkdir()
    packs, errs = P.load_all(str(tmp_path))
    assert packs == {} and "YAML error" in errs[0]
    assert P.load_all(str(tmp_path / "none")) == ({}, [])


def test_files(hub):
    assert P.prompt_files("acme/storefront", "implement") == ["packs/node-next/implement.md",
                                                             "packs/python-fastapi/implement.md"]
    assert P.prompt_files("ACME/Storefront", "review") == ["packs/node-next/review.md"]
    assert P.prompt_files("acme/storefront", "design") == []
    assert P.prompt_files("acme/bare", "implement") == []
    with pytest.raises(P.PackError):
        P.prompt_files("acme/storefront", "hack")


def test_ci_plan(hub):
    plan = P.ci_plan("acme/storefront")
    assert [p["pack"] for p in plan] == ["node-next", "python-fastapi"]
    node, py = plan
    assert node["root"] == "web" and node["paths"][0] == "web/**/*.ts"
    assert node["coverage"]["path"] == "web/coverage/lcov.info"
    assert node["setup"] == {"kind": "node", "version": "20"}
    assert py["root"] == "." and py["paths"] == ["**/*.py", "pyproject.toml"] and py["coverage"]["path"] == "coverage.xml"
    assert P.ci_plan("acme/bare") == []


def test_tools_and_risk(hub):
    assert P.tools("acme/storefront") == ["Bash(npm *)", "Bash(npx *)", "Bash(pytest *)"]
    assert P.risk_globs("acme/storefront") == ["**/middleware.ts", "**/auth/**", "**/migrations/**"]


def test_unknown_repo_and_pack(hub):
    with pytest.raises(P.PackError, match="not registered"):
        P.ci_plan("acme/missing")
    with pytest.raises(P.PackError, match="not found"):
        P.tools("acme/ghost")


def test_load_pack_invalid(tmp_path):
    root = make_hub(tmp_path, packs=())
    import shutil
    shutil.copytree(os.path.join(FIXTURE_PACKS, "broken"), os.path.join(root, "packs", "broken"))
    with pytest.raises(P.PackError, match="unknown key"):
        P.load_pack("broken", root)


def test_detect(hub, tmp_path):
    co = tmp_path / "checkout"
    (co / "web").mkdir(parents=True)
    (co / "node_modules" / "x").mkdir(parents=True)
    (co / "node_modules" / "x" / "pom.xml").write_text("")
    (co / "web" / "package.json").write_text("{}")
    (co / "pyproject.toml").write_text("")
    (co / "web" / "deep").mkdir()
    (co / "web" / "deep" / "pom.xml").write_text("")
    out = P.detect(str(co))
    assert {"pack": "node-next", "root": "web", "matched": "web/package.json"} in out
    assert {"pack": "python-fastapi", "root": ".", "matched": "pyproject.toml"} in out
    assert not any(o["pack"] == "java-spring" for o in out)
    assert any(o["pack"] == "java-spring" for o in P.detect(str(co), depth=2))


def test_main_commands(hub, capsys, tmp_path):
    assert P.main(["files", "--repo", "acme/storefront", "--role", "implement"]) == 0
    assert capsys.readouterr().out.splitlines() == ["packs/node-next/implement.md", "packs/python-fastapi/implement.md"]
    assert P.main(["ci-plan", "--repo", "acme/bare"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert P.main(["ci-plan", "--repo", "acme/storefront"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert P.main(["tools", "--repo", "acme/storefront"]) == 0
    assert "Bash(npm *)" in capsys.readouterr().out
    assert P.main(["risk-globs", "--repo", "acme/storefront"]) == 0
    assert "**/auth/**" in capsys.readouterr().out
    assert P.main(["list"]) == 0
    assert "node-next\tNode / Next.js\troot=frontend" in capsys.readouterr().out
    assert P.main(["validate"]) == 0
    assert "3 pack(s) valid" in capsys.readouterr().out
    (tmp_path / "co").mkdir()
    (tmp_path / "co" / "pom.xml").write_text("")
    assert P.main(["detect", str(tmp_path / "co")]) == 0
    assert json.loads(capsys.readouterr().out)[0]["pack"] == "java-spring"
    assert P.main(["tools", "--repo", "acme/missing"]) == 2
    assert "not registered" in capsys.readouterr().err


def test_main_validate_and_list_with_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SQUADHQ_HOME", os.path.dirname(FIXTURE_PACKS))
    assert P.main(["validate"]) == 1
    assert "error:" in capsys.readouterr().err
    assert P.main(["list"]) == 1


@pytest.mark.parametrize("data,needle", [
    ("{id: x, paths: [a], arch: 1}", "arch: expected a mapping"),
    ("{id: x, paths: [a], arch: {tool: t}}", "arch.command: is required"),
    ("{id: x, paths: [a], arch: {tool: t, config: c, command: c, extra: 1}}", "arch.extra: unknown key"),
    ("{id: x, paths: [a], arch: {tool: t, config: /abs, command: c}}", "relative path inside the project"),
    ("{id: x, paths: [a], arch: {tool: t, config: c, command: c, snippet: nope.cjs}}", "snippets/nope.cjs does not exist"),
    ("{id: x, paths: [a], arch: {tool: t, config: c, command: c, snippet: ../x}}", "must be a file name"),
    ("{id: x, paths: [a], arch: {tool: t, config: c, command: c, snippet_path: 'a/*.x'}}", "not a glob"),
    ("{id: x, paths: [a], ci: {lint: [1]}}", "lint: expected a mapping"),
    ("{id: x, paths: [a], ci: {lint: {configs: [a]}}}", "lint.command: is required"),
    ("{id: x, paths: [a], ci: {lint: {command: c}}}", "configs: required unless requires_config is false"),
    ("{id: x, paths: [a], ci: {lint: {command: c, requires_config: maybe}}}", "expected true or false"),
    ("{id: x, paths: [a], ci: {lint: {command: c, configs: ['../x']}}}", "relative path inside the project"),
    ("{id: x, paths: [a], ci: {lint: {command: c, configs: [a], bogus: 1}}}", "lint.bogus: unknown key"),
])
def test_parse_arch_and_lint_errors(data, needle):
    import yaml
    pack, errs = P.parse_pack(yaml.safe_load(data), "/hub/packs/x")
    assert pack is None and any(needle in e for e in errs), errs


def test_arch_and_lint_defaults(tmp_path):
    import yaml
    d = tmp_path / "x"
    (d / "snippets").mkdir(parents=True)
    (d / "snippets" / "cfg.ini").write_text("")
    (d / "snippets" / "ruff.toml").write_text("")
    pack, errs = P.parse_pack(yaml.safe_load(
        "{id: x, paths: [a], arch: {tool: t, config: .cfg, command: run, snippet: cfg.ini},"
        " ci: {lint: {command: lint, requires_config: false, snippet: ruff.toml}}}"), str(d))
    assert errs == []
    assert pack.arch.snippet_path == ".cfg" and pack.arch.local == "run"
    assert pack.ci.lint.snippet_path == "ruff.toml" and pack.ci.lint.local == "lint" and not pack.ci.lint.requires_config
    globbed, errs = P.parse_pack(yaml.safe_load(
        "{id: x, paths: [a], arch: {tool: t, config: '**/*Test.java', command: run, snippet: cfg.ini}}"), str(d))
    assert errs == [] and globbed.arch.snippet_path == ""  # a glob config is never seeded


def test_ci_plan_arch_lint_quality_and_gates(tmp_path, monkeypatch):
    projects = [dict(PROJECTS[0], quality={"arch": "required", "changed_coverage": "enforce"})]
    root = make_hub(tmp_path / "hub", projects=projects)
    monkeypatch.setenv("SQUADHQ_HOME", root)
    node, py = P.ci_plan("acme/storefront")
    assert node["arch"] == {"tool": "dependency-cruiser", "config": ".dependency-cruiser.cjs",
                            "command": "npx --no-install depcruise --config .dependency-cruiser.cjs .",
                            "snippet": "dependency-cruiser.cjs", "installed": "",
                            "requires": "npm install --save-dev dependency-cruiser"}
    assert node["lint"]["command"] == "npm run lint" and node["lint"]["requires_config"] is True
    assert py["arch"] is None and py["lint"] is None
    assert node["quality"] == {"arch": "required", "lint": "auto", "changed_coverage": "enforce"}
    assert "gates" not in node
    co = tmp_path / "co"
    (co / "web").mkdir(parents=True)
    (co / "web" / "package.json").write_text('{"scripts": {"lint": "eslint"}}')
    node, py = P.ci_plan("acme/storefront", checkout=str(co))
    assert node["gates"]["arch"] == "missing" and node["gates"]["lint"] == "run"
    assert py["gates"]["arch"] == "skip"
    (co / "web" / ".dependency-cruiser.cjs").write_text("")
    assert P.ci_plan("acme/storefront", checkout=str(co))[0]["gates"]["arch"] == "run"


def test_gate_commands(hub, capsys):
    assert P.gate_commands("acme/storefront") == [
        "- arch (node-next, dependency-cruiser; in web; when .dependency-cruiser.cjs exists): "
        "npx --no-install depcruise --config .dependency-cruiser.cjs .",
        "- lint (node-next; in web): npm run lint"]
    assert P.gate_commands("acme/bare") == []
    assert P.main(["gates", "--repo", "acme/storefront"]) == 0
    assert "npm run lint" in capsys.readouterr().out
    assert P.main(["ci-plan", "--repo", "acme/storefront", "--checkout", "."]) == 0
    assert "gates" in json.loads(capsys.readouterr().out)[0]


def _covered_by_tools(cmd, tools):
    import fnmatch
    import re
    pats = [t[5:-1] for t in tools if t.startswith("Bash(") and t.endswith(")")]
    pieces = [re.sub(r"\s*\([^)]*\)\s*$", "", p).strip() for p in re.split(r"\s+&&\s+|\s+or\s+", cmd)]
    return [p for p in pieces if p and not any(fnmatch.fnmatchcase(p, pat) for pat in pats)]


def test_shipped_packs_quality_gates_are_consistent():
    """Every shipped pack's arch/lint `local` command is runnable by an agent
    with the pack's own tool grants, every snippet exists, and every grant
    passes the tool-profile policy."""
    from squadhq.executors import tool_profiles
    packs, errs = P.load_all(REPO_ROOT)
    assert errs == [] and len(packs) >= 17
    for pack in packs.values():
        tool_profiles.validate_extra(pack.tools)
        for gate in (pack.arch, pack.ci.lint):
            if gate is None:
                continue
            local = gate.local
            if pack.id == "java-spring":
                local = local.split(", for the")[0]
            missing = _covered_by_tools(local, pack.tools)
            assert not missing, f"{pack.id}: {missing} not covered by tools"
            if gate.snippet:
                assert os.path.isfile(os.path.join(pack.dir, "snippets", gate.snippet))
    with_arch = {p.id for p in packs.values() if p.arch}
    assert {"node-next", "node-api", "react-vite", "react-native-expo", "python-fastapi", "python-django", "go",
            "dotnet", "java-spring", "php-laravel"} <= with_arch
    # Lint moved out of build: no pack runs its linter twice.
    for pid in ("node-next", "node-api", "react-vite", "react-native-expo"):
        assert "npm run lint" not in packs[pid].ci.build
    assert "ruff" not in packs["python-fastapi"].ci.build
    assert "clippy" not in packs["rust"].ci.build and "rubocop" not in packs["ruby-rails"].ci.build
