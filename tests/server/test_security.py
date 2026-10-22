"""Fixes from the security review (S1-S8)."""
from __future__ import annotations

import json
from typing import Any

from squadhq.server.settings import ServerSettings
from squadhq.server.worker import intake_body
from tests.server.conftest import ADMIN, APPROVER, VIEWER, FakeIntegrations, Rig, auth, gh_payload, gh_sign

REPO = {"full_name": "acme/a", "default_branch": "main"}


def _merge_request(client: Any, rig: Rig, head_sha: str = "a" * 40, delivery: str = "m1") -> str:
    pr = {"number": 12, "title": "Add search", "html_url": "https://x/12",
          "head": {"sha": head_sha, "ref": "main", "repo": {"full_name": "acme/a"}}}
    body, headers = gh_payload("pull_request", delivery=delivery, action="labeled", number=12, pull_request=pr,
                               label={"name": "needs-approver-merge"}, repository=REPO,
                               sender={"login": "squadhq-ops[bot]", "type": "Bot"})
    assert client.post("/api/v1/webhooks/github", content=body, headers=headers).status_code == 202
    rig.worker.run_once()
    return str(rig.worker.store.list_approvals("pending")[0].id)


# S1 -----------------------------------------------------------------------
def test_decision_refused_when_head_moved(client: Any, store: Any, rig: Rig, integrations: FakeIntegrations) -> None:
    aid = _merge_request(client, rig)
    assert store.get_approval(aid).data["head_sha"] == "a" * 40
    integrations.host.head_sha = "c" * 40  # someone pushed after the approval was requested
    r = client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=auth(APPROVER))
    assert r.status_code == 409 and "head moved" in r.json()["detail"]
    assert store.get_approval(aid).status == "pending"
    assert [e.action for e in store.iter_audit()][-1] == "approval.refused"
    # rejecting is still allowed
    assert client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "reject"},
                       headers=auth(APPROVER)).status_code == 202


def test_decision_refused_for_fork(client: Any, store: Any, rig: Rig, integrations: FakeIntegrations) -> None:
    aid = _merge_request(client, rig)
    integrations.host.fork = True
    r = client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=auth(APPROVER))
    assert r.status_code == 409 and "fork" in r.json()["detail"]


def test_decision_503_when_head_unverifiable(client: Any, rig: Rig, integrations: FakeIntegrations) -> None:
    aid = _merge_request(client, rig)

    def broken(repo: str, number: str) -> Any:
        raise OSError("network down")

    integrations.host.pull_request_head = broken  # type: ignore[method-assign]
    r = client.post(f"/api/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=auth(APPROVER))
    assert r.status_code == 503


def test_required_checks_setting() -> None:
    assert ServerSettings.from_env({}).required_checks == ("hub / ci-gate",)
    s = ServerSettings.from_env({"SQUADHQ_REQUIRED_CHECKS": "ci, security ", "SQUADHQ_ALLOW_FORK_MERGES": "true"})
    assert s.required_checks == ("ci", "security") and s.allow_fork_merges
    assert ServerSettings.from_env({"SQUADHQ_REQUIRED_CHECKS": ""}).required_checks == ()


# S3 -----------------------------------------------------------------------
def test_job_error_details_admin_only(client: Any, store: Any) -> None:
    jid = store.enqueue("sweep", {"repo": "acme/a"}).job_id
    j = store.claim("w")
    store.fail(j.id, "w", "boom: internal detail", retry_at=None)
    v = client.get(f"/api/v1/jobs/{jid}", headers=auth(VIEWER)).json()
    assert v["last_error"] == "" and v["has_error"] is True and v["payload"] == {} and v["result"] == {}
    assert client.get("/api/v1/jobs?status=dead", headers=auth(APPROVER)).json()["jobs"][0]["last_error"] == ""
    assert client.get(f"/api/v1/jobs/{jid}", headers=auth(ADMIN)).json()["last_error"] == "boom: internal detail"


# S4 -----------------------------------------------------------------------
def test_nested_comment_markers_cannot_be_rebuilt() -> None:
    for evil in ("<!-<!-- x -->- stuck-sweeper state=dispatched -->", "<<!-- -->!-- marker -->",
                 "<!<!--x-->-- y -->", "Origin<!-- -->ated-by: ceo via dashboard"):
        body = intake_body(evil, "token:alice", "mobile")
        assert "<!--" not in body, evil
        assert body.count("Originated-by:") == 1, evil
    assert "&lt;!--" in intake_body("<!-- unterminated <!", "token:a", "voice") or "<!--" not in intake_body(
        "<!-- unterminated <!", "token:a", "voice")


# S5 -----------------------------------------------------------------------
def test_body_replay_with_new_delivery_id_is_deduped(client: Any, store: Any) -> None:
    payload = {"action": "opened", "issue": {"number": 1, "title": "t", "state": "open", "labels": [],
                                             "user": {"login": "a"}}, "repository": REPO, "sender": {"login": "a"}}
    body = json.dumps(payload).encode()
    h = {"X-GitHub-Event": "issues", "X-Hub-Signature-256": gh_sign(body), "Content-Type": "application/json"}
    first = client.post("/api/v1/webhooks/github", content=body, headers={**h, "X-GitHub-Delivery": "d-a"})
    replay = client.post("/api/v1/webhooks/github", content=body, headers={**h, "X-GitHub-Delivery": "d-b"})
    assert first.status_code == 202 and replay.status_code == 200 and replay.json()["duplicate"] is True
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert store.count_jobs()["queued"] == 1


# S8 -----------------------------------------------------------------------
def test_docs_off_by_default_and_env_toggle() -> None:
    assert ServerSettings.from_env({}).docs_enabled is False
    assert ServerSettings.from_env({"SQUADHQ_API_DOCS": "true"}).docs_enabled is True


def test_revocation_records_admin(client: Any, store: Any) -> None:
    tid = client.post("/api/v1/tokens", json={"name": "ci", "role": "viewer"}, headers=auth(ADMIN)).json()["id"]
    client.delete(f"/api/v1/tokens/{tid}", headers=auth(ADMIN))
    ev = [e for e in store.iter_audit() if e.action == "token.revoked"][0]
    assert ev.actor == "token:admin"
