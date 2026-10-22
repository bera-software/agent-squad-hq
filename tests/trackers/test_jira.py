from __future__ import annotations

import base64

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError
from squadhq.trackers.jira import JiraTracker

from ._util import fx

BASE = "https://acme.atlassian.net"


def make(t: FakeTransport, **kw) -> JiraTracker:
    return JiraTracker(BASE, "bot@acme.test", "tok", transport=t, **kw)


def test_search_paginates_with_next_page_token_and_maps_status():
    t = FakeTransport().add("POST search/jql", (200, fx("jira_search_page1.json")), (200, fx("jira_search_page2.json")))
    tickets = make(t).list_tickets("ENG", state="all")
    assert [x.key for x in tickets] == ["ENG-42", "ENG-43"]
    a, b = tickets
    assert a.state == "open" and b.state == "closed"
    assert a.labels == ["backend", "status:in-progress"] and a.native_state == "In Progress"
    assert a.association == "MEMBER" and a.author == "pm@acme.test" and b.association == "NONE"
    assert a.url == f"{BASE}/browse/ENG-42"
    assert a.body.startswith("## Goal\n\nUsers **must** log in.")
    assert t.calls[1]["body"]["nextPageToken"] == "tok2"
    assert t.calls[0]["headers"]["Authorization"] == "Basic " + base64.b64encode(b"bot@acme.test:tok").decode()


def test_jql_translates_state_labels_to_status():
    jql = make(FakeTransport()).build_jql("ENG", "open", ["backend", "status:in-review"])
    assert jql == ('project = "ENG" AND statusCategory != Done AND labels = "backend" AND '
                   'status in ("In Review", "Code Review") ORDER BY created ASC')
    assert "statusCategory = Done" in make(FakeTransport()).build_jql("ENG", "closed", [])


def test_legacy_search_fallback():
    t = FakeTransport().add("POST search/jql", (404, {"errorMessages": ["nope"]}))
    t.add("GET rest/api/3/search?", (200, fx("jira_search_legacy.json")))
    assert len(make(t).list_tickets("ENG")) == 2


def test_labels_and_transitions():
    t = FakeTransport()
    t.add("PUT issue/ENG-42", (204, None))
    t.add("GET issue/ENG-42/transitions", (200, fx("jira_transitions.json")))
    t.add("POST issue/ENG-42/transitions", (204, None))
    t.add("GET issue/ENG-42?", (200, fx("jira_issue.json")))
    tr = make(t)
    tr.add_labels("ENG", "ENG-42", ["urgent", "status:in-review"])
    put = next(c for c in t.calls if c["method"] == "PUT")
    assert put["body"] == {"update": {"labels": [{"add": "urgent"}]}}
    post = [c for c in t.calls if c["method"] == "POST"]
    assert post[-1]["body"] == {"transition": {"id": "21"}}
    # removing the state label the ticket is in -> back to the open state
    tr.remove_labels("ENG", "ENG-42", ["status:in-progress", "urgent"])
    assert [c for c in t.calls if c["method"] == "POST"][-1]["body"] == {"transition": {"id": "31"}}
    # close falls back to any done-category transition ("Closed")
    tr.close_ticket("ENG", "ENG-42")
    assert [c for c in t.calls if c["method"] == "POST"][-1]["body"] == {"transition": {"id": "41"}}


def test_transition_unavailable_is_fatal():
    t = FakeTransport().add("GET transitions", (200, {"transitions": []}))
    with pytest.raises(FatalError, match="no transition"):
        make(t).add_labels("ENG", "ENG-42", ["needs-human"])


def test_create_comment_and_list_comments():
    t = FakeTransport()
    t.add("POST rest/api/3/issue", (201, fx("jira_created.json")))
    t.add("GET issue/ENG-42?", (200, fx("jira_issue.json")))
    t.add("GET issue/ENG-42/transitions", (200, fx("jira_transitions.json")))
    t.add("POST issue/ENG-42/transitions", (204, None))
    t.add("POST issue/ENG-42/comment", (201, fx("jira_comment_created.json")))
    t.add("GET issue/ENG-42/comment", (200, fx("jira_comments.json")))
    tr = make(t, issue_type="Story")
    ticket = tr.create_ticket("ENG", "Add login", "Para with `code`", ["backend", "status:in-progress"])
    assert ticket.key == "ENG-42"
    create = t.calls[0]["body"]["fields"]
    assert create["issuetype"] == {"name": "Story"} and create["labels"] == ["backend"]
    assert create["description"]["type"] == "doc"
    c = tr.comment("ENG", "ENG-42", "**hi**")
    assert c.association == "BOT" and c.body == "**hi**"
    comments = tr.list_comments("ENG", "ENG-42")
    assert comments[0].body == "done `x()`" and comments[0].association == "BOT"


def test_missing_config_is_fatal(monkeypatch):
    for v in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(FatalError):
        JiraTracker(transport=FakeTransport()).get_ticket("ENG", "ENG-1")
