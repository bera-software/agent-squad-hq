#!/usr/bin/env python3
"""Build the stack-CI matrix for a project checkout.

    python3 plan.py --repo owner/name [--changed FILE] [--workdir DIR]

Source of truth is ``python -P -m squadhq.packs ci-plan --repo <repo>
--checkout <workdir>`` (a JSON array of ``{pack, root, setup:{kind,version},
install, build, test, coverage:{format,path,min_overall,min_changed}, arch,
lint, quality, gates}``). When the packs module is unavailable or returns
nothing, the common toolchains are detected from the tree (no arch or lint
gates). Entries whose root has no changed file are dropped (fail-open when the
changed-file list is missing). Prints ``{"include": [...], "count": N,
"source": ..., "arch": "true|false", "lint": "true|false",
"changed_coverage": "enforce|report|off"}`` — the last three tell ci-gate which
quality sub-checks it must require.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

SKIP_DIRS = {".git", ".squadhq", "node_modules", ".venv", "venv", "vendor", "dist", "build", "target"}
# A change here can affect every stack, so it keeps every entry.
GLOBAL_FILES = {".tool-versions", ".nvmrc", ".node-version", ".python-version", ".java-version", "Makefile"}

NODE_SCRIPTS = (
    'for s in lint typecheck test build; do '
    'if node -e "process.exit((require(\'./package.json\').scripts||{})[process.argv[1]]?0:1)" "$s"; '
    'then {pm} run "$s"; fi; done'
)


NO_GATES = {"arch": "skip", "arch_reason": "", "lint": "skip", "lint_reason": "", "lint_default": {"src": "", "dst": ""}}


def packs_plan(repo, workdir="."):
    try:
        r = subprocess.run([sys.executable, "-P", "-m", "squadhq.packs", "ci-plan", "--repo", repo,
                            "--checkout", workdir], capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"packs ci-plan unavailable: {e!r}", file=sys.stderr)
        return None
    if r.returncode != 0 or not r.stdout.strip():
        print(f"packs ci-plan returned nothing (rc={r.returncode}): {r.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        plan = json.loads(r.stdout)
    except ValueError:
        print("packs ci-plan output is not JSON", file=sys.stderr)
        return None
    return plan if isinstance(plan, list) and plan else None


def candidate_dirs(base):
    yield "."
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if os.path.isdir(p) and name not in SKIP_DIRS and not name.startswith("."):
            yield name


def entry(pack, root, kind, version, install, build, test="", cov_format="none", cov_path=""):
    # Coverage paths are repo-relative, like squadhq.packs ci-plan emits them.
    if cov_path and root != ".":
        cov_path = f"{root}/{cov_path}"
    return {
        "pack": pack, "root": root, "setup": {"kind": kind, "version": version},
        "install": install, "build": build, "test": test,
        "coverage": {"format": cov_format, "path": cov_path, "min_overall": 0, "min_changed": 0},
        "arch": None, "lint": None, "quality": {}, "gates": dict(NO_GATES),
    }


def gate_outputs(plan):
    """Which quality sub-checks ci-gate must require (mirrors squadhq.quality.gates.outputs)."""
    gates = [e.get("gates") or {} for e in plan]
    modes = []
    for e in plan:
        cov = e.get("coverage") or {}
        if (cov.get("format") or "none") == "none":
            continue
        mode = (e.get("quality") or {}).get("changed_coverage") or "report"
        if mode == "enforce" and int(cov.get("min_changed") or 0) <= 0:
            mode = "report"
        modes.append(mode)
    return {
        "arch": str(any(g.get("arch") in ("run", "missing") for g in gates)).lower(),
        "lint": str(any(g.get("lint") in ("run", "default") for g in gates)).lower(),
        "changed_coverage": "enforce" if "enforce" in modes else ("report" if "report" in modes else "off"),
    }


def detect(base):
    found = []
    claimed = set()
    for d in candidate_dirs(base):
        path = os.path.join(base, d)
        has = lambda f, path=path: os.path.exists(os.path.join(path, f))  # noqa: E731
        if has("gradlew") or has("build.gradle") or has("build.gradle.kts"):
            gw = "./gradlew" if has("gradlew") else "gradle"
            found.append(entry("gradle", d, "java", os.environ.get("JAVA_VERSION", "21"), "",
                               f"{gw} build --continue", "", "jacoco",
                               "build/reports/jacoco/test/jacocoTestReport.xml"))
            claimed.add(d)
        elif has("pom.xml"):
            mvn = "./mvnw" if has("mvnw") else "mvn"
            found.append(entry("maven", d, "java", os.environ.get("JAVA_VERSION", "21"), "",
                               f"{mvn} -B verify", "", "jacoco", "target/site/jacoco/jacoco.xml"))
            claimed.add(d)
        if has("package.json"):
            if has("pnpm-lock.yaml"):
                pm, install = "pnpm", "pnpm install --frozen-lockfile"
            elif has("yarn.lock"):
                pm = "yarn"
                install = "yarn install --immutable" if has(".yarnrc.yml") else "yarn install --frozen-lockfile"
            else:
                pm, install = "npm", "npm ci" if has("package-lock.json") else "npm install"
            found.append(entry(f"node-{pm}", d, "node", os.environ.get("NODE_VERSION", "22"), install,
                               NODE_SCRIPTS.replace("{pm}", pm), "", "lcov", "coverage/lcov.info"))
        if has("go.mod"):
            found.append(entry("go", d, "go", "", "go mod download", "go build ./... && go vet ./...",
                               "go test ./..."))
        if (has("pyproject.toml") or has("setup.py") or has("requirements.txt")) and d not in claimed:
            install = ("python -P -m pip install -r requirements.txt" if has("requirements.txt") else "")
            if has("pyproject.toml") or has("setup.py"):
                install = (install + " && " if install else "") + "python -P -m pip install -e ."
            found.append(entry("python", d, "python", os.environ.get("PYTHON_VERSION", "3.12"), install,
                               "", "python -P -m pip install pytest && python -P -m pytest"))
    return found


def keep(plan, changed):
    if changed is None:
        return plan
    if any(os.path.basename(f) in GLOBAL_FILES for f in changed):
        return plan
    out = []
    for e in plan:
        globs = e.get("paths") or []
        root = (e.get("root") or ".").strip("/") or "."
        if globs:
            hit = any(fnmatch.fnmatch(f, g) for f in changed for g in globs)
        else:
            hit = root == "." or any(f == root or f.startswith(root + "/") for f in changed)
        if hit:
            out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--changed", help="file with one changed path per line")
    ap.add_argument("--workdir", default=".")
    args = ap.parse_args()

    plan = packs_plan(args.repo, args.workdir)
    source = "packs"
    if plan is None:
        plan = detect(args.workdir)
        source = "auto-detect"

    changed = None
    if args.changed and os.path.exists(args.changed):
        lines = [ln.strip() for ln in open(args.changed) if ln.strip()]
        changed = lines or None
    plan = keep(plan, changed)

    for e in plan:
        e["id"] = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{e.get('pack', 'stack')}-{e.get('root') or '.'}").strip("-")
        e.setdefault("setup", {"kind": "none", "version": ""})
        e.setdefault("coverage", {"format": "none", "path": "", "min_overall": 0, "min_changed": 0})
        e.setdefault("quality", {})
        e["gates"] = {**NO_GATES, **(e.get("gates") or {})}
    outputs = gate_outputs(plan)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### CI plan ({source}): {len(plan)} stack(s)\n")
            for e in plan:
                kind = (e.get("setup") or {}).get("kind", "none")
                g = e["gates"]
                f.write(f"- `{e['id']}` in `{e.get('root') or '.'}` ({kind}) — arch: {g['arch']}"
                        f"{' (' + g['arch_reason'] + ')' if g.get('arch_reason') else ''}; lint: {g['lint']}"
                        f"{' (' + g['lint_reason'] + ')' if g.get('lint_reason') else ''}\n")
            f.write(f"\nQuality sub-checks required by ci-gate: arch {outputs['arch']}, lint {outputs['lint']}, "
                    f"changed-coverage {outputs['changed_coverage']}\n")
    print(json.dumps({"include": plan, "count": len(plan), "source": source, **outputs}))


if __name__ == "__main__":
    main()
