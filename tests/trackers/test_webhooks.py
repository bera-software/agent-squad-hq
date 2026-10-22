from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from squadhq.trackers import webhooks as wh

from ._util import fx, raw

SECRET = "whsec"
BODY = b'{"action":"opened"}'
# Precomputed literal vectors (computed once, hardcoded so a regression in the
# helpers cannot hide behind a test that recomputes the same wrong value).
GITHUB_DOCS_SIG = "757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"  # docs.github.com example
SHA256_WHSEC = "f403dd7e1a2565bfb726c469c9221fa161a0c63c4595a7f9a64aeec291a0dc3c"
TRELLO_CB = "https://hq.example.com/api/v1/webhooks/trello"
TRELLO_SIG = "mqEVjPqQILRaF0qjEzOqxBQi6aU="


def test_github_signature_official_vector():
    assert wh.verify_github("It's a Secret to Everybody", b"Hello, World!", "sha256=" + GITHUB_DOCS_SIG)
    assert wh.verify_github(SECRET, BODY, "sha256=" + SHA256_WHSEC)
    assert wh.verify_github(SECRET, BODY, "sha256=" + SHA256_WHSEC.upper())
    assert not wh.verify_github(SECRET, BODY + b" ", "sha256=" + SHA256_WHSEC)
    assert not wh.verify_github(SECRET, BODY, SHA256_WHSEC)  # missing prefix
    assert not wh.verify_github(SECRET, BODY, None)
    assert not wh.verify_github("", BODY, "sha256=" + SHA256_WHSEC)


def test_jira_signature():
    assert wh.verify_jira(SECRET, BODY, "sha256=" + SHA256_WHSEC)
    assert wh.verify_jira(SECRET, BODY, SHA256_WHSEC)
    assert not wh.verify_jira(SECRET, BODY, "sha1=" + SHA256_WHSEC)
    assert not wh.verify_jira(SECRET, BODY, None)
    computed = hmac.new(b"other", BODY, hashlib.sha256).hexdigest()
    assert wh.verify_jira("other", BODY, f"sha256={computed}")


def test_linear_signature_and_replay_window():
    assert wh.verify_linear(SECRET, BODY, SHA256_WHSEC)
    assert wh.verify_linear(SECRET, BODY, SHA256_WHSEC, timestamp_ms=1_000_000, now_ms=1_030_000)
    assert not wh.verify_linear(SECRET, BODY, SHA256_WHSEC, timestamp_ms=1_000_000, now_ms=1_090_001)
    assert not wh.verify_linear(SECRET, BODY, "00" * 32)
    assert not wh.verify_linear(SECRET, BODY, None)


def test_trello_signature():
    assert wh.trello_signature("trello-secret", BODY, TRELLO_CB) == TRELLO_SIG
    assert wh.verify_trello("trello-secret", BODY, TRELLO_CB, TRELLO_SIG)
    assert not wh.verify_trello("trello-secret", BODY, TRELLO_CB + "x", TRELLO_SIG)
    assert not wh.verify_trello("trello-secret", BODY, "", TRELLO_SIG)
    manual = base64.b64encode(hmac.new(b"s", b"x" + b"cb", hashlib.sha1).digest()).decode()
    assert wh.verify_trello("s", b"x", "cb", manual)


def test_gitlab_token():
    assert wh.verify_gitlab("tok", "tok")
    assert not wh.verify_gitlab("tok", "tok2")
    assert not wh.verify_gitlab("tok", None)
    assert not wh.verify_gitlab("", "")


def test_verify_dispatch_case_insensitive_headers():
    assert wh.verify("github", {"x-hub-signature-256": "sha256=" + SHA256_WHSEC}, BODY, secret=SECRET)
    assert wh.verify("jira", {"X-Hub-Signature": "sha256=" + SHA256_WHSEC}, BODY, secret=SECRET)
    assert wh.verify("trello", {"x-trello-webhook": TRELLO_SIG}, BODY, secret="trello-secret", callback_url=TRELLO_CB)
    assert wh.verify("gitlab", {"X-Gitlab-Token": "t"}, BODY, secret="t")
    assert not wh.verify("bitbucket", {}, BODY, secret="t")
    lin_body = json.dumps({"webhookTimestamp": 1}).encode()
    sig = hmac.new(SECRET.encode(), lin_body, hashlib.sha256).hexdigest()
    assert not wh.verify("linear", {"Linear-Signature": sig}, lin_body, secret=SECRET)  # stale timestamp
    lin_body2 = b'{"action":"x"}'
    sig2 = hmac.new(SECRET.encode(), lin_body2, hashlib.sha256).hexdigest()
    assert wh.verify("linear", {"Linear-Signature": sig2}, lin_body2, secret=SECRET)
    assert not wh.verify("linear", {"Linear-Signature": sig2}, b"not json", secret=SECRET)


GH = {"X-GitHub-Delivery": "d-1"}


@pytest.mark.parametrize(
    ("event", "fixture", "action", "key", "assoc"),
    [
        ("issue_comment", "wh_github_issue_comment.json", "comment_created", "42", "OWNER"),
        ("issues", "wh_github_issues_labeled.json", "labels_changed", "42", "MEMBER"),
        ("pull_request", "wh_github_pr_closed.json", "pr_merged", "50", "NONE"),
        ("check_run", "wh_github_check_run.json", "check_completed", "50", "NONE"),
    ],
)
def test_github_events(event, fixture, action, key, assoc):
    ev = wh.normalize_event("github", {**GH, "X-GitHub-Event": event}, raw(fixture))
    assert (ev.action, ev.key, ev.association, ev.project) == (action, key, assoc, "acme/app")
    assert ev.delivery_id == "d-1"
    if event == "issues":
        assert ev.labels_added == ["agent:implement"] and ev.ticket and ev.ticket.key == "42"
    if event == "issue_comment":
        assert ev.text == "@squad implement" and ev.raw_type == "issue_comment.created"
    if event == "check_run":
        assert ev.native_state == "failure"


def test_github_misc_events():
    assert wh.normalize_event("github", {"X-GitHub-Event": "ping"}, b'{"zen":"x"}').action == "ping"
    assert wh.normalize_event("github", {"X-GitHub-Event": "star"}, b"{}").action == "other"
    opened = {"action": "opened", "issue": {"number": 1, "body": "b", "user": {"login": "u"}, "author_association": "NONE"},
              "repository": {"full_name": "a/b"}, "sender": {"login": "u"}}
    ev = wh.normalize_event("github", {"X-GitHub-Event": "issues"}, opened)
    assert ev.action == "ticket_opened" and ev.text == "b" and ev.association == "NONE"
    pr = {"action": "synchronize", "pull_request": {"number": 2, "user": {"login": "x"}},
          "repository": {"full_name": "a/b"}, "sender": {"login": "x", "type": "Bot"}}
    ev = wh.normalize_event("github", {"X-GitHub-Event": "pull_request"}, pr)
    assert ev.action == "pr_updated" and ev.association == "BOT"
    pr["action"] = "opened"
    assert wh.normalize_event("github", {"X-GitHub-Event": "pull_request"}, pr).action == "pr_opened"
    pr["action"] = "closed"
    assert wh.normalize_event("github", {"X-GitHub-Event": "pull_request"}, pr).action == "pr_closed"


def test_jira_issue_updated():
    ev = wh.normalize_event("jira", {"X-Atlassian-Webhook-Identifier": "j1"}, raw("wh_jira_issue_updated.json"))
    assert ev.action == "state_changed" and ev.key == "ENG-42" and ev.project == "ENG"
    assert ev.native_state == "In Progress" and ev.previous_native_state == "Selected for Development"
    assert ev.labels_added == ["urgent", "status:in-progress"] and ev.labels_removed == ["triaged:ready"]
    assert ev.association == "MEMBER" and ev.delivery_id == "j1"
    assert ev.ticket and ev.ticket.url == "https://acme.atlassian.net/browse/ENG-42"


def test_jira_other_events():
    ev = wh.normalize_event("jira", {}, raw("wh_jira_comment_created.json"))
    assert (ev.action, ev.text, ev.association) == ("comment_created", "hello", "NONE")
    created = fx("wh_jira_issue_updated.json")
    created["webhookEvent"] = "jira:issue_created"
    assert wh.normalize_event("jira", {}, created).action == "ticket_opened"
    created["webhookEvent"] = "jira:issue_updated"
    created["changelog"] = {"items": [{"field": "labels", "fromString": "a", "toString": ""}]}
    ev = wh.normalize_event("jira", {}, created)
    assert ev.action == "labels_changed" and ev.labels_removed == ["a"]
    created["changelog"] = {"items": [{"field": "summary"}]}
    assert wh.normalize_event("jira", {}, created).action == "ticket_updated"
    created["webhookEvent"] = "jira:issue_deleted"
    assert wh.normalize_event("jira", {}, created).action == "other"
    done = fx("wh_jira_issue_updated.json")
    done["issue"]["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    done["changelog"]["items"][0]["toString"] = "Done"
    assert wh.normalize_event("jira", {}, done).action == "ticket_closed"


def test_linear_events(monkeypatch):
    monkeypatch.delenv("SQUADHQ_TRUSTED_ACTORS", raising=False)
    ev = wh.normalize_event("linear", {"Linear-Delivery": "l1"}, raw("wh_linear_issue_update.json"))
    # membership is not proven by the payload (guests are type=user too) -> NONE
    assert (ev.action, ev.key, ev.project, ev.association) == ("state_changed", "ENG-42", "ENG", "NONE")
    assert ev.labels_added == ["urgent", "status:in-progress"]
    c = wh.normalize_event("linear", {}, raw("wh_linear_comment_create.json"))
    assert (c.action, c.key, c.text, c.association) == ("comment_created", "ENG-42", "@squad go", "NONE")
    member = fx("wh_linear_issue_update.json")
    member["actor"] = {**(member.get("actor") or {}), "type": "user", "guest": False}
    assert wh.normalize_event("linear", {}, member).association == "MEMBER"
    member["actor"]["guest"] = True
    assert wh.normalize_event("linear", {}, member).association == "NONE"
    member["actor"].pop("guest")
    member["actor"]["email"] = "lead@acme.test"
    monkeypatch.setenv("SQUADHQ_TRUSTED_ACTORS", "Lead@acme.test")
    assert wh.normalize_event("linear", {}, member).association == "MEMBER"
    member["actor"]["type"] = "OauthClient"
    assert wh.normalize_event("linear", {}, member).association == "BOT"
    monkeypatch.delenv("SQUADHQ_TRUSTED_ACTORS")
    body = fx("wh_linear_issue_update.json")
    body["updatedFrom"] = {"labelIds": ["l1"]}
    assert wh.normalize_event("linear", {}, body).action == "labels_changed"
    body["updatedFrom"] = {"title": "old"}
    assert wh.normalize_event("linear", {}, body).action == "ticket_updated"
    body["action"] = "create"
    assert wh.normalize_event("linear", {}, body).action == "ticket_opened"
    body["action"] = "remove"
    assert wh.normalize_event("linear", {}, body).action == "other"
    body["updatedFrom"] = {"stateId": "x"}
    body["action"] = "update"
    body["data"]["state"] = {"name": "Done", "type": "completed"}
    assert wh.normalize_event("linear", {}, body).action == "ticket_closed"
    assert wh.normalize_event("linear", {}, {"type": "Project", "action": "create"}).action == "other"


def test_trello_association_needs_trusted_actor(monkeypatch):
    body = {"action": {"type": "addLabelToCard", "idMemberCreator": "m-123", "memberCreator": {"username": "alice"},
                       "data": {"card": {"id": "c"}, "board": {"id": "b"}, "label": {"name": "agent:implement"}}}}
    monkeypatch.delenv("SQUADHQ_TRUSTED_ACTORS", raising=False)
    assert wh.normalize_event("trello", {}, body).association == "NONE"
    monkeypatch.setenv("SQUADHQ_TRUSTED_ACTORS", "m-123")
    assert wh.normalize_event("trello", {}, body).association == "MEMBER"
    monkeypatch.setenv("SQUADHQ_TRUSTED_ACTORS", "ALICE")
    assert wh.normalize_event("trello", {}, body).association == "MEMBER"


def test_trello_events():
    ev = wh.normalize_event("trello", {}, raw("wh_trello_update_card.json"))
    assert (ev.action, ev.key, ev.project, ev.native_state) == ("state_changed", "card-1", "board-1", "Doing")
    assert ev.labels_added == ["status:in-progress"] and ev.labels_removed == ["triaged:ready"]
    c = wh.normalize_event("trello", {}, raw("wh_trello_comment.json"))
    assert (c.action, c.text, c.association, c.actor) == ("comment_created", "hi", "NONE", "mallory")

    def act(type_, **data):
        return {"action": {"type": type_, "data": {"card": {"id": "c", "closed": data.pop("closed", False)}, "board": {"id": "b"}, **data}}}

    assert wh.normalize_event("trello", {}, act("createCard", list={"name": "To Do"})).action == "ticket_opened"
    assert wh.normalize_event("trello", {}, act("updateCard", closed=True, old={"closed": False})).action == "ticket_closed"
    assert wh.normalize_event("trello", {}, act("updateCard", old={"closed": True})).action == "ticket_reopened"
    assert wh.normalize_event("trello", {}, act("updateCard", old={"name": "x"})).action == "ticket_updated"
    ev = wh.normalize_event("trello", {}, act("addLabelToCard", label={"name": "bug"}))
    assert ev.action == "labels_changed" and ev.labels_added == ["bug"]
    ev = wh.normalize_event("trello", {}, act("removeLabelFromCard", label={"name": "bug"}))
    assert ev.labels_removed == ["bug"]
    assert wh.normalize_event("trello", {}, act("deleteCard")).action == "other"


def test_gitlab_events():
    h = {"X-Gitlab-Event": "Issue Hook", "X-Gitlab-Event-UUID": "g1"}
    ev = wh.normalize_event("gitlab", h, raw("wh_gitlab_issue.json"))
    assert (ev.action, ev.key, ev.project, ev.labels_added) == ("labels_changed", "5", "grp/app", ["triaged:ready"])
    assert ev.delivery_id == "g1" and ev.raw_type == "Issue Hook.update"
    n = wh.normalize_event("gitlab", {"X-Gitlab-Event": "Note Hook"}, raw("wh_gitlab_note.json"))
    assert (n.action, n.key, n.text) == ("comment_created", "5", "@squad fix")
    m = wh.normalize_event("gitlab", {"X-Gitlab-Event": "Merge Request Hook"}, raw("wh_gitlab_mr.json"))
    assert (m.action, m.key) == ("pr_merged", "9")
    body = fx("wh_gitlab_issue.json")
    body["object_attributes"]["action"] = "close"
    body["changes"] = {}
    assert wh.normalize_event("gitlab", {}, body).action == "ticket_closed"
    body["object_attributes"]["action"] = "update"
    assert wh.normalize_event("gitlab", {}, body).action == "ticket_updated"
    assert wh.normalize_event("gitlab", {}, {"object_kind": "pipeline", "user": {"bot": True}}).association == "BOT"


def test_malformed_never_raises():
    assert wh.normalize_event("github", {"X-GitHub-Event": "issues"}, b"not json").action in ("ticket_updated", "other")
    assert wh.normalize_event("github", {}, b"[1,2]").action == "other"
    assert wh.normalize_event("bitbucket", {}, b"{}").action == "other"
    ev = wh.normalize_event("trello", {}, {"action": "notadict"})
    assert ev.action == "other" and ev.raw_type == "malformed"
