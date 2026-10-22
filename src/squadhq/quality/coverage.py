"""Changed-lines coverage: coverage reports + a git diff -> a gate verdict.

    python3 -I coverage.py changed --reports DIR [--repo-dir .] [--base REF] [--head REF]
                                   [--diff-file FILE] [--matrix JSON | env MATRIX]
    python -m squadhq.quality.coverage changed ...   (same, from an installed hub)

Standard library only: the router copies this file into the quality-kit
artifact and runs it in the ``changed-coverage`` job, which holds no token
and never executes PR code. Coverage reports are produced by PR code, so they
are parsed as untrusted data: size-capped, symlinks refused, XML with entity
declarations refused, record counts capped.

Formats: ``lcov`` (``SF:``/``DA:``), ``cobertura`` (``<class filename>`` /
``<line number hits>``) and ``jacoco`` (``<package name>/<sourcefile name>`` /
``<line nr ci>``). Report paths are matched to repo paths by the longest
common path suffix, so absolute paths, pack roots and Java package paths all
resolve.

Per CI-plan entry (``--matrix``: the ci-plan matrix JSON, each entry carrying
``id``, ``paths``, ``coverage`` and ``quality.changed_coverage``):

* changed lines = lines added or modified by the diff in files matching the
  entry's ``paths`` globs;
* measurable changed lines = changed lines the report has data for (blank
  lines and comments carry none);
* the entry passes when it has no measurable changed lines or
  ``covered / measurable >= coverage.min_changed``.

``quality.changed_coverage``: ``enforce`` fails the run (exit 1) on a miss,
but only when ``min_changed > 0`` and the report was found; ``report`` (the
default) and every degraded case only write the summary; ``off`` skips the
entry. The diff defaults to ``HEAD^1..HEAD`` — on a ``pull_request`` checkout
HEAD is the merge commit the reports were built from, so its first parent is
the base and the line numbers match.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET  # noqa: S405 - guarded by _safe_xml (size cap, no DTD entities)
from dataclasses import dataclass, field
from typing import Any

MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_FILES = 200_000
MAX_LINES = 20_000_000
MAX_DIFF_BYTES = 32 * 1024 * 1024
FORMATS = ("lcov", "cobertura", "jacoco")
MODES = ("enforce", "report", "off")

# {path as written in the report: {line number: hits}}
Report = dict[str, dict[int, int]]


class CoverageError(ValueError):
    """The report is unreadable, too large, or not safe to parse."""


# ---------------------------------------------------------------- reading


def read_capped(path: str, limit: int = MAX_REPORT_BYTES) -> bytes:
    """Bytes of a regular file (never a symlink), refusing anything over ``limit``."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise CoverageError(f"cannot stat {path}: {e.strerror}") from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise CoverageError(f"{path} is not a regular file")
    if st.st_size > limit:
        raise CoverageError(f"{path} is {st.st_size} bytes (limit {limit})")
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise CoverageError(f"{path} grew past {limit} bytes while reading")
    return data


def _safe_xml(data: bytes) -> ET.Element:
    # JaCoCo and Cobertura reports carry a DOCTYPE with an external DTD
    # reference, which ElementTree never fetches. Entity declarations are the
    # expansion-bomb vector and have no place in a coverage report.
    if b"<!ENTITY" in data or b"<!entity" in data:
        raise CoverageError("XML entity declarations are not allowed in a coverage report")
    try:
        return ET.fromstring(data)  # noqa: S314 - size-capped, entity-free, never fetches external DTDs
    except ET.ParseError as e:
        raise CoverageError(f"not well-formed XML: {e}") from e


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _add(report: Report, path: str, line: int | None, hits: int | None, counter: list[int]) -> None:
    if not path or line is None or hits is None or line < 1:
        return
    lines = report.get(path)
    if lines is None:
        if len(report) >= MAX_FILES:
            raise CoverageError(f"more than {MAX_FILES} files in one report")
        lines = report[path] = {}
    counter[0] += 1
    if counter[0] > MAX_LINES:
        raise CoverageError(f"more than {MAX_LINES} line records in one report")
    lines[line] = max(lines.get(line, 0), max(hits, 0))


def parse_lcov(text: str) -> Report:
    report: Report = {}
    counter = [0]
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current = line[3:].strip()
        elif line.startswith("DA:") and current:
            parts = line[3:].split(",")
            if len(parts) >= 2:
                _add(report, current, _int(parts[0]), _int(parts[1].split(".")[0]), counter)
        elif line == "end_of_record":
            current = ""
    return report


def parse_cobertura(data: bytes) -> Report:
    root = _safe_xml(data)
    if root.tag != "coverage":
        raise CoverageError(f"root element is <{root.tag}>, expected <coverage>")
    report: Report = {}
    counter = [0]
    for cls in root.iter("class"):
        path = (cls.get("filename") or "").strip()
        lines = cls.find("lines")
        for ln in (lines.iter("line") if lines is not None else ()):
            _add(report, path, _int(ln.get("number")), _int(ln.get("hits")), counter)
    return report


def parse_jacoco(data: bytes) -> Report:
    root = _safe_xml(data)
    if root.tag != "report":
        raise CoverageError(f"root element is <{root.tag}>, expected <report>")
    report: Report = {}
    counter = [0]
    # <report> may nest <group>s (multi-module aggregates); packages sit at any depth.
    for pkg in root.iter("package"):
        pname = (pkg.get("name") or "").strip("/")
        for sf in pkg.findall("sourcefile"):
            name = (sf.get("name") or "").strip()
            path = f"{pname}/{name}" if pname else name
            for ln in sf.findall("line"):
                ci = _int(ln.get("ci"))
                _add(report, path, _int(ln.get("nr")), ci, counter)
    return report


def load_report(path: str, fmt: str) -> Report:
    if fmt not in FORMATS:
        raise CoverageError(f"unsupported coverage format {fmt!r}")
    data = read_capped(path)
    if fmt == "lcov":
        return parse_lcov(data.decode("utf-8", "replace"))
    if fmt == "cobertura":
        return parse_cobertura(data)
    return parse_jacoco(data)


# ---------------------------------------------------------------- diff

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> dict[str, set[int]]:
    """Added/modified line numbers (new side) per file from a ``--unified=0`` diff."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = target[2:] if target.startswith("b/") else target
                out.setdefault(current, set())
        elif line.startswith("@@") and current is not None:
            m = _HUNK_RE.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or "1")
                out[current].update(range(start, start + count))
    return {k: v for k, v in out.items() if v}


def git_diff(repo_dir: str, base: str, head: str) -> str:
    """Unified-0 diff between two refs. No external diff drivers, no
    textconv, no renames-as-copies: the checkout's content is data only."""
    for ref in (base, head):
        if not re.match(r"^[A-Za-z0-9_./^~@{}-]+$", ref) or ref.startswith("-"):
            raise CoverageError(f"invalid git ref {ref!r}")
    r = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
         "--unified=0", "--diff-filter=AMR", "-M", base, head, "--"],
        cwd=repo_dir, capture_output=True, timeout=300, check=False)
    if r.returncode != 0:
        raise CoverageError(f"git diff {base} {head} failed: {r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    if len(r.stdout) > MAX_DIFF_BYTES:
        raise CoverageError(f"diff larger than {MAX_DIFF_BYTES} bytes")
    return r.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- matching


def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]


class PathIndex:
    """Maps repo-relative paths to report paths: the report path with the
    longest common path suffix, where one path is a whole suffix of the other."""

    def __init__(self, report_paths: list[str]) -> None:
        self._by_name: dict[str, list[tuple[str, list[str]]]] = {}
        for p in sorted(report_paths):
            parts = _parts(p)
            if parts:
                self._by_name.setdefault(parts[-1], []).append((p, parts))

    def match(self, repo_path: str) -> str | None:
        want = _parts(repo_path)
        if not want:
            return None
        best, best_score = None, 0
        for p, parts in self._by_name.get(want[-1], []):
            score = 0
            for a, b in zip(reversed(want), reversed(parts), strict=False):
                if a != b:
                    break
                score += 1
            # One path must be a whole suffix of the other: "src/x.ts" for
            # "web/src/x.ts", or an absolute report path ending in the repo path.
            if score < min(len(want), len(parts)):
                continue
            if score > best_score:
                best, best_score = p, score
        return best


# ---------------------------------------------------------------- evaluation


@dataclass
class FileResult:
    path: str
    covered: int
    measurable: int
    missed_lines: list[int] = field(default_factory=list)


@dataclass
class EntryResult:
    id: str
    mode: str
    minimum: int
    report: str
    found: bool = False
    error: str = ""
    covered: int = 0
    measurable: int = 0
    changed_files: int = 0
    files: list[FileResult] = field(default_factory=list)

    @property
    def pct(self) -> float | None:
        return 100.0 * self.covered / self.measurable if self.measurable else None

    @property
    def ok(self) -> bool:
        pct = self.pct
        return pct is None or pct >= self.minimum

    @property
    def enforced(self) -> bool:
        return self.mode == "enforce" and self.minimum > 0 and self.found and not self.error

    @property
    def blocking(self) -> bool:
        return self.enforced and not self.ok


def evaluate(report: Report, changed: dict[str, set[int]], globs: list[str], result: EntryResult) -> EntryResult:
    idx = PathIndex(list(report))
    for path in sorted(changed):
        if globs and not any(fnmatch.fnmatch(path, g) for g in globs):
            continue
        result.changed_files += 1
        rp = idx.match(path)
        if rp is None:
            continue
        data = report[rp]
        lines = sorted(n for n in changed[path] if n in data)
        if not lines:
            continue
        covered = [n for n in lines if data[n] > 0]
        missed = [n for n in lines if data[n] <= 0]
        result.files.append(FileResult(path, len(covered), len(lines), missed))
        result.covered += len(covered)
        result.measurable += len(lines)
    return result


def _entries(matrix: Any) -> list[dict[str, Any]]:
    if isinstance(matrix, dict):
        matrix = matrix.get("include") or []
    return [e for e in matrix if isinstance(e, dict)] if isinstance(matrix, list) else []


def find_report(reports_dir: str, entry_id: str, report_path: str) -> str | None:
    """The report the stack-ci leg uploaded as artifact ``coverage-<id>``."""
    d = os.path.join(reports_dir, f"coverage-{entry_id}")
    if not os.path.isdir(d) or os.path.islink(d):
        return None
    want = os.path.basename(report_path)
    cand = os.path.join(d, want)
    if want and os.path.isfile(cand) and not os.path.islink(cand):
        return cand
    files = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    return os.path.join(d, files[0]) if len(files) == 1 else None


def run(matrix: Any, reports_dir: str, changed: dict[str, set[int]]) -> list[EntryResult]:
    results = []
    for e in _entries(matrix):
        cov = e.get("coverage") or {}
        fmt = str(cov.get("format") or "none")
        mode = str((e.get("quality") or {}).get("changed_coverage") or "report")
        if fmt not in FORMATS or mode not in MODES or mode == "off":
            continue
        try:
            minimum = int(cov.get("min_changed") or 0)
        except (TypeError, ValueError):
            minimum = 0
        res = EntryResult(id=str(e.get("id") or e.get("pack") or "stack"), mode=mode, minimum=minimum,
                          report=str(cov.get("path") or ""))
        path = find_report(reports_dir, res.id, res.report)
        if path is None:
            res.error = "no coverage report was uploaded"
            results.append(res)
            continue
        res.found = True
        try:
            report = load_report(path, fmt)
        except CoverageError as exc:
            res.error = str(exc)
            results.append(res)
            continue
        globs = [g for g in (e.get("paths") or []) if isinstance(g, str)]
        results.append(evaluate(report, changed, globs, res))
    return results


def _md(s: str) -> str:
    return s.replace("`", "'").replace("|", "\\|")


def render(results: list[EntryResult], max_files: int = 25) -> str:
    out = ["### Changed-lines coverage", ""]
    if not results:
        out.append("⏭ No stack with a coverage report applies to this change.")
        return "\n".join(out) + "\n"
    for r in results:
        label = {"enforce": "enforced" if r.enforced else "enforce → report only", "report": "report only"}[r.mode]
        if r.error:
            out.append(f"- ⚠ `{_md(r.id)}`: {_md(r.error)} ({label})")
            continue
        pct = r.pct
        if pct is None:
            out.append(f"- ✅ `{_md(r.id)}`: no measurable changed lines in {r.changed_files} changed file(s) "
                       f"({label})")
            continue
        mark = "✅" if r.ok else ("❌" if r.enforced else "⚠")
        out.append(f"- {mark} `{_md(r.id)}`: {pct:.1f}% of {r.measurable} changed line(s) covered "
                   f"(minimum {r.minimum}%, {label})")
        worst = sorted((f for f in r.files if f.missed_lines), key=lambda f: (-len(f.missed_lines), f.path))
        for f in worst[:max_files]:
            lines = ", ".join(str(n) for n in f.missed_lines[:20]) + (" …" if len(f.missed_lines) > 20 else "")
            out.append(f"  - `{_md(f.path)}` {f.covered}/{f.measurable} — uncovered: {lines}")
        if len(worst) > max_files:
            out.append(f"  - … {len(worst) - max_files} more file(s)")
    if not any(r.mode == "enforce" for r in results):
        out += ["", "_Report only: set `projects[].quality.changed_coverage: enforce` to make this a "
                "required ci-gate check._"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coverage.py", description="Changed-lines coverage gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("changed", help="evaluate every ci-plan entry against the diff")
    c.add_argument("--reports", required=True, help="directory holding coverage-<id>/ artifact folders")
    c.add_argument("--matrix", default=None, help="ci-plan matrix JSON (default: env MATRIX)")
    c.add_argument("--repo-dir", default=".", help="checkout to diff (data only)")
    c.add_argument("--base", default="HEAD^1")
    c.add_argument("--head", default="HEAD")
    c.add_argument("--diff-file", default=None, help="read a --unified=0 diff from this file instead of git")
    c.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY") or "",
                   help="append the markdown summary here (default: GITHUB_STEP_SUMMARY; stdout otherwise)")
    a = ap.parse_args(argv)
    try:
        matrix = json.loads(a.matrix if a.matrix is not None else os.environ.get("MATRIX") or "[]")
        if a.diff_file:
            diff = read_capped(a.diff_file, MAX_DIFF_BYTES).decode("utf-8", "replace")
        else:
            diff = git_diff(a.repo_dir, a.base, a.head)
    except (ValueError, OSError) as e:
        print(f"changed-coverage: {e}", file=sys.stderr)
        return 2
    results = run(matrix, a.reports, parse_diff(diff))
    text = render(results)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    blocking = [r.id for r in results if r.blocking]
    if blocking:
        print(f"::error::changed-lines coverage below the minimum for: {', '.join(blocking)}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
