from __future__ import annotations

import json
from typing import Any

import pytest

from squadhq.interfaces import FatalError, TransientError
from squadhq.trackers.linear import LinearTracker

from ._util import fx


class GQL:
    """Routes GraphQL calls by the first top-level field in the query."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(self, req, timeout):
        body = json.loads(req.data)
        self.calls.append(body)
        for name, resp in self.responses.items():
            if f"{name}(" in body["query"]:
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                return _Resp(resp)
        raise AssertionError(body["query"])


class _Resp:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return json.dumps(self.obj).encode()


def test_list_paginates_and_filters():
    g = GQL({"issues": [fx("linear_issues_page1.json"), fx("linear_issues_page2.json")]})
    tr = LinearTracker("lin_api_test", transport=g)
    tickets = tr.list_tickets("ENG", labels=["backend", "status:in-review"])
    assert [t.key for t in tickets] == ["ENG-42", "ENG-43"]
    assert tickets[0].labels == ["backend", "status:in-review"] and tickets[0].association == "MEMBER"
    assert tickets[1].state == "closed" and tickets[1].association == "BOT"
    flt = g.calls[0]["variables"]["filter"]
    assert flt["team"] == {"key": {"eq": "ENG"}}
    assert flt["state"] == {"type": {"nin": ["completed", "canceled"]}}
    assert {"state": {"name": {"in": ["In Review"]}}} in flt["and"]
    assert g.calls[1]["variables"]["after"] == "c1"


def test_get_comments():
    g = GQL({"issue": [fx("linear_issue.json"), fx("linear_comments.json")]})
    tr = LinearTracker("k", transport=g)
    assert tr.get_ticket("ENG", "ENG-42").native_state == "In Review"
    cs = tr.list_comments("ENG", "ENG-42")
    assert [(c.author, c.association) for c in cs] == [("pm@acme.test", "MEMBER"), ("GitHub", "BOT")]


def test_create_add_remove_close():
    created = {"data": {"issueCreate": {"success": True, "issue": fx("linear_issue.json")["data"]["issue"]}}}
    g = GQL({"teams": fx("linear_team.json"),
             "issueLabelCreate": {"data": {"issueLabelCreate": {"success": True, "issueLabel": {"id": "l9", "name": "urgent"}}}},
             "issueCreate": created,
             "issueUpdate": {"data": {"issueUpdate": {"success": True}}},
             "commentCreate": {"data": {"commentCreate": {"success": True, "comment": {"id": "c", "body": "hi", "createdAt": "t"}}}},
             "issue": fx("linear_issue.json")})
    tr = LinearTracker("k", transport=g)
    t = tr.create_ticket("ENG", "Add", "Body", ["backend", "urgent", "status:in-progress"])
    assert t.key == "ENG-42"
    inp = next(c for c in g.calls if "issueCreate(" in c["query"])["variables"]["input"]
    assert inp["labelIds"] == ["l1", "l9"] and inp["stateId"] == "s-prog" and inp["teamId"] == "team-1"
    tr.add_labels("ENG", "ENG-42", ["urgent", "status:in-review"])
    upd = [c for c in g.calls if "issueUpdate(" in c["query"]]
    assert upd[-1]["variables"]["input"] == {"addedLabelIds": ["l9"], "stateId": "s-rev"}
    tr.remove_labels("ENG", "ENG-42", ["backend", "status:in-review", "missing"])
    upd = [c for c in g.calls if "issueUpdate(" in c["query"]]
    assert upd[-1]["variables"]["input"] == {"removedLabelIds": ["l1"], "stateId": "s-todo"}
    tr.close_ticket("ENG", "ENG-42", "not_planned")
    assert [c for c in g.calls if "issueUpdate(" in c["query"]][-1]["variables"]["input"] == {"stateId": "s-can"}
    assert tr.comment("ENG", "ENG-42", "hi").body == "hi"


def test_graphql_errors_mapped():
    tr = LinearTracker("k", transport=GQL({"issue": {"errors": [{"message": "slow down", "extensions": {"code": "RATELIMITED"}}]}}))
    with pytest.raises(TransientError):
        tr.get_ticket("ENG", "ENG-1")
    tr = LinearTracker("k", transport=GQL({"issue": {"errors": [{"message": "bad"}]}}))
    with pytest.raises(FatalError):
        tr.get_ticket("ENG", "ENG-1")
    tr = LinearTracker("k", transport=GQL({"issue": {"data": {"issue": None}}}))
    with pytest.raises(FatalError, match="not found"):
        tr.get_ticket("ENG", "ENG-1")
    tr = LinearTracker("k", transport=GQL({"teams": {"data": {"teams": {"nodes": []}}}}))
    with pytest.raises(FatalError, match="team"):
        tr.team("NOPE")
    g = GQL({"teams": fx("linear_team.json"), "issue": fx("linear_issue.json")})
    with pytest.raises(FatalError):
        LinearTracker("k", transport=g, status_overrides={"label_to_state": {"needs-human": "Nowhere"}}).add_labels("ENG", "ENG-42", ["needs-human"])
