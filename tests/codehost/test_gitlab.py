import json
import logging
from pathlib import Path

import pytest

from squadhq.codehost import get_codehost
from squadhq.codehost.gitlab import GitLabCodeHost
from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError, TransientError

FIX = Path(__file__).parent / "fixtures"
BASE = "https://gitlab.example.com"
PID = "acme%2Fapp"


def fx(name):
    return json.loads((FIX / name).read_text())


def host(t):
    return get_codehost("gitlab", token="glpat", base_url=BASE + "/", transport=t)


def test_env_defaults(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "x")
    monkeypatch.delenv("GITLAB_URL", raising=False)
    h = GitLabCodeHost()
    assert h.token == "x" and h.base_url == "https://gitlab.com"
    assert GitLabCodeHost(token="")._headers() == {}


def test_list_mrs_maps_states_and_paginates(caplog):
    t = FakeTransport()
    t.add(f"GET /projects/{PID}/merge_requests", (200, fx("gitlab_mrs.json"),
                                                  {"Link": f'<{BASE}/api/v4/projects/{PID}/merge_requests?page=2>; rel="next"'}))
    t.add(f"GET /projects/{PID}/merge_requests?page=2", (200, []))
    prs = host(t).list_pull_requests("acme/app", state="all")
    assert [p.state for p in prs] == ["open", "merged", "closed"]
    assert prs[0].author_is_bot and prs[0].draft and not prs[1].author_is_bot
    assert prs[0].number == "12" and prs[0].head_ref == "agent/12"
    assert "state=all" in t.calls[0]["url"]
    assert t.calls[0]["headers"]["Private-token"] == "glpat"
    with caplog.at_level(logging.WARNING):
        t2 = FakeTransport().add("GET /merge_requests", (200, fx("gitlab_mrs.json")))
        assert len(host(t2).list_pull_requests("acme/app", limit=2)) == 2
    assert "truncated" in caplog.text


def test_get_mr_with_changes():
    t = FakeTransport().add("GET /merge_requests/12/changes", (200, fx("gitlab_mr_changes.json")))
    pr = host(t).get_pull_request("acme/app", "12")
    assert pr.changed_files == ["a.py", "old.py"]


def test_checks():
    t = FakeTransport().add("GET /repository/commits/abc/statuses", (200, fx("gitlab_statuses.json")))
    checks = host(t).list_checks("acme/app", "abc")
    assert [(c.status, c.conclusion) for c in checks] == [
        ("completed", "success"), ("in_progress", None), ("completed", "failure"),
        ("queued", None), ("completed", "neutral")]


def test_read_and_write_file():
    t = FakeTransport()
    t.add("GET /repository/files/README.md", (200, fx("gitlab_file.json")))
    t.add("PUT /repository/files/README.md", (200, {"file_path": "README.md", "branch": "main"}))
    t.add("POST /repository/files/README.md", (201, {"file_path": "README.md", "branch": "main"}))
    h = host(t)
    assert h.read_file("acme/app", "README.md") == ("hello\n", "lc1")
    assert "ref=HEAD" in t.calls[0]["url"]
    assert h.write_file("acme/app", "README.md", "x", "msg", "lc0") == "lc1"
    put = [c for c in t.calls if c["method"] == "PUT"][0]
    assert put["body"]["last_commit_id"] == "lc0" and put["body"]["branch"] == "main"
    h.write_file("acme/app", "README.md", "x", "msg", None)
    assert any(c["method"] == "POST" for c in t.calls)


@pytest.mark.parametrize("status,body,exc", [
    (400, {"message": "You are attempting to update a file that has changed since you started editing it."}, TransientError),
    (409, {"message": "conflict"}, TransientError),
    (400, {"message": "branch is required"}, FatalError),
])
def test_write_conflicts(status, body, exc):
    t = FakeTransport().add("PUT /repository/files/f", (status, body))
    with pytest.raises(exc):
        host(t).write_file("acme/app", "f", "x", "m", "old")


def test_merge_variants():
    t = FakeTransport()
    t.add("PUT /merge_requests/12/merge", (200, {"state": "merged"}))
    t.add("PUT /merge_requests/12/rebase", (202, {"rebase_in_progress": True}))
    h = host(t)
    h.merge("acme/app", "12")
    assert t.calls[-1]["body"] == {"squash": True}
    h.merge("acme/app", "12", "rebase")
    assert t.calls[-2]["url"].endswith("/rebase")
    with pytest.raises(TransientError):
        host(FakeTransport().add("PUT /merge", (406, {"message": "Branch cannot be merged"}))).merge("a/b", "1")
    with pytest.raises(FatalError):
        host(FakeTransport().add("PUT /merge", (405, {"message": "Method Not Allowed"}))).merge("a/b", "1")
