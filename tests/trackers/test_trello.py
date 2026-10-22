from __future__ import annotations

import pytest

from squadhq.http import FakeTransport
from squadhq.interfaces import FatalError
from squadhq.trackers.trello import TrelloTracker, created_from_id

from ._util import fx


def make() -> tuple[TrelloTracker, FakeTransport]:
    t = FakeTransport()
    t.add("GET boards/board-1/lists", (200, fx("trello_lists.json")))
    t.add("GET boards/board-1/labels", (200, fx("trello_labels.json")))
    t.add("GET boards/board-1/members", (200, fx("trello_members.json")))
    t.add("GET boards/board-1/cards/open", (200, fx("trello_cards.json")))
    t.add("GET cards/5f1a2b3c0000000000000001?", (200, fx("trello_card.json")))
    t.add("GET cards/5f1a2b3c0000000000000001/actions", (200, fx("trello_actions.json")))
    t.add("POST labels", (200, {"id": "lab-9", "name": "urgent"}))
    t.add("POST cards?", (200, fx("trello_card.json")))
    t.add("POST idLabels", (200, []))
    t.add("DELETE idLabels", (200, {}))
    t.add("PUT cards/", (200, {}))
    t.add("POST actions/comments", (200, {"id": "a9", "date": "d", "memberCreator": {"username": "bot"}}))
    return TrelloTracker("key", "tok", transport=t), t


def test_list_maps_lists_to_flow_labels():
    tr, t = make()
    tickets = tr.list_tickets("board-1")
    assert [x.native_state for x in tickets] == ["Doing", "To Do"]
    assert tickets[0].labels == ["backend", "status:in-progress"]
    assert tickets[0].created_at == created_from_id("5f1a2b3c") == "2020-07-24T00:28:44Z"
    assert tr.list_tickets("board-1", labels=["status:in-progress"])[0].key.endswith("1")
    assert 'oauth_token="tok"' in t.calls[0]["headers"]["Authorization"]
    assert "token=" not in t.calls[0]["url"]


def test_comments_association_from_membership():
    tr, _ = make()
    cs = tr.list_comments("board-1", "5f1a2b3c0000000000000001")
    assert [(c.author, c.association, c.body) for c in cs] == [("alice", "MEMBER", "first"), ("mallory", "NONE", "second")]


def test_create_labels_move_close():
    tr, t = make()
    card = "5f1a2b3c0000000000000001"
    tr.create_ticket("board-1", "T", "B", ["backend", "urgent", "status:in-review"])
    post = next(c for c in t.calls if c["method"] == "POST" and "/cards?" in c["url"])
    assert "idList=list-review" in post["url"] and "idLabels=lab-1%2Clab-9" in post["url"]
    tr.add_labels("board-1", card, ["backend", "status:in-progress"])
    assert "idList=list-doing" in t.calls[-1]["url"]
    tr.remove_labels("board-1", card, ["backend", "nolabel", "status:in-progress"])
    assert "idList=list-todo" in t.calls[-1]["url"]
    tr.close_ticket("board-1", card)
    assert "closed=true" in t.calls[-1]["url"] and "idList=list-done" in t.calls[-1]["url"]
    assert tr.comment("board-1", card, "hi").id == "a9"


def test_create_without_matching_list_uses_first_and_errors():
    tr, t = make()
    tr.create_ticket("board-1", "T", "B", [])
    assert "idList=list-todo" in next(c for c in t.calls if "/cards?" in c["url"] and c["method"] == "POST")["url"]
    with pytest.raises(FatalError):
        tr.add_labels("board-1", "c", ["needs-human"])  # board has no Blocked list


def test_duplicate_label_is_ok():
    tr, t = make()
    t.add("POST idLabels", (400, "that label is already on the card"))
    tr.add_labels("board-1", "c", ["backend"])
    t.add("POST idLabels", (400, "invalid value"))
    with pytest.raises(FatalError):
        tr.add_labels("board-1", "c", ["backend"])


def test_bad_id():
    assert created_from_id("zz") == ""
