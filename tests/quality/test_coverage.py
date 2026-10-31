"""squadhq.quality.coverage: report parsers, diff parsing, path matching and
the changed-lines verdict. Reports are untrusted data, so the safety limits
are tested too."""
import json
import os
import shutil
import subprocess

import pytest

from squadhq.quality import coverage as C

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(os.path.dirname(HERE), "fixtures", "quality")


def fx(name):
    return os.path.join(FIX, name)


DIFF = """\
diff --git a/web/src/lib/a.ts b/web/src/lib/a.ts
--- a/web/src/lib/a.ts
+++ b/web/src/lib/a.ts
@@ -2,0 +3,2 @@ export const a = 1;
+line3
+line4
diff --git a/web/src/new.ts b/web/src/new.ts
new file mode 100644
--- /dev/null
+++ b/web/src/new.ts
@@ -0,0 +1 @@
+export const n = 1;
diff --git a/web/src/gone.ts b/web/src/gone.ts
deleted file mode 100644
--- a/web/src/gone.ts
+++ /dev/null
@@ -1 +0,0 @@
-x
diff --git a/web/src/lib/a2.ts b/web/src/lib/a2.ts
--- a/web/src/lib/a2.ts
+++ b/web/src/lib/a2.ts
@@ -5 +5 @@
-old
+new
"""


def test_parse_lcov():
    r = C.load_report(fx("lcov.info"), "lcov")
    assert r["src/lib/a.ts"] == {1: 1, 2: 1, 3: 0, 4: 0}
    assert r["/home/runner/work/app/app/web/src/components/Button.tsx"] == {10: 3, 11: 0}
    # Malformed DA lines and records without SF are ignored.
    assert C.parse_lcov("DA:1,1\nSF:x\nDA:bad\nDA:2,1.0\nend_of_record\nDA:3,1\n") == {"x": {2: 1}}


def test_parse_cobertura_and_jacoco():
    cob = C.load_report(fx("cobertura.xml"), "cobertura")
    assert cob == {"app/services/orders.py": {1: 1, 5: 1, 6: 0, 7: 0}}
    jac = C.load_report(fx("jacoco.xml"), "jacoco")
    assert jac == {"com/acme/orders/OrderService.java": {10: 3, 11: 0, 12: 1}}


@pytest.mark.parametrize("fmt,data,needle", [
    ("cobertura", b"<report/>", "expected <coverage>"),
    ("jacoco", b"<coverage/>", "expected <report>"),
    ("jacoco", b"<report", "not well-formed"),
    ("cobertura", b'<!DOCTYPE c [<!ENTITY a "aaaa">]><coverage>&a;</coverage>', "entity"),
    ("xml", b"", "unsupported"),
])
def test_bad_reports(tmp_path, fmt, data, needle):
    p = tmp_path / "r"
    p.write_bytes(data)
    with pytest.raises(C.CoverageError, match=needle):
        C.load_report(str(p), fmt)


def test_read_capped_refuses_symlinks_large_and_missing(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"x" * 11)
    with pytest.raises(C.CoverageError, match="limit 10"):
        C.read_capped(str(big), limit=10)
    link = tmp_path / "link"
    link.symlink_to(big)
    with pytest.raises(C.CoverageError, match="not a regular file"):
        C.read_capped(str(link))
    with pytest.raises(C.CoverageError, match="cannot stat"):
        C.read_capped(str(tmp_path / "nope"))


def test_record_caps(monkeypatch):
    monkeypatch.setattr(C, "MAX_FILES", 1)
    with pytest.raises(C.CoverageError, match="more than 1 files"):
        C.parse_lcov("SF:a\nDA:1,1\nSF:b\nDA:1,1\n")
    monkeypatch.setattr(C, "MAX_FILES", 10)
    monkeypatch.setattr(C, "MAX_LINES", 2)
    with pytest.raises(C.CoverageError, match="line records"):
        C.parse_lcov("SF:a\nDA:1,1\nDA:2,1\nDA:3,1\n")


def test_parse_diff():
    assert C.parse_diff(DIFF) == {"web/src/lib/a.ts": {3, 4}, "web/src/new.ts": {1}, "web/src/lib/a2.ts": {5}}


def test_path_index_longest_suffix():
    idx = C.PathIndex(["src/lib/a.ts", "other/lib/a.ts", "/abs/web/src/components/Button.tsx", "x/a.ts"])
    assert idx.match("web/src/lib/a.ts") == "src/lib/a.ts"
    assert idx.match("web/src/components/Button.tsx") == "/abs/web/src/components/Button.tsx"
    assert idx.match("web/src/none.ts") is None
    assert idx.match("web/other/lib/x/a.ts") == "x/a.ts"  # the only whole-suffix candidate
    assert C.PathIndex(["pkg/one/a.ts"]).match("web/two/a.ts") is None
    assert idx.match("") is None
    jac = C.PathIndex(["com/acme/orders/OrderService.java"])
    assert jac.match("core/src/main/java/com/acme/orders/OrderService.java") == "com/acme/orders/OrderService.java"


def entry(id_, fmt, path, globs, mode="enforce", min_changed=70):
    return {"id": id_, "paths": globs, "coverage": {"format": fmt, "path": path, "min_changed": min_changed},
            "quality": {"changed_coverage": mode}}


def reports(tmp_path, **files):
    d = tmp_path / "reports"
    for entry_id, src in files.items():
        (d / f"coverage-{entry_id}").mkdir(parents=True)
        shutil.copy(fx(src), d / f"coverage-{entry_id}" / os.path.basename(src))
    return str(d)


def test_run_enforce_fails_below_minimum(tmp_path):
    d = reports(tmp_path, web="lcov.info")
    res = C.run({"include": [entry("web", "lcov", "web/coverage/lcov.info", ["web/**"])]}, d, C.parse_diff(DIFF))
    (r,) = res
    # a.ts lines 3,4 are measurable and uncovered; new.ts / a2.ts have no data.
    assert (r.covered, r.measurable, r.changed_files) == (0, 2, 3)
    assert r.enforced and r.blocking and r.pct == 0.0
    text = C.render(res)
    assert "❌ `web`: 0.0% of 2 changed line(s)" in text and "uncovered: 3, 4" in text


def test_run_report_mode_and_passing(tmp_path):
    d = reports(tmp_path, api="cobertura.xml", core="jacoco.xml")
    changed = {"api/app/services/orders.py": {5, 6, 99}, "core/src/main/java/com/acme/orders/OrderService.java": {10, 12},
               "README.md": {1}}
    res = C.run([entry("api", "cobertura", "api/coverage.xml", ["api/**/*.py"], mode="report"),
                 entry("core", "jacoco", "core/build/x.xml", ["**/*.java"])], d, changed)
    api, core = res
    assert (api.covered, api.measurable, api.mode) == (1, 2, "report") and not api.blocking and not api.ok
    assert core.ok and core.pct == 100.0 and not core.blocking
    text = C.render(res)
    assert "⚠ `api`: 50.0%" in text and "report only" in text and "✅ `core`: 100.0%" in text


def test_run_degraded_cases(tmp_path):
    d = reports(tmp_path, bad="lcov.info", nomin="lcov.info")
    (tmp_path / "reports" / "coverage-bad" / "lcov.info").write_bytes(b"<x/>")
    res = C.run([
        entry("missing", "lcov", "a/lcov.info", []),
        entry("bad", "cobertura", "lcov.info", []),
        entry("off", "lcov", "x", [], mode="off"),
        entry("none", "none", "", []),
        entry("nomin", "lcov", "lcov.info", [], min_changed=0),
        {"id": "weird", "coverage": {"format": "lcov", "min_changed": "x"}},
        "not-a-dict",
    ], d, {"zzz.ts": {1}})
    by = {r.id: r for r in res}
    assert set(by) == {"missing", "bad", "nomin", "weird"}
    assert by["missing"].error and not by["missing"].enforced
    assert "expected <coverage>" in by["bad"].error and not by["bad"].blocking
    assert by["weird"].minimum == 0 and by["weird"].mode == "report"
    text = C.render(res)
    assert "⚠ `missing`: no coverage report was uploaded (enforce → report only)" in text
    assert "no measurable changed lines" in text
    assert "No stack with a coverage report" in C.render([])


def test_find_report_single_file_fallback(tmp_path):
    d = tmp_path / "coverage-x"
    d.mkdir()
    (d / "other.xml").write_text("")
    assert C.find_report(str(tmp_path), "x", "coverage.xml") == str(d / "other.xml")
    (d / "second.xml").write_text("")
    assert C.find_report(str(tmp_path), "x", "coverage.xml") is None
    assert C.find_report(str(tmp_path), "nope", "a") is None


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_main_against_a_real_merge_commit(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "src" / "lib").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "lib" / "a.ts").write_text("1\n2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feature")
    (repo / "src" / "lib" / "a.ts").write_text("1\n2\n3\n4\n")
    git(repo, "commit", "-qam", "feature")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    d = reports(tmp_path, web="lcov.info")
    summary = tmp_path / "summary.md"
    matrix = json.dumps({"include": [entry("web", "lcov", "coverage/lcov.info", ["src/**"])]})
    assert C.main(["changed", "--reports", d, "--repo-dir", str(repo), "--matrix", matrix,
                   "--summary", str(summary)]) == 1
    assert "0.0% of 2 changed line(s)" in summary.read_text()
    assert "::error::changed-lines coverage below the minimum for: web" in capsys.readouterr().out
    lenient = json.dumps({"include": [entry("web", "lcov", "coverage/lcov.info", ["src/**"], mode="report")]})
    assert C.main(["changed", "--reports", d, "--repo-dir", str(repo), "--matrix", lenient, "--summary", ""]) == 0


def test_main_diff_file_env_matrix_and_errors(tmp_path, monkeypatch, capsys):
    d = reports(tmp_path, web="lcov.info")
    diff = tmp_path / "d.diff"
    diff.write_text(DIFF.replace("+line3\n+line4\n", "+line3\n+line4\n").replace("@@ -2,0 +3,2 @@", "@@ -0,0 +1,2 @@"))
    monkeypatch.setenv("MATRIX", json.dumps([entry("web", "lcov", "lcov.info", [])]))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert C.main(["changed", "--reports", d, "--diff-file", str(diff)]) == 0
    assert "100.0% of 2 changed line(s)" in capsys.readouterr().out
    assert C.main(["changed", "--reports", d, "--matrix", "{not json"]) == 2
    assert C.main(["changed", "--reports", d, "--repo-dir", str(tmp_path), "--base=-evil"]) == 2
    assert C.main(["changed", "--reports", d, "--repo-dir", str(tmp_path), "--base", "HEAD~1"]) == 2
    assert "git diff" in capsys.readouterr().err


def test_diff_size_cap(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "1")
    (repo / "f").write_text("a\nb\n" * 50)
    git(repo, "commit", "-qam", "2")
    monkeypatch.setattr(C, "MAX_DIFF_BYTES", 10)
    with pytest.raises(C.CoverageError, match="larger than"):
        C.git_diff(str(repo), "HEAD^1", "HEAD")


# ---------------------------------------------------------------- overall

JACOCO_EMPTY = b'<?xml version="1.0"?><report name="root"><counter type="LINE" missed="0" covered="0"/></report>'


def _jacoco(pkg, name, lines):
    rows = "".join(f'<line nr="{nr}" mi="{0 if ci else 1}" ci="{ci}" mb="0" cb="0"/>' for nr, ci in lines)
    return (f'<?xml version="1.0"?><report name="m"><package name="{pkg}">'
            f'<sourcefile name="{name}">{rows}</sourcefile></package></report>').encode()


REL = "build/reports/jacoco/test/jacocoTestReport.xml"


def _write(base, rel, data):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_overall_uses_the_configured_report_when_it_has_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, REL, _jacoco("a", "A.java", [(1, 1), (2, 0)]))
    _write(tmp_path, f"mod/{REL}", _jacoco("b", "B.java", [(1, 0)]))
    used, report = C.resolve("jacoco", REL, ".")
    assert used == [REL] and C.line_counts(report) == (1, 2)


def test_overall_merges_module_reports_when_the_root_report_is_empty(tmp_path, monkeypatch):
    # A multi-module Gradle build: the root project has no code, each module has its own report.
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, REL, JACOCO_EMPTY)
    _write(tmp_path, f"domain/{REL}", _jacoco("com/x/domain", "Policy.java", [(1, 1), (2, 1), (3, 0)]))
    _write(tmp_path, f"app/{REL}", _jacoco("com/x/app", "Svc.java", [(1, 1)]))
    _write(tmp_path, f"node_modules/x/{REL}", _jacoco("junk", "J.java", [(1, 0)] * 1))
    _write(tmp_path, f"archunit/{REL}", JACOCO_EMPTY)
    out = tmp_path / "out"
    rc = C.overall("jacoco", REL, ".", 60, str(out))
    assert rc == 0  # 3 of 4 lines
    written = out / "jacocoTestReport.xml"
    back = C.parse_jacoco(written.read_bytes())
    assert back == {"com/x/domain/Policy.java": {1: 1, 2: 1, 3: 0}, "com/x/app/Svc.java": {1: 1}}
    assert C.overall("jacoco", REL, ".", 80, None) == 1


def test_overall_without_any_report_fails_only_with_a_minimum(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert C.overall("jacoco", REL, ".", 60, None) == 1
    assert C.overall("jacoco", REL, ".", 0, None) == 0


def test_overall_lcov_modules_under_a_pack_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "web/packages/ui/coverage/lcov.info", b"SF:src/a.ts\nDA:1,1\nDA:2,0\nend_of_record\n")
    _write(tmp_path, "web/packages/api/coverage/lcov.info", b"SF:src/b.ts\nDA:1,3\nend_of_record\n")
    out = tmp_path / "out"
    assert C.overall("lcov", "web/coverage/lcov.info", "web", 60, str(out)) == 0
    assert C.parse_lcov((out / "lcov.info").read_text()) == {"src/a.ts": {1: 1, 2: 0}, "src/b.ts": {1: 3}}


@pytest.mark.parametrize("fmt", ["lcov", "cobertura", "jacoco"])
def test_dump_round_trips(fmt):
    report = {"com/x/A.java": {1: 2, 5: 0}, "B.java": {3: 1}}
    data = C.dump(report, fmt)
    back = C.parse_lcov(data.decode()) if fmt == "lcov" else (
        C.parse_cobertura(data) if fmt == "cobertura" else C.parse_jacoco(data))
    assert back == report


def test_overall_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, f"m/{REL}", _jacoco("p", "A.java", [(1, 1)]))
    summary = tmp_path / "summary.md"
    rc = C.main(["overall", "--format", "jacoco", "--report", REL, "--min", "60", "--summary", str(summary)])
    assert rc == 0 and "100.0%" in summary.read_text()


def test_merged_jacoco_keeps_every_counter(tmp_path, monkeypatch):
    # The PR coverage comment reads INSTRUCTION counters per source file.
    monkeypatch.chdir(tmp_path)
    mod = (b'<?xml version="1.0"?><report name="m"><package name="p{n}"><sourcefile name="A{n}.java">'
           b'<line nr="1" mi="0" ci="3" mb="0" cb="0"/><counter type="INSTRUCTION" missed="1" covered="3"/>'
           b'<counter type="LINE" missed="0" covered="1"/></sourcefile></package>'
           b'<counter type="INSTRUCTION" missed="1" covered="3"/><counter type="LINE" missed="0" covered="1"/></report>')
    _write(tmp_path, REL, JACOCO_EMPTY)
    _write(tmp_path, f"a/{REL}", mod.replace(b"{n}", b"1"))
    _write(tmp_path, f"b/{REL}", mod.replace(b"{n}", b"2"))
    out = tmp_path / "out"
    assert C.overall("jacoco", REL, ".", 60, str(out)) == 0
    root = C._safe_xml((out / "jacocoTestReport.xml").read_bytes())
    files = [sf for sf in root.iter("sourcefile")]
    assert {sf.get("name") for sf in files} == {"A1.java", "A2.java"}
    assert all(sf.find("counter[@type='INSTRUCTION']") is not None for sf in files)
    assert root.find("counter[@type='INSTRUCTION']").attrib == {"type": "INSTRUCTION", "missed": "2", "covered": "6"}
