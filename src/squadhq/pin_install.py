"""Add or change JavaScript dependencies at exact versions, without install scripts.

Agents may run ``npm ci`` (the lockfile as committed) but not ``npm install``:
an unpinned install pulls whatever the registry serves today, and package
install scripts run arbitrary code. Dependency-upgrade tickets still need a
version change, so this is the one narrow way: every spec must be
``name@<exact version>`` (no ranges, tags or URLs), the name must be a valid
npm package name (so nothing can be passed as a flag), install scripts are
off, and the exact version is saved. The project's own package manager is used
(npm, pnpm or yarn, from its lockfile).

    python -P -m squadhq.pin_install [--dir frontend] [--dev] next@16.2.11 @types/node@22.15.3
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

NAME = r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*"
VERSION = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
SPEC_RE = re.compile(rf"^({NAME})@({VERSION})$")
MAX_SPECS = 20


class SpecError(ValueError):
    pass


def check_specs(specs: list[str]) -> list[str]:
    if not specs:
        raise SpecError("give at least one name@exact-version")
    if len(specs) > MAX_SPECS:
        raise SpecError(f"at most {MAX_SPECS} packages per call")
    for s in specs:
        if not SPEC_RE.match(s) or len(s) > 214:
            raise SpecError(f"{s!r} is not name@exact-version (no ranges, tags, URLs or flags)")
    return specs


def project_dir(path: str, cwd: str | None = None) -> str:
    """``path`` inside the checkout (relative, no ``..``), holding a package.json."""
    base = os.path.realpath(cwd or os.getcwd())
    if os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        raise SpecError("--dir must be a relative path inside the checkout")
    full = os.path.realpath(os.path.join(base, path))
    if os.path.commonpath([full, base]) != base:
        raise SpecError("--dir must stay inside the checkout")
    if not os.path.isfile(os.path.join(full, "package.json")):
        raise SpecError(f"no package.json in {path or '.'}")
    return full


def command(directory: str, specs: list[str], dev: bool) -> list[str]:
    has = lambda f: os.path.isfile(os.path.join(directory, f))  # noqa: E731
    if has("pnpm-lock.yaml"):
        return ["pnpm", "add", "--save-exact", "--ignore-scripts", *(["--save-dev"] if dev else []), *specs]
    if has("yarn.lock"):
        return ["yarn", "add", "--exact", "--ignore-scripts", *(["--dev"] if dev else []), *specs]
    return ["npm", "install", "--save-exact", "--ignore-scripts", *(["--save-dev"] if dev else []), *specs]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -P -m squadhq.pin_install", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=".", help="the package directory, relative to the checkout")
    ap.add_argument("--dev", action="store_true", help="save as a dev dependency")
    ap.add_argument("specs", nargs="*")
    a = ap.parse_args(argv)
    try:
        directory = project_dir(a.dir)
        specs = check_specs(a.specs)
    except SpecError as e:
        print(f"pin_install: {e}", file=sys.stderr)
        return 2
    cmd = command(directory, specs, a.dev)
    print("pin_install: " + " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, cwd=directory, timeout=900).returncode


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
