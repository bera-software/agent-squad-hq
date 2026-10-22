"""Tests for squadhq.stub_sync. No network: the `gh` seam is replaced by a
fake that answers from a dict of (method, path) -> (code, json)."""
import base64
import json
import os
import subprocess
from unittest import mock

import pytest

from squadhq import stub_sync as ss

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "onboard")
SHA = "0123456789abcdef0123456789abcdef01234567"
SUBS = {"__HUB_REPO__": "acme/squad-hub", "__HUB_REF__": SHA, "__HUB_TAG__": "v1.2.0"}


@pytest.fixture(autouse=True)
def hub(monkeypatch):
    monkeypatch.setenv("SQUADHQ_HOME", FIXTURE)
    monkeypatch.setenv("SQUADHQ_HUB_REPO", "acme/squad-hub")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("SQUADHQ_MODE", raising=False)
    monkeypatch.delenv("SQUADHQ_DEPLOYMENT", raising=False)
    monkeypatch.setenv("SQUADHQ_STUB_REF", "v1.2.0")


class FakeGH:
    def __init__(self, routes, prs=""):
        self.routes = routes
        self.prs = prs
        self.calls = []

    def __call__(self, args, input_text=None):
        self.calls.append((list(args), input_text))
        if args[0] == "api" and "/commits/" in args[1]:
            ok = args[1].endswith("/v1.2.0")
            return subprocess.CompletedProcess(args, 0 if ok else 1, SHA + "\n" if ok else "", "" if ok else "404")
        if args[0] == "api":
            key = (args[2], args[3])
            code, data = self.routes.get(key, (1, None))
            return subprocess.CompletedProcess(args, code, json.dumps(data) if data is not None else "",
                                               "" if code == 0 else f"HTTP 404 {key}")
        if args[:2] == ["pr", "list"]:
            return subprocess.CompletedProcess(args, 0, self.prs, "")
        if args[:2] == ["pr", "create"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/acme/demo/pull/9\n", "")
        raise AssertionError(args)

    def methods(self):
        return [(a[2], a[3]) for a, _ in self.calls if a[0] == "api"]


def content(text):
    return {"content": base64.b64encode(text.encode()).decode(), "sha": "s1"}


def test_render_substitutes_hub_placeholders():
    out = ss.render(SUBS)
    assert f"acme/squad-hub/.github/workflows/router.yml@{SHA}  # v1.2.0" in out
    assert "__" not in out
    assert "agent.yml@" + SHA in ss.render(SUBS, server=True)


def test_leftover_placeholder_is_an_error():
    with pytest.raises(ss.StubError, match="__HUB_REF__"):
        ss.render({"__HUB_REPO__": "a/b"})


def test_placeholders_resolve_ref_to_sha():
    fake = FakeGH({})
    with mock.patch.object(ss, "gh", fake):
        assert ss.placeholders() == SUBS
        assert fake.calls[0][0] == ["api", "repos/acme/squad-hub/commits/v1.2.0", "--jq", ".sha"]
        with pytest.raises(ss.StubError, match="cannot resolve"):
            ss.placeholders(ref="nope")


def test_non_sha_answer_is_rejected():
    def gh(args, input_text=None):
        return subprocess.CompletedProcess(args, 0, "main\n", "")
    with mock.patch.object(ss, "gh", gh), pytest.raises(ss.StubError):
        ss.resolve_ref("acme/squad-hub", "main")


def test_server_mode_switch(monkeypatch):
    assert not ss.server_mode()
    assert ss.server_mode(True)
    monkeypatch.setenv("SQUADHQ_DEPLOYMENT", "Server")
    assert ss.server_mode()
    monkeypatch.setenv("SQUADHQ_DEPLOYMENT", "actions")
    assert not ss.server_mode()
    monkeypatch.setenv("SQUADHQ_MODE", "worker")  # the server's own setting never flips Actions behaviour
    assert not ss.server_mode()
    monkeypatch.setenv("SQUADHQ_MODE", "Server")  # legacy spelling still honoured
    assert ss.server_mode()


def base_routes(have=None):
    r = {("GET", "repos/acme/demo"): (0, {"default_branch": "main"})}
    if have is not None:
        r[("GET", f"repos/acme/demo/contents/{ss.PATH}?ref=main")] = (0, content(have))
    return r


def test_in_sync():
    fake = FakeGH(base_routes(ss.render(SUBS)))
    with mock.patch.object(ss, "gh", fake):
        assert ss.sync("acme/demo", False, SUBS) == ("ok", f"| acme/demo | {ss.PATH}: in sync | — |")


def test_render_error_is_a_row_error():
    with mock.patch.object(ss, "gh", FakeGH({})):
        status, row = ss.sync("acme/demo", False, {"__HUB_REPO__": "x"})
    assert status == "error" and "rendering" in row
    with mock.patch.object(ss, "gh", FakeGH(base_routes(ss.render(SUBS)))):
        assert ss.sync("acme/demo", True)[0] == "ok"  # resolves placeholders itself


def test_out_of_date_dry_run_writes_nothing():
    fake = FakeGH(base_routes("old"))
    with mock.patch.object(ss, "gh", fake):
        status, row = ss.sync("acme/demo", True, SUBS)
    assert status == "ok" and "would open" in row
    assert all(m == "GET" for m, _ in fake.methods())


def test_creates_branch_and_pr():
    routes = base_routes(None)
    routes[("GET", "repos/acme/demo/git/ref/heads/main")] = (0, {"object": {"sha": "abc"}})
    routes[("POST", "repos/acme/demo/git/refs")] = (0, {})
    routes[("PUT", f"repos/acme/demo/contents/{ss.PATH}")] = (0, {})
    fake = FakeGH(routes)
    with mock.patch.object(ss, "gh", fake):
        status, row = ss.sync("acme/demo", False, SUBS)
    assert status == "ok" and "pull/9" in row
    put = next(json.loads(i) for a, i in fake.calls if a[:3] == ["api", "-X", "PUT"])
    assert put["branch"] == ss.BRANCH and "sha" not in put


def test_updates_existing_branch_and_reuses_pr():
    routes = base_routes("old")
    routes[("GET", "repos/acme/demo/git/ref/heads/main")] = (0, {"object": {"sha": "abc"}})
    routes[("GET", f"repos/acme/demo/git/ref/heads/{ss.BRANCH}")] = (0, {"object": {"sha": "old"}})
    routes[("PATCH", f"repos/acme/demo/git/refs/heads/{ss.BRANCH}")] = (0, {})
    routes[("PUT", f"repos/acme/demo/contents/{ss.PATH}")] = (0, {})
    fake = FakeGH(routes, prs=json.dumps([
        {"url": "https://github.com/acme/demo/pull/2", "author": {"login": "mallory"}},
        {"url": "https://github.com/acme/demo/pull/3", "author": {"login": "app/squadhq-ops"}}]))
    with mock.patch.object(ss, "gh", fake):
        status, row = ss.sync("acme/demo", False, SUBS)
    assert status == "ok" and "pull/3" in row
    put = next(json.loads(i) for a, i in fake.calls if a[:3] == ["api", "-X", "PUT"])
    assert put["sha"] == "s1"
    assert not any(a[:2] == ["pr", "create"] for a, _ in fake.calls)


@pytest.mark.parametrize("missing", ["ref", "branch", "put"])
def test_write_errors(missing):
    routes = base_routes(None)
    if missing != "ref":
        routes[("GET", "repos/acme/demo/git/ref/heads/main")] = (0, {"object": {"sha": "abc"}})
    if missing != "branch":
        routes[("POST", "repos/acme/demo/git/refs")] = (0, {})
    fake = FakeGH(routes)
    with mock.patch.object(ss, "gh", fake):
        status, row = ss.sync("acme/demo", False)
    assert status == "error"


def test_pr_create_failure_is_error():
    routes = base_routes(None)
    routes[("GET", "repos/acme/demo/git/ref/heads/main")] = (0, {"object": {"sha": "abc"}})
    routes[("POST", "repos/acme/demo/git/refs")] = (0, {})
    routes[("PUT", f"repos/acme/demo/contents/{ss.PATH}")] = (0, {})
    fake = FakeGH(routes)

    def gh(args, input_text=None):
        if args[:2] == ["pr", "create"]:
            return subprocess.CompletedProcess(args, 1, "", "no permission")
        return fake(args, input_text)
    with mock.patch.object(ss, "gh", gh):
        assert ss.sync("acme/demo", False)[0] == "error"


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with mock.patch.object(ss, "gh", FakeGH(base_routes(ss.render(SUBS)))):
        assert ss.main([]) == 0
    assert "1 ok / 0 failed" in summary.read_text()
    # server mode syncs a second file on its own branch; it is missing -> dry-run "out of date"
    with mock.patch.object(ss, "gh", FakeGH(base_routes(ss.render(SUBS)))):
        assert ss.main(["--server", "--dry-run"]) == 0
    assert ss.SERVER_PATH in capsys.readouterr().out
    # an unresolvable hub ref fails every repo
    with mock.patch.object(ss, "gh", FakeGH(base_routes(ss.render(SUBS)))):
        assert ss.main(["--ref", "nope"]) == 1
    with mock.patch.object(ss, "gh", FakeGH({})):
        assert ss.main(["--repo", "acme/other"]) == 1
    capsys.readouterr()
    with mock.patch.object(ss, "gh", FakeGH({})), mock.patch.object(ss, "sync", side_effect=RuntimeError("boom")):
        assert ss.main(["--dry-run"]) == 1
    assert "boom" in capsys.readouterr().out


def test_api_seam_uses_client(monkeypatch):
    from squadhq import github
    seen = []

    def runner(cmd, **kw):
        seen.append((cmd, kw.get("input")))
        return subprocess.CompletedProcess(cmd, 0, "not json", "")
    github.set_client(github.GitHubClient(runner=runner))
    try:
        code, data, _ = ss.api("repos/x/y", "POST", {"a": 1})
    finally:
        github.set_client(None)
    assert code == 0 and data == {}
    assert seen[0][0][:4] == ["gh", "api", "-X", "POST"] and json.loads(seen[0][1]) == {"a": 1}


def test_server_stub_uses_its_own_branch():
    routes = base_routes(None)
    routes[("GET", "repos/acme/demo/git/ref/heads/main")] = (0, {"object": {"sha": "abc"}})
    routes[("POST", "repos/acme/demo/git/refs")] = (0, {})
    routes[("PUT", f"repos/acme/demo/contents/{ss.SERVER_PATH}")] = (0, {})
    fake = FakeGH(routes)
    with mock.patch.object(ss, "gh", fake):
        status, row = ss.sync("acme/demo", False, SUBS, server=True)
    assert status == "ok" and ss.SERVER_PATH in row
    put = next(json.loads(i) for a, i in fake.calls if a[:3] == ["api", "-X", "PUT"])
    assert put["branch"] == ss.BRANCH + "-server"


def test_spoofed_fork_pr_with_our_branch_name_is_not_reused():
    routes = base_routes("old")
    routes[("GET", "repos/acme/demo/git/ref/heads/main")] = (0, {"object": {"sha": "abc"}})
    routes[("GET", f"repos/acme/demo/git/ref/heads/{ss.BRANCH}")] = (0, {"object": {"sha": "old"}})
    routes[("PATCH", f"repos/acme/demo/git/refs/heads/{ss.BRANCH}")] = (0, {})
    routes[("PUT", f"repos/acme/demo/contents/{ss.PATH}")] = (0, {})
    fake = FakeGH(routes, prs=json.dumps([{"url": "https://github.com/acme/demo/pull/2",
                                           "author": {"login": "mallory"}}]))
    with mock.patch.object(ss, "gh", fake):
        status, row = ss.sync("acme/demo", False, SUBS)
    assert "pull/2" not in row
    assert any(a[:2] == ["pr", "create"] for a, _ in fake.calls)
