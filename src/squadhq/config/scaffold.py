"""Scaffold a new hub repository from the hub template.

The template is the source tree when this package runs from a checkout
(editable install), otherwise the copy bundled in the wheel under
``squadhq/_hub`` (see ``[tool.hatch.build.targets.wheel.force-include]``).
Scaffolding copies files and runs ``git init``; it never adds a remote or
pushes.

A scaffolded hub is self-contained: workflows ``pip install`` the hub itself
(``setup-hub``), so it also gets ``pyproject.toml``, the hashed
``requirements/actions.lock`` and the source of the running ``squadhq``
package under ``src/squadhq``, pinning the hub to the version that created it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable

from squadhq import paths

# Paths (relative to the hub root) that make up a hub. Keep in sync with
# pyproject's force-include table.
TEMPLATE_ITEMS: tuple[str, ...] = (
    "prompts", "agents", "packs", "skills", "org", "onboarding", "templates",
    "config/flows.yml", "schema", "evals/golden", "evals/thresholds.yml",
    ".github/workflows", ".github/actions", ".github/dependabot.yml",
    "squadhq.example.yml", "deploy/github-app", "docs/FLOW-DIAGRAM.md",
    "pyproject.toml", "requirements/actions.lock", "LICENSE", "NOTICE",
)
# Workflows of the agent-squad-hq project itself (its release and its own test
# suite), never copied into a user's hub.
PROJECT_ONLY: frozenset[str] = frozenset({
    os.path.join(".github", "workflows", "release.yml"),
    os.path.join(".github", "workflows", "hub-ci.yml"),
    os.path.join(".github", "workflows", "scorecard.yml"),
})
PACKAGE_DEST = os.path.join("src", "squadhq")
HUB_MARKER = os.path.join("config", "flows.yml")
GITIGNORE = "# never commit GitHub App keys\n*.pem\n__pycache__/\n.venv/\n*.egg-info/\nbuild/\ndist/\n"
# pyproject's `readme` must exist for `pip install <hub>`.
README = """# Agent Squad HQ hub

This repository is an [Agent Squad HQ](https://github.com/bera-software/agent-squad-hq) hub,
created by `squadhq init`. `squadhq.yml` is the configuration; `squadhq render` regenerates
`config/*.json` and the workflow schedules from it. `src/squadhq` is the pinned copy of the
squadhq package the workflows install.
"""
NOT_A_HUB = "not a hub checkout: run `squadhq init` or set SQUADHQ_HOME"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def is_hub(root: str) -> bool:
    return os.path.isfile(os.path.join(root, HUB_MARKER))


def bundled_root() -> str:
    return os.path.join(paths.PACKAGE_DIR, "_hub")


def template_root() -> str | None:
    """Source tree first (so edits in a checkout are what gets scaffolded),
    then the wheel's bundled copy; None when neither exists."""
    src = os.path.dirname(os.path.dirname(paths.PACKAGE_DIR))
    for cand in (src, bundled_root()):
        if is_hub(cand):
            return cand
    return None


def known_template_packs() -> set[str] | None:
    root = template_root()
    if root is None:
        return None
    from .loader import known_pack_ids
    return known_pack_ids(root)


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n in ("__pycache__", ".pytest_cache", ".DS_Store") or n.endswith(".pyc")]


def _copy_new(a: str, b: str) -> None:
    if not os.path.exists(b):
        shutil.copy2(a, b)


def _copytree(s: str, d: str, ignore: Callable[[str, list[str]], list[str]]) -> None:
    shutil.copytree(s, d, ignore=ignore, dirs_exist_ok=True, copy_function=_copy_new)


def package_source(source: str | None = None) -> str | None:
    """The squadhq package to vendor into the hub: ``<source>/src/squadhq``
    for an explicit template, else the running package (a checkout's
    ``src/squadhq`` or the installed wheel's package directory)."""
    if source is None:
        return paths.PACKAGE_DIR
    cand = os.path.join(source, PACKAGE_DEST)
    return cand if os.path.isfile(os.path.join(cand, "__init__.py")) else None


def scaffold(dest: str, *, source: str | None = None, package: str | None = None, git: bool = True,
             run: Runner | None = None) -> list[str]:
    """Copy the template into ``dest`` (created if missing), plus the squadhq
    package source (``package``, default :func:`package_source`) into
    ``dest/src/squadhq``. Existing files are never overwritten. Returns the
    template items copied."""
    src = source or template_root()
    if src is None:
        raise FileNotFoundError("hub template not found: reinstall agent-squad-hq (the wheel bundles it)")
    os.makedirs(dest, exist_ok=True)

    def ignore(dir_: str, names: list[str]) -> list[str]:
        rel = os.path.relpath(dir_, src)
        return _ignore(dir_, names) + [n for n in names if os.path.normpath(os.path.join(rel, n)) in PROJECT_ONLY]

    copied: list[str] = []
    for item in TEMPLATE_ITEMS:
        s, d = os.path.join(src, item), os.path.join(dest, item)
        if not os.path.exists(s):
            continue
        if os.path.isdir(s):
            _copytree(s, d, ignore)
        elif not os.path.exists(d):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
        copied.append(item)
    pkg = package or package_source(source)
    if pkg and os.path.isfile(os.path.join(pkg, "__init__.py")):
        def ignore_pkg(dir_: str, names: list[str]) -> list[str]:
            # _hub is the wheel's bundled template: the hub already is that template.
            return _ignore(dir_, names) + (["_hub"] if os.path.samefile(dir_, pkg) else [])

        _copytree(pkg, os.path.join(dest, PACKAGE_DEST), ignore_pkg)
        copied.append(PACKAGE_DEST)
    for name, text in ((".gitignore", GITIGNORE), ("README.md", README)):
        p = os.path.join(dest, name)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
    if git and not os.path.isdir(os.path.join(dest, ".git")):
        try:
            (run or subprocess.run)(["git", "init", "-q", "-b", "main", dest], capture_output=True, text=True,
                                    timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # git missing: the hub still works; the user can `git init` later
    return copied
