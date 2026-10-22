from __future__ import annotations

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError, IssueTracker
from squadhq.trackers import available, get_tracker
from squadhq.trackers.github import GitHubTracker

from ._util import fx

API = "https://api.github.com"


def make(t: FakeTransport) -> GitHubTracker:
    return GitHubTracker(token="ghs_test", transport=t)


def test_registry_returns_protocol_instance():
    tr = get_tracker("github", token="x", transport=FakeTransport())
    assert isinstance(tr, IssueTracker)
    assert available() == ["github", "gitlab", "jira", "linear", "trello"]
    with pytest.raises(ValueError):
        get_tracker("nope")


def test_list_paginates_and_skips_prs():
    t = FakeTransport()
    t.add("GET repos/acme/app/issues?", (200, fx("github_issues_page1.json"),
                                          {"Link": f'<{API}/repositories/1/issues?page=2>; rel="next", <{API}/x>; rel="last"'}))
    t.add("GET issues?page=2", (200, fx("github_issues_page2.json")))
    tickets = make(t).list_tickets("acme/app", labels=["triaged:ready"])
    assert [x.key for x in tickets] == ["42", "44"]
    assert tickets[0].association == "MEMBER" and tickets[0].labels == ["triaged:ready", "P2"]
    assert tickets[0].assignees == ["dev1"]
    assert tickets[1].association == "BOT"
    assert "labels=triaged%3Aready" in t.calls[0]["url"]
    assert t.calls[0]["headers"]["Authorization"] == "Bearer ghs_test"


def test_list_respects_limit_and_warns(capsys):
    t = FakeTransport()
    t.add("GET repos/acme/app/issues?", (200, fx("github_issues_page1.json"), {"Link": f'<{API}/next?page=2>; rel="next"'}))
    tickets = make(t).list_tickets("acme/app", limit=1)
    assert len(tickets) == 1
    assert "limit" in capsys.readouterr().err


def test_get_comments_create_comment_labels_close():
    t = FakeTransport()
    t.add("GET issues/42/comments", (200, fx("github_comments.json")))
    t.add("GET issues/42", (200, fx("github_issue.json")))
    t.add("POST repos/acme/app/issues/42/comments", (201, {"id": 1, "user": {"login": "squadhq-ops[bot]", "type": "Bot"}, "body": "hi"}))
    t.add("POST repos/acme/app/issues/42/labels", (200, []))
    t.add("DELETE labels/", (404, {"message": "Label does not exist"}))
    t.add("PATCH repos/acme/app/issues/42", (200, {}))
    t.add("POST repos/acme/app/issues", (201, fx("github_issue.json")))
    tr = make(t)
    assert tr.get_ticket("acme/app", "42").title == "Add login"
    comments = tr.list_comments("acme/app", "42")
    assert [c.association for c in comments] == ["OWNER", "CONTRIBUTOR"]
    assert tr.comment("acme/app", "42", "hi").association == "BOT"
    tr.add_labels("acme/app", "42", ["a"])
    tr.add_labels("acme/app", "42", [])
    tr.remove_labels("acme/app", "42", ["status:in-review"])
    tr.close_ticket("acme/app", "42", "not_planned")
    created = tr.create_ticket("acme/app", "T", "B", ["x"])
    assert created.key == "42"
    bodies = {(c["method"], c["url"].split("acme/app")[-1]): c["body"] for c in t.calls}
    assert bodies[("PATCH", "/issues/42")] == {"state": "closed", "state_reason": "not_planned"}
    assert bodies[("POST", "/issues")] == {"title": "T", "body": "B", "labels": ["x"]}
    assert any("status%3Ain-review" in c["url"] for c in t.calls)


def test_missing_token_is_fatal(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(FatalError):
        GitHubTracker(transport=FakeTransport()).get_ticket("a/b", "1")


def test_env_token_and_enterprise_url(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "envtok")
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3/")
    t = FakeTransport().add("GET ghe.example.com/api/v3/repos/a/b/issues/1", (200, fx("github_issue.json")))
    GitHubTracker(transport=t).get_ticket("a/b", "1")
    assert t.calls[0]["headers"]["Authorization"] == "Bearer envtok"
