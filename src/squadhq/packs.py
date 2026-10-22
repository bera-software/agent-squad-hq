"""Stack packs: ``packs/<id>/pack.yml`` loader, validator and CLI.

    python -m squadhq.packs list
    python -m squadhq.packs validate
    python -m squadhq.packs files --repo <owner/name> --role <implement|review|debt|design>
    python -m squadhq.packs ci-plan --repo <owner/name>
    python -m squadhq.packs tools --repo <owner/name>
    python -m squadhq.packs risk-globs --repo <owner/name>
    python -m squadhq.packs detect <path-to-checkout>

Projects (and their ``packs`` / ``pack_roots``) come from the rendered
``config/projects.json`` under the hub root (``SQUADHQ_HOME``).

``ci-plan`` prints a JSON array, one element per selected pack::

    {"pack": "node-next", "name": "Node / Next.js", "root": "web",
     "setup": {"kind": "node", "version": "20"},
     "install": "npm ci", "build": "npm run build", "test": "npm test",
     "coverage": {"format": "lcov", "path": "web/coverage/lcov.info", "min_overall": 60, "min_changed": 70},
     "paths": ["web/**/*.ts", ...]}

``arch`` (``{"tool", "config", "command", "snippet"}``) and ``lint``
(``{"command", "configs", "requires_config", "snippet", "snippet_path"}``) are
``null`` when the pack has none; ``quality`` is the project's
``quality`` policy (``arch``/``lint``/``changed_coverage``, defaults filled
in). With ``--checkout DIR`` every entry also gets ``gates`` (which arch and
lint gates apply to that checkout; see :mod:`squadhq.quality.gates`).

``paths`` and ``coverage.path`` are repo-relative (pack root applied);
``arch.config``, ``lint.configs`` and ``lint.snippet_path`` are relative to
the entry's ``root``.
``risk-globs`` prints the packs' ``risk_paths`` verbatim (repo-relative, NOT
prefixed with the root) so the risk floor errs broad.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

from squadhq.paths import hub_root
from squadhq.quality import gates as quality_gates

ROLES = ("implement", "review", "debt", "design")
SETUP_KINDS = ("java", "node", "python", "go", "dotnet", "rust", "ruby", "php", "none")
COVERAGE_FORMATS = ("jacoco", "lcov", "cobertura", "none")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\(.+\))?$")
_PACK_KEYS = {"id", "name", "description", "detect", "paths", "root", "ci", "risk_paths", "tools", "version", "arch"}
_ARCH_KEYS = {"tool", "config", "command", "local", "snippet", "snippet_path", "requires", "installed"}
_LINT_KEYS = {"command", "local", "configs", "requires_config", "snippet", "snippet_path"}


class PackError(ValueError):
    pass


@dataclass
class Coverage:
    format: str = "none"
    path: str = ""
    min_overall: int = 0
    min_changed: int = 0


@dataclass
class Arch:
    """Architecture gate: ``command`` runs when ``config`` (a root-relative
    path or glob) exists in the project. ``snippet`` is a starter config under
    ``packs/<id>/snippets/``; onboarding seeds it at ``snippet_path`` (default:
    ``config`` when it is a plain path). ``requires`` says what the project
    must add for ``command`` to work (shown in the onboarding PR); ``installed``
    is a marker (``<glob>`` or ``<glob>:<text>``) proving it was added, and
    under ``quality.arch: auto`` the gate is skipped until it matches. ``local``
    is the form an agent runs before pushing (default: ``command``); it must
    stay within the pack's ``tools``."""
    tool: str = ""
    config: str = ""
    command: str = ""
    local: str = ""
    snippet: str = ""
    snippet_path: str = ""
    requires: str = ""
    installed: str = ""


@dataclass
class Lint:
    """Lint/format gate, separate from ``build``. ``configs`` are config
    markers (``<glob>`` or ``<glob>:<text>``); ``requires_config: false`` means
    the linter runs on its defaults, so the gate always applies. ``local`` as
    for :class:`Arch`."""
    command: str = ""
    local: str = ""
    configs: list[str] = field(default_factory=list)
    requires_config: bool = True
    snippet: str = ""
    snippet_path: str = ""


@dataclass
class Ci:
    kind: str = "none"
    version: str = ""
    install: str = ""
    build: str = ""
    test: str = ""
    coverage: Coverage = field(default_factory=Coverage)
    lint: Lint | None = None


@dataclass
class Pack:
    id: str
    name: str
    dir: str
    detect_files: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    root: str = "."
    ci: Ci = field(default_factory=Ci)
    risk_paths: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    arch: Arch | None = None


def _strs(d: dict[str, Any], key: str, where: str, errors: list[str]) -> list[str]:
    v = d.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        errors.append(f"{where}.{key}: expected a list of non-empty strings")
        return []
    return list(v)


def _s(d: dict[str, Any], key: str, where: str, errors: list[str], default: str = "") -> str:
    v = d.get(key, default)
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if not isinstance(v, str):
        errors.append(f"{where}.{key}: expected a string, got {type(v).__name__}")
        return default
    return v


def _rel_path(value: str, where: str, errors: list[str], glob_ok: bool = False) -> None:
    if value.startswith("/") or ".." in value.split("/") or "\\" in value:
        errors.append(f"{where}: {value!r} must be a relative path inside the project")
    elif not glob_ok and quality_gates.has_glob(value):
        errors.append(f"{where}: {value!r} must be a plain path, not a glob")


def _snippet(d: dict[str, Any], where: str, pack_dir: str, errors: list[str]) -> str:
    name = _s(d, "snippet", where, errors)
    if name and (name != os.path.basename(name) or name in (".", "..")):
        errors.append(f"{where}.snippet: {name!r} must be a file name under snippets/")
    elif name and not os.path.isfile(os.path.join(pack_dir, "snippets", name)):
        errors.append(f"{where}.snippet: snippets/{name} does not exist")
    return name


def _parse_arch(raw: Any, where: str, pack_dir: str, errors: list[str]) -> Arch | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a mapping with tool, config, command")
        return None
    for k in raw:
        if k not in _ARCH_KEYS:
            errors.append(f"{where}.{k}: unknown key (allowed: {', '.join(sorted(_ARCH_KEYS))})")
    a = Arch(tool=_s(raw, "tool", where, errors), config=_s(raw, "config", where, errors),
             command=_s(raw, "command", where, errors), local=_s(raw, "local", where, errors),
             snippet=_snippet(raw, where, pack_dir, errors),
             snippet_path=_s(raw, "snippet_path", where, errors), requires=_s(raw, "requires", where, errors),
             installed=_s(raw, "installed", where, errors))
    a.local = a.local or a.command
    for k in ("tool", "config", "command"):
        if not getattr(a, k):
            errors.append(f"{where}.{k}: is required")
    if a.config:
        _rel_path(a.config, f"{where}.config", errors, glob_ok=True)
    if a.snippet_path:
        _rel_path(a.snippet_path, f"{where}.snippet_path", errors)
    elif a.snippet and a.config and not quality_gates.has_glob(a.config):
        a.snippet_path = a.config
    return a


def _parse_lint(raw: Any, where: str, pack_dir: str, errors: list[str]) -> Lint | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a mapping with command, configs")
        return None
    for k in raw:
        if k not in _LINT_KEYS:
            errors.append(f"{where}.{k}: unknown key (allowed: {', '.join(sorted(_LINT_KEYS))})")
    rc = raw.get("requires_config", True)
    if not isinstance(rc, bool):
        errors.append(f"{where}.requires_config: expected true or false")
        rc = True
    lint = Lint(command=_s(raw, "command", where, errors), local=_s(raw, "local", where, errors),
                configs=_strs(raw, "configs", where, errors),
                requires_config=rc, snippet=_snippet(raw, where, pack_dir, errors),
                snippet_path=_s(raw, "snippet_path", where, errors))
    if not lint.command:
        errors.append(f"{where}.command: is required")
    lint.local = lint.local or lint.command
    if lint.requires_config and not lint.configs:
        errors.append(f"{where}.configs: required unless requires_config is false")
    for c in lint.configs:
        _rel_path(c.partition(":")[0], f"{where}.configs", errors, glob_ok=True)
    if lint.snippet and not lint.snippet_path:
        lint.snippet_path = lint.snippet
    if lint.snippet_path:
        _rel_path(lint.snippet_path, f"{where}.snippet_path", errors)
    return lint


def parse_pack(data: Any, pack_dir: str) -> tuple[Pack | None, list[str]]:
    dirname = os.path.basename(os.path.normpath(pack_dir))
    where = f"packs/{dirname}/pack.yml"
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, [f"{where}: expected a mapping"]
    for k in data:
        if k not in _PACK_KEYS:
            errors.append(f"{where}.{k}: unknown key (allowed: {', '.join(sorted(_PACK_KEYS))})")
    pid = _s(data, "id", where, errors)
    if not pid:
        errors.append(f"{where}.id: is required")
    elif not _ID_RE.match(pid):
        errors.append(f"{where}.id: {pid!r} must be lowercase letters, digits and dashes")
    elif pid != dirname:
        errors.append(f"{where}.id: {pid!r} must equal its directory name {dirname!r}")
    detect = data.get("detect") or {}
    if not isinstance(detect, dict):
        errors.append(f"{where}.detect: expected a mapping with `files`")
        detect = {}
    root = _s(data, "root", where, errors, ".").rstrip("/") or "."
    if root.startswith("/") or ".." in root.split("/"):
        errors.append(f"{where}.root: {root!r} must be a relative path inside the repo")
    ci_raw = data.get("ci") or {}
    if not isinstance(ci_raw, dict):
        errors.append(f"{where}.ci: expected a mapping")
        ci_raw = {}
    setup = ci_raw.get("setup") or {}
    if not isinstance(setup, dict):
        errors.append(f"{where}.ci.setup: expected a mapping with `kind`")
        setup = {}
    kind = _s(setup, "kind", f"{where}.ci.setup", errors, "none")
    if kind not in SETUP_KINDS:
        errors.append(f"{where}.ci.setup.kind: {kind!r} not one of {', '.join(SETUP_KINDS)}")
    cov_raw = ci_raw.get("coverage") or {}
    if not isinstance(cov_raw, dict):
        errors.append(f"{where}.ci.coverage: expected a mapping")
        cov_raw = {}
    cov = Coverage(format=_s(cov_raw, "format", f"{where}.ci.coverage", errors, "none"),
                   path=_s(cov_raw, "path", f"{where}.ci.coverage", errors))
    if cov.format not in COVERAGE_FORMATS:
        errors.append(f"{where}.ci.coverage.format: {cov.format!r} not one of {', '.join(COVERAGE_FORMATS)}")
    if cov.format != "none" and not cov.path:
        errors.append(f"{where}.ci.coverage.path: required when format is {cov.format}")
    for k in ("min_overall", "min_changed"):
        v = cov_raw.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
            errors.append(f"{where}.ci.coverage.{k}: expected a number 0-100, got {v!r}")
        else:
            setattr(cov, k, int(v))
    lint = _parse_lint(ci_raw.get("lint"), f"{where}.ci.lint", pack_dir, errors)
    arch = _parse_arch(data.get("arch"), f"{where}.arch", pack_dir, errors)
    tools = _strs(data, "tools", where, errors)
    for t in tools:
        if not _TOOL_RE.match(t):
            errors.append(f"{where}.tools: {t!r} is not an allowedTools entry (e.g. \"Bash(npm *)\")")
    pack = Pack(
        id=pid or dirname, name=_s(data, "name", where, errors, pid or dirname), dir=pack_dir,
        detect_files=_strs(detect, "files", f"{where}.detect", errors),
        paths=_strs(data, "paths", where, errors), root=root,
        ci=Ci(kind=kind, version=_s(setup, "version", f"{where}.ci.setup", errors),
              install=_s(ci_raw, "install", f"{where}.ci", errors), build=_s(ci_raw, "build", f"{where}.ci", errors),
              test=_s(ci_raw, "test", f"{where}.ci", errors), coverage=cov, lint=lint),
        risk_paths=_strs(data, "risk_paths", where, errors), tools=tools, arch=arch,
    )
    if not pack.paths:
        errors.append(f"{where}.paths: at least one change-detection glob is required")
    if kind != "none" and not (pack.ci.build or pack.ci.test):
        errors.append(f"{where}.ci: needs `build` or `test` when setup.kind is {kind}")
    return (None if errors else pack), errors


def packs_dir(root: str | None = None) -> str:
    return os.path.join(root or hub_root(), "packs")


def load_all(root: str | None = None) -> tuple[dict[str, Pack], list[str]]:
    base = packs_dir(root)
    out: dict[str, Pack] = {}
    errors: list[str] = []
    if not os.path.isdir(base):
        return out, errors
    for name in sorted(os.listdir(base)):
        mf = os.path.join(base, name, "pack.yml")
        if not os.path.isfile(mf):
            continue
        try:
            with open(mf, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"packs/{name}/pack.yml: YAML error: {e}")
            continue
        pack, errs = parse_pack(data, os.path.join(base, name))
        errors.extend(errs)
        if pack:
            out[pack.id] = pack
    return out, errors


def load_pack(pack_id: str, root: str | None = None) -> Pack:
    d = os.path.join(packs_dir(root), pack_id)
    mf = os.path.join(d, "pack.yml")
    if not os.path.isfile(mf):
        raise PackError(f"pack {pack_id!r} not found ({mf})")
    with open(mf, encoding="utf-8") as f:
        pack, errs = parse_pack(yaml.safe_load(f), d)
    if pack is None:
        raise PackError("; ".join(errs))
    return pack


def find_project(repo: str, root: str | None = None) -> dict[str, Any]:
    path = os.path.join(root or hub_root(), "config", "projects.json")
    with open(path, encoding="utf-8") as f:
        projects = json.load(f).get("projects", [])
    for p in projects:
        if str(p.get("repo", "")).lower() == repo.lower():
            return dict(p)
    raise PackError(f"{repo} is not registered in config/projects.json")


def project_packs(repo: str, root: str | None = None) -> list[tuple[Pack, str]]:
    """``(pack, effective_root)`` for every pack a project selects, in order.
    Unknown pack ids raise :class:`PackError`."""
    proj = find_project(repo, root)
    roots = proj.get("pack_roots") or {}
    out: list[tuple[Pack, str]] = []
    for pid in proj.get("packs") or []:
        pack = load_pack(pid, root)
        out.append((pack, str(roots.get(pid) or pack.root).rstrip("/") or "."))
    return out


def _under(root: str, glob: str) -> str:
    return glob if root in ("", ".") else f"{root}/{glob}"


def prompt_files(repo: str, role: str, root: str | None = None) -> list[str]:
    if role not in ROLES:
        raise PackError(f"unknown role {role!r} (one of {', '.join(ROLES)})")
    base = root or hub_root()
    out = []
    for pack, _r in project_packs(repo, root):
        rel = f"packs/{pack.id}/{role}.md"
        if os.path.isfile(os.path.join(base, rel)):
            out.append(rel)
    return out


def project_quality(repo: str, root: str | None = None) -> dict[str, str]:
    """The project's ``quality`` policy from projects.json, defaults filled in."""
    return quality_gates.quality_policy(find_project(repo, root).get("quality"))


def ci_plan(repo: str, root: str | None = None, checkout: str | None = None) -> list[dict[str, Any]]:
    plan = []
    quality = project_quality(repo, root)
    for pack, r in project_packs(repo, root):
        c = pack.ci
        a = pack.arch
        entry: dict[str, Any] = {
            "pack": pack.id, "name": pack.name, "root": r,
            "setup": {"kind": c.kind, "version": c.version},
            "install": c.install, "build": c.build, "test": c.test,
            "coverage": {"format": c.coverage.format,
                         "path": _under(r, c.coverage.path) if c.coverage.path else "",
                         "min_overall": c.coverage.min_overall, "min_changed": c.coverage.min_changed},
            "paths": [_under(r, g) for g in pack.paths],
            "arch": ({"tool": a.tool, "config": a.config, "command": a.command, "snippet": a.snippet,
                      "installed": a.installed, "requires": a.requires}
                     if a else None),
            "lint": ({"command": c.lint.command, "configs": list(c.lint.configs),
                      "requires_config": c.lint.requires_config, "snippet": c.lint.snippet,
                      "snippet_path": c.lint.snippet_path} if c.lint else None),
            "quality": dict(quality),
        }
        if checkout is not None:
            entry["gates"] = quality_gates.probe(entry, checkout)
        plan.append(entry)
    return plan


def _uniq(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        seen.setdefault(i, None)
    return list(seen)


def tools(repo: str, root: str | None = None) -> list[str]:
    return _uniq([t for pack, _r in project_packs(repo, root) for t in pack.tools])


def gate_commands(repo: str, root: str | None = None) -> list[str]:
    """``- <gate> (<pack>...): <local command>`` lines for the agent prompt:
    the arch and lint checks to run locally before pushing."""
    quality = project_quality(repo, root)
    out = []
    for pack, r in project_packs(repo, root):
        if pack.arch and quality["arch"] != "off":
            out.append(f"- arch ({pack.id}, {pack.arch.tool}; in {r}; when {pack.arch.config} exists): "
                       f"{pack.arch.local}")
        if pack.ci.lint and quality["lint"] != "off":
            out.append(f"- lint ({pack.id}; in {r}): {pack.ci.lint.local}")
    return out


def risk_globs(repo: str, root: str | None = None) -> list[str]:
    return _uniq([g for pack, _r in project_packs(repo, root) for g in pack.risk_paths])


def detect(checkout: str, root: str | None = None, depth: int = 1) -> list[dict[str, str]]:
    """Propose packs for a checkout: a pack matches when any ``detect.files``
    pattern matches a file at the checkout root or up to ``depth`` directory
    levels below it (``web/package.json`` proposes root ``web``)."""
    packs, _errs = load_all(root)
    candidates: list[tuple[str, list[str]]] = []  # (relative dir, file names)
    for cur, dirs, files in os.walk(checkout):
        rel = os.path.relpath(cur, checkout)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "vendor", "dist",
                                                                                  "build", "target", "venv"))
        if level >= depth:
            dirs[:] = []
        candidates.append((rel.replace(os.sep, "/"), files))
    out: list[dict[str, str]] = []
    for pack in packs.values():
        for rel, files in candidates:
            hit = next((f for pat in pack.detect_files for f in sorted(files) if fnmatch.fnmatch(f, pat)), None)
            if hit:
                out.append({"pack": pack.id, "root": rel, "matched": hit if rel == "." else f"{rel}/{hit}"})
                break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="squadhq packs", description="Stack pack queries. Projects and their packs "
                                 "come from config/projects.json under the hub root (SQUADHQ_HOME).")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("list", help="list packs (id, name, default root)")
    sub.add_parser("validate", help="validate every packs/*/pack.yml")
    f = sub.add_parser("files", help="pack prompt files to append for a role (hub-relative paths)")
    f.add_argument("--repo", required=True, help="project repo, owner/name")
    f.add_argument("--role", required=True, choices=ROLES)
    for name, help_ in (("ci-plan", "JSON array, one CI job per selected pack (setup, build, test, coverage, "
                                    "lint, arch, paths)"),
                        ("tools", "extra allowedTools entries for coding agents, one per line"),
                        ("risk-globs", "repo-relative globs that force risk >= high, one per line"),
                        ("gates", "the arch and lint commands an agent should run locally, one per line")):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("--repo", required=True, help="project repo, owner/name")
        if name == "ci-plan":
            sp.add_argument("--checkout", default=None,
                            help="project checkout: add `gates` (which arch/lint gates apply) to each entry")
    d = sub.add_parser("detect", help="propose packs for a local checkout (JSON)")
    d.add_argument("path", help="path to the project checkout")
    d.add_argument("--depth", type=int, default=1, help="directory levels below the root to search (default 1)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            packs, errs = load_all()
            for p in packs.values():
                print(f"{p.id}\t{p.name}\troot={p.root}")
            for e in errs:
                print(e, file=sys.stderr)
            return 1 if errs else 0
        if a.cmd == "validate":
            packs, errs = load_all()
            for e in errs:
                print(f"error: {e}", file=sys.stderr)
            print(f"{len(packs)} pack(s) valid, {len(errs)} error(s)")
            return 1 if errs else 0
        if a.cmd == "files":
            for rel in prompt_files(a.repo, a.role):
                print(rel)
        elif a.cmd == "ci-plan":
            print(json.dumps(ci_plan(a.repo, checkout=a.checkout)))
        elif a.cmd == "gates":
            for line in gate_commands(a.repo):
                print(line)
        elif a.cmd == "tools":
            for t in tools(a.repo):
                print(t)
        elif a.cmd == "risk-globs":
            for g in risk_globs(a.repo):
                print(g)
        elif a.cmd == "detect":
            print(json.dumps(detect(a.path, depth=a.depth), indent=2))
    except (PackError, OSError, json.JSONDecodeError) as e:
        print(f"squadhq.packs: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
