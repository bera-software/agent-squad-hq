import os

import pytest

from squadhq.config import scaffold as SC
from squadhq.config.github_ops import truncate_words
from squadhq.config.init_wizard import default_slug

from .hubfix import REPO_ROOT


def test_template_root_prefers_source_tree():
    assert SC.template_root() == REPO_ROOT
    assert "node-next" in SC.known_template_packs()


def test_template_root_bundled_and_missing(monkeypatch, tmp_path):
    pkg = tmp_path / "site" / "squadhq"
    (pkg / "_hub" / "config").mkdir(parents=True)
    (pkg / "_hub" / "config" / "flows.yml").write_text("labels: {}\n")
    monkeypatch.setattr(SC.paths, "PACKAGE_DIR", str(pkg))
    assert SC.template_root() == str(pkg / "_hub")
    assert SC.known_template_packs() is None
    os.remove(pkg / "_hub" / "config" / "flows.yml")
    assert SC.template_root() is None and SC.known_template_packs() is None
    with pytest.raises(FileNotFoundError):
        SC.scaffold(str(tmp_path / "x"))


def test_scaffold_copies_never_overwrites(tmp_path):
    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "config" / "flows.yml").write_text("new")
    (src / "prompts" / "__pycache__").mkdir(parents=True)
    (src / "prompts" / "a.md").write_text("A")
    (src / "prompts" / "b.md").write_text("B")
    (src / "squadhq.example.yml").write_text("ex")
    dest = tmp_path / "dest"
    (dest / "prompts").mkdir(parents=True)
    (dest / "prompts" / "a.md").write_text("mine")
    calls = []
    copied = SC.scaffold(str(dest), source=str(src), run=lambda *a, **k: calls.append(a[0]))
    assert set(copied) == {"config/flows.yml", "prompts", "squadhq.example.yml"}
    assert (dest / "prompts" / "a.md").read_text() == "mine" and (dest / "prompts" / "b.md").read_text() == "B"
    assert not (dest / "prompts" / "__pycache__").exists()
    assert calls == [["git", "init", "-q", "-b", "main", str(dest)]]
    (dest / ".git").mkdir()
    (src / "squadhq.example.yml").write_text("changed")
    SC.scaffold(str(dest), source=str(src), run=lambda *a, **k: calls.append(a[0]))
    assert len(calls) == 1 and (dest / "squadhq.example.yml").read_text() == "ex"


def test_scaffold_survives_missing_git(tmp_path):
    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "config" / "flows.yml").write_text("x")

    def no_git(*a, **k):
        raise FileNotFoundError("git")

    SC.scaffold(str(tmp_path / "d"), source=str(src), run=no_git)
    assert SC.is_hub(str(tmp_path / "d"))


def test_template_items_match_wheel_force_include():
    import tomllib
    with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as f:
        fi = tomllib.load(f)["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert {k for k, v in fi.items() if v == f"squadhq/_hub/{k}"} == set(SC.TEMPLATE_ITEMS)
    for item in ("pyproject.toml", "requirements/actions.lock"):
        assert item in SC.TEMPLATE_ITEMS and os.path.exists(os.path.join(REPO_ROOT, item))


def test_scaffold_vendors_package_and_skips_project_workflows(tmp_path):
    src = tmp_path / "tpl"
    (src / "config").mkdir(parents=True)
    (src / "config" / "flows.yml").write_text("x")
    wf = src / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name in ("release.yml", "hub-ci.yml", "hub-config.yml", "router.yml"):
        (wf / name).write_text(name)
    (src / "pyproject.toml").write_text("[project]\n")
    (src / "requirements").mkdir()
    (src / "requirements" / "actions.lock").write_text("pyyaml==6\n")
    # an installed wheel's package directory: the bundled template must not be vendored
    pkg = tmp_path / "site" / "squadhq"
    (pkg / "_hub" / "config").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "sub" / "_hub").mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (pkg / "sub" / "m.py").write_text("")
    (pkg / "sub" / "_hub" / "keep.txt").write_text("")
    dest = tmp_path / "hub"
    copied = SC.scaffold(str(dest), source=str(src), package=str(pkg), git=False)
    assert {"pyproject.toml", "requirements/actions.lock", "src/squadhq"} <= set(copied)
    assert sorted(os.listdir(dest / ".github" / "workflows")) == ["hub-config.yml", "router.yml"]
    vendored = dest / "src" / "squadhq"
    assert (vendored / "__init__.py").read_text() == '__version__ = "9.9.9"\n'
    assert not (vendored / "_hub").exists() and not (vendored / "__pycache__").exists()
    assert (vendored / "sub" / "m.py").exists() and (vendored / "sub" / "_hub" / "keep.txt").exists()
    assert (dest / "README.md").exists() and (dest / "requirements" / "actions.lock").exists()


def test_scaffold_from_checkout_vendors_running_package(tmp_path):
    dest = tmp_path / "hub"
    SC.scaffold(str(dest), git=False)
    assert (dest / "src" / "squadhq" / "cli.py").exists() and (dest / "pyproject.toml").exists()
    assert not (dest / ".github" / "workflows" / "release.yml").exists()
    assert not (dest / ".github" / "workflows" / "hub-ci.yml").exists()
    assert (dest / ".github" / "workflows" / "hub-config.yml").exists()
    assert SC.package_source(str(dest)) == str(dest / "src" / "squadhq")
    assert SC.package_source(str(tmp_path / "nowhere")) is None


def test_truncate_words():
    assert truncate_words("short") == "short"
    long = "Applied when the change is separable into independent parts " * 3
    t = truncate_words(long)
    assert len(t) <= 100 and t.endswith("…") and not t[:-1].endswith("separ")
    assert long.startswith(t[:-1])
    assert truncate_words("x" * 150) == "x" * 99 + "…"


def test_default_slug():
    assert default_slug("Acme") == "acme-squad"
    assert default_slug("My_Org.") == "my-org-squad"
    assert len(default_slug("a" * 60)) <= 34
    assert default_slug("___") == "acme-squad"
