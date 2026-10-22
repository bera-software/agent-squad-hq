from __future__ import annotations

from squadhq.http import FakeTransport
from squadhq.trackers.gitlab import GitLabTracker, access_to_association

from ._util import fx

P = "projects/grp%2Fapp"


def make() -> tuple[GitLabTracker, FakeTransport]:
    t = FakeTransport()
    t.add(f"GET {P}/issues?", (200, fx("gitlab_issues.json"), {"Link": '<https://gitlab.com/api/v4/next?page=2>; rel="next"'}))
    t.add("GET next?page=2", (200, []))
    t.add(f"GET {P}/members/all/11", (200, {"access_level": 40}))
    t.add(f"GET {P}/members/all/12", (404, {"message": "404 Not found"}))
    t.add(f"GET {P}/issues/5/notes", (200, fx("gitlab_notes.json")))
    t.add(f"GET {P}/issues/5", (200, fx("gitlab_issues.json")[0]))
    t.add(f"POST {P}/issues/5/notes", (201, {"id": 3, "body": "hi", "author": {"username": "bot"}}))
    t.add(f"POST {P}/issues", (201, fx("gitlab_issues.json")[0]))
    t.add(f"PUT {P}/issues/5", (200, {}))
    return GitLabTracker("glpat-test", transport=t), t


def test_list_and_association():
    tr, t = make()
    tickets = tr.list_tickets("grp/app", labels=["bug"])
    assert [(x.key, x.association) for x in tickets] == [("5", "MEMBER"), ("6", "NONE"), ("7", "BOT")]
    assert "state=opened" in t.calls[0]["url"] and "labels=bug" in t.calls[0]["url"]
    assert t.calls[0]["headers"]["Private-token"] == "glpat-test"
    tr.get_ticket("grp/app", "5")
    assert sum("members/all/11" in c["url"] for c in t.calls) == 1  # cached


def test_comments_skip_system_notes_and_writes():
    tr, t = make()
    cs = tr.list_comments("grp/app", "5")
    assert [c.body for c in cs] == ["please fix"]
    assert tr.comment("grp/app", "5", "hi").body == "hi"
    tr.create_ticket("grp/app", "T", "B", ["a", "b"])
    assert t.calls[-1]["body"] == {"title": "T", "description": "B", "labels": "a,b"}
    tr.add_labels("grp/app", "5", ["x"])
    assert t.calls[-1]["body"] == {"add_labels": "x"}
    tr.remove_labels("grp/app", "5", ["x", "y"])
    assert t.calls[-1]["body"] == {"remove_labels": "x,y"}
    tr.close_ticket("grp/app", "5", "not_planned")
    assert t.calls[-1]["body"] == {"state_event": "close", "add_labels": "not-planned"}
    n = len(t.calls)
    tr.add_labels("grp/app", "5", [])
    tr.remove_labels("grp/app", "5", [])
    assert len(t.calls) == n


def test_access_levels():
    assert [access_to_association(x) for x in (None, 5, 10, 30, 50)] == ["NONE", "NONE", "CONTRIBUTOR", "MEMBER", "OWNER"]
    tr = GitLabTracker("t", transport=FakeTransport(), resolve_association=False)
    assert tr.association_for("p", {"id": 1, "username": "u"}) == "NONE"
    assert tr.association_for("p", None) == "NONE"
