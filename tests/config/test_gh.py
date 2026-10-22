import subprocess

from squadhq.config.gh import FakeGh, Gh, Result


def test_result_json():
    assert Result(0, '{"a": 1}').json() == {"a": 1}
    assert Result(0, "not json").json() is None
    assert Result(0, "").json() is None
    assert Result(0).ok and not Result(1).ok


def test_real_gh_runs_list_args_with_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = Gh("ghx", timeout=5).run(["api", "user"], input_text="x")
    assert r.ok and r.stdout == "out"
    assert seen["argv"] == ["ghx", "api", "user"]
    assert seen["kw"]["timeout"] == 5 and seen["kw"]["input"] == "x" and seen["kw"]["check"] is False


def test_real_gh_errors(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    assert Gh().run(["x"]).returncode == 127

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("gh", 1)

    monkeypatch.setattr(subprocess, "run", slow)
    r = Gh().run(["api", "x"], timeout=1)
    assert r.returncode == 124 and "timed out" in r.stderr


def test_available_and_authenticated(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: None)
    assert not Gh().available() and not Gh().authenticated()
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""))
    assert Gh().authenticated()


def test_fake_gh_longest_prefix_and_callable():
    g = FakeGh(routes={("api",): Result(0, "short"), ("api", "user"): lambda a, i: Result(0, f"long {i}")})
    assert g.run(["api", "user"], input_text="z").stdout == "long z"
    assert g.run(["api", "other"]).stdout == "short"
    assert not g.run(["label"]).ok
    assert g.calls[0] == ["api", "user"] and g.available()
