import base64
import json
import logging
from pathlib import Path
from unittest import mock

import pytest

from squadhq.codehost import available, get_codehost
from squadhq.codehost.github import GitHubCodeHost
from squadhq.http import FakeTransport
from squadhq.interfaces import CodeHost, FatalError, TransientError

FIX = Path(__file__).parent / "fixtures"
API = "https://api.github.com"


def fx(name):
    return json.loads((FIX / name).read_text())


def host(t, **kw):
    return get_codehost("github", token="t0k", transport=t, **kw)


def test_registry():
    assert "github" in available()
    h = host(FakeTransport())
    assert isinstance(h, CodeHost)
    with pytest.raises(ValueError):
        get_codehost("bitbucket")


def test_env_defaults(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "envtok")
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3/")
    h = GitHubCodeHost()
    assert h.token == "envtok"
    assert h.api_url == "https://ghe.example.com/api/v3"


def test_list_pull_requests_paginates_and_maps():
    t = FakeTransport()
    t.add("GET /repos/acme/app/pulls", (200, fx("github_pulls_page1.json"),
                                         {"Link": f'<{API}/repos/acme/app/pulls?page=2>; rel="next"'}))
    t.add("GET /repos/acme/app/pulls?page=2", (200, fx("github_pulls_page2.json")))
    prs = host(t).list_pull_requests("acme/app", state="all")
    assert [p.number for p in prs] == ["7", "8", "9", "10"]
    assert prs[0].author_is_bot and not prs[1].author_is_bot
    assert prs[0].labels == ["status:in-review", "risk:low"]
    assert prs[1].draft and prs[1].body == ""
    assert prs[2].state == "merged" and prs[3].state == "closed" and prs[0].state == "open"
    assert "state=all" in t.calls[0]["url"]
    assert t.calls[0]["headers"]["Authorization"] == "Bearer t0k"


def test_list_pull_requests_limit_warns(caplog):
    t = FakeTransport().add("GET /pulls", (200, fx("github_pulls_page1.json"),
                                             {"Link": f'<{API}/repos/acme/app/pulls?page=2>; rel="next"'}))
    with caplog.at_level(logging.WARNING):
        prs = host(t).list_pull_requests("acme/app", limit=2)
    assert len(prs) == 2
    assert "truncated" in caplog.text
    assert "per_page=2" in t.calls[0]["url"]


def test_get_pull_request_with_files():
    t = FakeTransport()
    t.add("GET /repos/acme/app/pulls/7", (200, fx("github_pr.json")))
    t.add("GET /repos/acme/app/pulls/7/files", (200, fx("github_pr_files.json")))
    pr = host(t).get_pull_request("acme/app", "7")
    assert pr.changed_files == ["src/a.py", "README.md"]
    assert pr.head_ref == "agent/7-widget" and pr.base_ref == "main"
    assert pr.url.endswith("/pull/7")


def test_list_checks():
    t = FakeTransport().add("GET /commits/agent%2Fx/check-runs", (200, fx("github_check_runs.json")))
    checks = host(t).list_checks("acme/app", "agent/x")
    assert [(c.name, c.status, c.conclusion) for c in checks] == [
        ("build", "completed", "success"), ("lint", "in_progress", None), ("odd", "queued", None)]


def test_read_file():
    t = FakeTransport().add("GET /contents/state/capacity.json", (200, fx("github_contents.json")))
    content, sha = host(t).read_file("acme/app", "state/capacity.json", ref="main")
    assert json.loads(content) == {"paused_until": ""}
    assert sha == "abc123"
    assert "ref=main" in t.calls[0]["url"]


def test_read_file_directory_is_fatal():
    t = FakeTransport().add("GET /contents/dir", (200, [{"type": "file"}]))
    with pytest.raises(FatalError):
        host(t).read_file("acme/app", "dir")


def test_write_file_cas_success():
    t = FakeTransport().add("PUT /contents/state/capacity.json", (200, fx("github_put_contents.json")))
    new = host(t).write_file("acme/app", "state/capacity.json", "{}", "chore: x", "abc123")
    assert new == "def456"
    body = t.calls[0]["body"]
    assert body["sha"] == "abc123"
    assert base64.b64decode(body["content"]) == b"{}"


def test_write_file_create_without_sha():
    t = FakeTransport().add("PUT /contents/new.txt", (201, fx("github_put_contents.json")))
    host(t).write_file("acme/app", "new.txt", "x", "add", None)
    assert "sha" not in t.calls[0]["body"]


@pytest.mark.parametrize("status,body,exc", [
    (409, {"message": "is at abc but expected def"}, TransientError),
    (422, {"message": "sha wasn't supplied"}, TransientError),
    (422, {"message": "Invalid request: path too long"}, FatalError),
])
def test_write_file_conflicts(status, body, exc):
    t = FakeTransport().add("PUT /contents/f", (status, body))
    with pytest.raises(exc):
        host(t).write_file("acme/app", "f", "x", "m", "old")


def test_merge_ok_and_errors():
    t = FakeTransport().add("PUT /pulls/7/merge", (200, {"merged": True}))
    host(t).merge("acme/app", "7", "rebase")
    assert t.calls[0]["body"] == {"merge_method": "rebase"}
    with pytest.raises(FatalError):
        host(FakeTransport().add("PUT /merge", (405, {"message": "not mergeable"}))).merge("a/b", "1")
    with pytest.raises(TransientError):
        host(FakeTransport().add("PUT /merge", (409, {"message": "Head branch was modified"}))).merge("a/b", "1")


def test_server_error_retries_then_raises():
    t = FakeTransport().add("GET /pulls/1", (502, "bad gateway"))
    with mock.patch("squadhq.http.time.sleep") as sleep, pytest.raises(TransientError):
        host(t).get_pull_request("a/b", "1")
    assert sleep.call_count == 2 and len(t.calls) == 3


def test_auth_failure_is_fatal():
    t = FakeTransport().add("GET /pulls", (401, {"message": "Bad credentials"}))
    with pytest.raises(FatalError):
        host(t).list_pull_requests("a/b")


def test_no_token_no_auth_header(monkeypatch):
    t = FakeTransport().add("GET /pulls", (200, []))
    GitHubCodeHost(token="", transport=t).list_pull_requests("a/b")
    assert "Authorization" not in t.calls[0]["headers"]
