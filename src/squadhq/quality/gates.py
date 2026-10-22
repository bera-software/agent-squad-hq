"""Which architecture and lint gates apply to a checkout, and the quality kit.

    python -m squadhq.quality.gates kit --out DIR [--hub HUB] [--matrix JSON | env MATRIX]

Standard library only (see the package docstring).

``probe(entry, checkout)`` annotates one ``squadhq.packs ci-plan`` entry with
``gates``, from the files present in the project checkout and the project's
``quality`` policy:

``gates.arch``
    ``run``      the pack has an ``arch`` block and its ``config`` exists (and,
                 under ``auto``, its ``installed`` marker matches);
    ``missing``  no config but ``quality.arch: required`` (the router's arch
                 step then fails with the reason);
    ``skip``     no ``arch`` block, no config (``auto``), or ``off``.
``gates.lint``
    ``run``      the pack has ``ci.lint`` and one of its ``configs`` exists, or
                 the linter needs no config (``requires_config: false``);
    ``default``  no config, ``quality.lint: default`` and the pack ships a
                 default config: the router copies ``gates.lint_default.src``
                 from the kit to ``gates.lint_default.dst`` (never over an
                 existing file), then runs the command;
    ``skip``     otherwise.

Config markers are pack-root-relative globs, optionally ``<glob>:<text>`` to
require the file to contain ``<text>`` (``pyproject.toml:[tool.ruff``).
Symlinks and paths that leave the checkout never count.

``kit`` writes what tokenless jobs need from the hub into one directory that
the ci-plan job uploads as the ``squadhq-quality-kit`` artifact:
``coverage.py``, ``gates.py`` and ``snippets/<pack>/<file>`` for every entry
whose lint gate is ``default``.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from typing import Any

ARCH_MODES = ("auto", "required", "off")
LINT_MODES = ("auto", "default", "off")
CHANGED_COVERAGE_MODES = ("enforce", "report", "off")
DEFAULT_QUALITY = {"arch": "auto", "lint": "auto", "changed_coverage": "report"}
SKIP_DIRS = frozenset({".git", ".squadhq", "node_modules", ".venv", "venv", "vendor", "dist", "build", "target",
                       ".next", "coverage", "__pycache__", "bin", "obj", ".gradle", ".idea"})
MAX_WALK_FILES = 50_000
MAX_NEEDLE_BYTES = 1024 * 1024
_GLOB_CHARS = set("*?[")


def has_glob(pattern: str) -> bool:
    return any(c in pattern for c in _GLOB_CHARS)


def _inside(base: str, path: str) -> bool:
    real_base = os.path.realpath(base)
    real = os.path.realpath(path)
    return real == real_base or real.startswith(real_base + os.sep)


def _regular(base: str, path: str) -> bool:
    return os.path.isfile(path) and not os.path.islink(path) and _inside(base, path)


def _walk(base: str) -> list[str]:
    out: list[str] = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(cur, d)))
        rel = os.path.relpath(cur, base)
        for f in sorted(files):
            out.append(f if rel == "." else f"{rel.replace(os.sep, '/')}/{f}")
            if len(out) >= MAX_WALK_FILES:
                return out
    return out


def _glob_match(rel: str, pattern: str) -> bool:
    if fnmatch.fnmatch(rel, pattern):
        return True
    # "**/x" also matches "x" at the top level.
    while pattern.startswith("**/"):
        pattern = pattern[3:]
        if fnmatch.fnmatch(rel, pattern):
            return True
    return False


def _contains(path: str, needle: str) -> bool:
    try:
        with open(path, "rb") as f:
            return needle.encode("utf-8") in f.read(MAX_NEEDLE_BYTES)
    except OSError:
        return False


def find_marker(root_dir: str, marker: str) -> str | None:
    """Root-relative path of the first file matching ``marker`` (see module doc), or None."""
    pattern, _, needle = marker.partition(":")
    pattern = pattern.strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
        return None
    if not os.path.isdir(root_dir):
        return None
    if has_glob(pattern):
        candidates = [r for r in _walk(root_dir) if _glob_match(r, pattern)]
    else:
        candidates = [pattern]
    for rel in candidates:
        path = os.path.join(root_dir, rel)
        if _regular(root_dir, path) and (not needle or _contains(path, needle)):
            return rel
    return None


def quality_policy(raw: Any) -> dict[str, str]:
    """The project's ``quality`` block with defaults; unknown values fall back to the default."""
    q = dict(DEFAULT_QUALITY)
    if isinstance(raw, dict):
        for key, allowed in (("arch", ARCH_MODES), ("lint", LINT_MODES),
                             ("changed_coverage", CHANGED_COVERAGE_MODES)):
            v = raw.get(key)
            if isinstance(v, str) and v in allowed:
                q[key] = v
    return q


def probe(entry: dict[str, Any], checkout: str) -> dict[str, Any]:
    """``gates`` for one ci-plan entry (see module doc). Never raises on odd input."""
    q = quality_policy(entry.get("quality"))
    root = str(entry.get("root") or ".")
    root_dir = checkout if root in ("", ".") else os.path.join(checkout, root)
    if not _inside(checkout, root_dir):
        root_dir = os.path.join(checkout, ".squadhq-invalid-root")
    gates: dict[str, Any] = {"arch": "skip", "arch_reason": "", "lint": "skip", "lint_reason": "",
                             "lint_default": {"src": "", "dst": ""}}

    arch = entry.get("arch") or {}
    if not isinstance(arch, dict) or not arch.get("command"):
        gates["arch_reason"] = "this pack ships no architecture check"
    elif q["arch"] == "off":
        gates["arch_reason"] = "quality.arch: off"
    else:
        found = find_marker(root_dir, str(arch.get("config") or ""))
        installed = str(arch.get("installed") or "")
        if found and installed and q["arch"] == "auto" and not find_marker(root_dir, installed):
            # Config seeded (e.g. by onboarding) but the tool not added yet:
            # don't fail every PR until the owner finishes the setup.
            gates["arch_reason"] = (f"{found} present but {arch.get('tool') or 'the tool'} is not installed "
                                    f"({arch.get('requires') or installed}; quality.arch: auto)")
        elif found:
            gates["arch"], gates["arch_reason"] = "run", f"{arch.get('tool') or 'arch'} config {found}"
        elif q["arch"] == "required":
            gates["arch"] = "missing"
            gates["arch_reason"] = (f"quality.arch: required, but no {arch.get('config')} under {root} "
                                    f"(start from packs/{entry.get('pack')}/snippets/{arch.get('snippet') or ''})")
        else:
            gates["arch_reason"] = f"no {arch.get('config')} under {root} (quality.arch: auto)"

    lint = entry.get("lint") or {}
    if not isinstance(lint, dict) or not lint.get("command"):
        gates["lint_reason"] = "this pack has no separate lint step"
    elif q["lint"] == "off":
        gates["lint_reason"] = "quality.lint: off"
    else:
        configs = [str(c) for c in (lint.get("configs") or []) if c]
        found = next((f for f in (find_marker(root_dir, c) for c in configs) if f), None)
        if found:
            gates["lint"], gates["lint_reason"] = "run", f"config {found}"
        elif not lint.get("requires_config", True):
            gates["lint"], gates["lint_reason"] = "run", "the linter needs no config"
        elif q["lint"] == "default" and lint.get("snippet") and lint.get("snippet_path"):
            dst = str(lint["snippet_path"])
            gates["lint"], gates["lint_reason"] = "default", f"pack default config -> {dst}"
            gates["lint_default"] = {"src": f"snippets/{entry.get('pack')}/{lint['snippet']}", "dst": dst}
        else:
            hint = " (or quality.lint: default)" if lint.get("snippet") else ""
            gates["lint_reason"] = f"no linter config under {root}{hint}"
    return gates


def build_kit(plan: list[dict[str, Any]], hub: str, out: str) -> list[str]:
    """Copy the standalone scripts and the needed default lint configs into ``out``."""
    os.makedirs(out, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    written = []
    for name in ("coverage.py", "gates.py"):
        shutil.copyfile(os.path.join(here, name), os.path.join(out, name))
        written.append(name)
    packs_dir = os.path.realpath(os.path.join(hub, "packs"))
    for e in plan:
        src = ((e.get("gates") or {}).get("lint_default") or {}).get("src") or ""
        if not src.startswith("snippets/"):
            continue
        pack_rel = src[len("snippets/"):]
        pack_id, _, fname = pack_rel.partition("/")
        source = os.path.realpath(os.path.join(packs_dir, pack_id, "snippets", fname))
        if not fname or not source.startswith(packs_dir + os.sep) or not os.path.isfile(source):
            continue
        dest = os.path.join(out, "snippets", pack_id, fname)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(source, dest)
        written.append(f"snippets/{pack_id}/{fname}")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m squadhq.quality.gates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("kit", help="write the quality kit for tokenless jobs")
    k.add_argument("--out", required=True)
    k.add_argument("--hub", default=os.environ.get("SQUADHQ_HOME") or ".")
    k.add_argument("--matrix", default=None, help="ci-plan matrix JSON (default: env MATRIX)")
    a = ap.parse_args(argv)
    try:
        matrix = json.loads(a.matrix if a.matrix is not None else os.environ.get("MATRIX") or "[]")
    except ValueError as e:
        print(f"gates: matrix is not JSON: {e}", file=sys.stderr)
        return 2
    plan = matrix.get("include") if isinstance(matrix, dict) else matrix
    files = build_kit([e for e in (plan or []) if isinstance(e, dict)], a.hub, a.out)
    print("\n".join(files))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
