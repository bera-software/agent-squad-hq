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
