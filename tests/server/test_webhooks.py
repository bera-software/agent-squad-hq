from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from squadhq.server import events as ev_mod
from tests.server.conftest import GH_SECRET, gh_payload, gh_sign

ISSUE = {"number": 7, "title": "Add search", "body": "please", "state": "open", "labels": [{"name": "agent:implement"}],
         "user": {"login": "alice"}, "author_association": "MEMBER", "html_url": "https://github.com/acme/a/issues/7",
         "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z"}
REPO = {"full_name": "acme/a", "default_branch": "main"}


def test_github_signed_delivery_is_enqueued(client: Any, store: Any) -> None:
    body, headers = gh_payload("issues", action="labeled", issue=ISSUE, label={"name": "agent:implement"},
                               repository=REPO, sender={"login": "alice"})
    r = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert r.status_code == 202, r.text
    job = store.get_job(r.json()["job_id"])
    assert job.kind == "webhook_event"
    ev = job.payload["event"]
    assert ev["action"] == "labels_changed" and ev["project"] == "acme/a" and ev["key"] == "7"
    assert ev["labels_added"] == ["agent:implement"]
    assert "raw" not in ev  # the raw payload is not persisted


def test_duplicate_delivery_is_idempotent(client: Any, store: Any) -> None:
    body, headers = gh_payload("issues", action="opened", issue=ISSUE, repository=REPO, sender={"login": "alice"})
    first = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    second = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["job_id"] == second.json()["job_id"]
    assert store.count_jobs()["queued"] == 1


def test_unsigned_and_bad_signatures_rejected(client: Any, store: Any) -> None:
    body, headers = gh_payload("issues", action="opened", issue=ISSUE, repository=REPO)
    unsigned = {k: v for k, v in headers.items() if k != "X-Hub-Signature-256"}
    assert client.post("/api/v1/webhooks/github", content=body, headers=unsigned).status_code == 401
    forged = {**headers, "X-Hub-Signature-256": gh_sign(body, "wrong-secret")}
    assert client.post("/api/v1/webhooks/github", content=body, headers=forged).status_code == 401
    tampered = body.replace(b"Add search", b"Add malware")
    assert client.post("/api/v1/webhooks/github", content=tampered, headers=headers).status_code == 401
    assert store.count_jobs()["queued"] == 0
    m = client.get("/metrics", headers={"Authorization": "Bearer sqh_viewer-token"}).text
    assert 'squadhq_webhooks_total{result="bad_signature",source="github"}' not in m  # label order is fixed
    assert 'squadhq_webhooks_total{source="github",result="bad_signature"} 3' in m


def test_unconfigured_and_unknown_sources(client: Any) -> None:
    assert client.post("/api/v1/webhooks/gitlab", content=b"{}", headers={"X-Gitlab-Token": "x"}).status_code == 401
    assert client.post("/api/v1/webhooks/bitbucket", content=b"{}").status_code == 404


def test_ping_is_acknowledged_not_enqueued(client: Any, store: Any) -> None:
    body, headers = gh_payload("ping", zen="hi", repository=REPO)
    r = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["ping"] is True
    assert store.count_jobs()["queued"] == 0


def test_linear_signature(client: Any, store: Any) -> None:
    payload = {"action": "update", "type": "Issue", "data": {"id": "x", "identifier": "ENG-1", "title": "T",
                                                             "team": {"key": "ENG"}}}
    body = json.dumps(payload).encode()
    sig = hmac.new(b"lin-secret", body, hashlib.sha256).hexdigest()
    r = client.post("/api/v1/webhooks/linear", content=body, headers={"Linear-Signature": sig,
                                                                      "Linear-Delivery": "lin-1"})
    assert r.status_code == 202, r.text
    assert r.json()["delivery_id"] == "lin-1"
    assert client.post("/api/v1/webhooks/linear", content=body, headers={"Linear-Signature": "00"}).status_code == 401


def test_jira_signature(client: Any) -> None:
    body = json.dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "ENG-2", "fields": {}}}).encode()
    sig = "sha256=" + hmac.new(b"jira-secret", body, hashlib.sha256).hexdigest()
    r = client.post("/api/v1/webhooks/jira", content=body,
                    headers={"X-Hub-Signature": sig, "X-Atlassian-Webhook-Identifier": "j-1"})
    assert r.status_code == 202, r.text
    assert client.post("/api/v1/webhooks/jira", content=body).status_code == 401


def test_trello_signature_and_body_hash_delivery_id(client: Any) -> None:
    body = json.dumps({"action": {"id": "a1", "type": "updateCard", "data": {"card": {"id": "c1"}}},
                       "model": {"id": "b1"}}).encode()
    cb = "https://hq.example.com/api/v1/webhooks/trello"
    sig = base64.b64encode(hmac.new(b"trello-secret", body + cb.encode(), hashlib.sha1).digest()).decode()
    r = client.post("/api/v1/webhooks/trello", content=body, headers={"X-Trello-Webhook": sig})
    assert r.status_code == 202, r.text
    assert r.json()["delivery_id"] == "sha256:" + hashlib.sha256(body).hexdigest()


def test_webhook_body_limit(store: Any) -> None:
    from fastapi.testclient import TestClient

    from squadhq.server.app import create_app
    from tests.server.conftest import make_settings

    c = TestClient(create_app(make_settings(webhook_max_body_bytes=50), store=store))
    body, headers = gh_payload("issues", action="opened", issue=ISSUE, repository=REPO)
    assert c.post("/api/v1/webhooks/github", content=body, headers=headers).status_code == 413


def test_verify_fails_closed() -> None:
    assert ev_mod.verify("github", {}, b"{}", "") is False
    assert ev_mod.verify("nope", {}, b"{}", "secret") is False


def test_normalize_push_keeps_shas() -> None:
    body = json.dumps({"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40, "repository": REPO}).encode()
    ev = ev_mod.normalize("github", {"X-GitHub-Event": "push", "X-Hub-Signature-256": gh_sign(body, GH_SECRET)},
                          body)
    assert ev["action"] == "push" and ev["project"] == "acme/a"
    assert ev["raw_subset"] == {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40,
                                "default_branch": "main"}


def test_normalize_handles_garbage() -> None:
    ev = ev_mod.normalize("github", {"X-GitHub-Event": "issues"}, b"not json")
    assert ev["action"] in ("other", "ticket_updated")
    ev2 = ev_mod.normalize("github", {}, b"[1,2]")
    assert "raw_subset" in ev2


def test_helpers() -> None:
    assert ev_mod.delivery_id("github", {"x-github-delivery": "abc"}, b"") == "abc"
    assert ev_mod.delivery_id("github", {"X-GitHub-Delivery": "x" * 300}, b"z").startswith("sha256:")
    assert ev_mod.subject_for({"source": "github", "action": "pr_updated", "key": "3"}) == "pr#3"
    assert ev_mod.subject_for({"source": "jira", "action": "labels_changed", "key": "ENG-3"}) == "ENG-3"
    want, drop = ev_mod.approval_changes({"action": "labels_changed", "labels_added": ["proposal:pending"],
                                          "labels_removed": ["escalation"]})
    assert want == ["proposal"] and drop == ["escalation"]
    want, drop = ev_mod.approval_changes({"action": "ticket_closed", "ticket": {"labels": ["needs-human"]}})
    assert want == [] and drop == ["escalation"]
    assert ev_mod.agent_roles({"labels_added": ["agent:review", "agent:", "bug"]}) == ["review"]
    allowed = ev_mod.DEFAULT_ALLOWED_ASSOCIATIONS
    assert ev_mod.may_trigger({"actor": "x", "association": "MEMBER"}, allowed, set())
    assert not ev_mod.may_trigger({"actor": "x", "association": "NONE"}, allowed, set())
    bot_ev = {"source": "github", "actor": "squadhq-ops[bot]", "association": "NONE",
              "raw_subset": {"sender_type": "Bot"}}
    assert ev_mod.may_trigger(bot_ev, allowed, {"squadhq-ops[bot]"})
    # full login only (no bare slug), sender must be a Bot, and only for GitHub events
    assert not ev_mod.may_trigger({**bot_ev, "actor": "squadhq-ops"}, allowed, {"squadhq-ops[bot]"})
    assert not ev_mod.may_trigger({**bot_ev, "raw_subset": {"sender_type": "User"}}, allowed, {"squadhq-ops[bot]"})
    assert not ev_mod.may_trigger({**bot_ev, "source": "jira"}, allowed, {"squadhq-ops[bot]"})
    assert ev_mod.may_trigger({"actor": "Jane", "association": "NONE"}, allowed, set(), frozenset({"jane"}))
    assert ev_mod.ticket_row({"ticket": None}) is None
    assert ev_mod.flow_state(["agent:implement"], "open") != "open"
