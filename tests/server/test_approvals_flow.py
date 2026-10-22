"""Webhook -> worker -> /approvals -> decision -> deterministic merge -> audit chain."""
from __future__ import annotations

from typing import Any

from squadhq.audit import verify_store
from tests.server.conftest import APPROVER, VIEWER, Rig, auth, gh_payload

PR = {"number": 12, "title": "Add search", "html_url": "https://github.com/acme/a/pull/12", "merged": False,
      "head": {"sha": "a" * 40, "ref": "feature", "repo": {"full_name": "acme/a"}},
      "user": {"login": "squadhq-ops[bot]", "type": "Bot"}, "labels": []}
REPO = {"full_name": "acme/a", "default_branch": "main"}


def _label_pr(client: Any, label: str, delivery: str) -> None:
    body, headers = gh_payload("pull_request", delivery=delivery, action="labeled", number=12, pull_request=PR,
                               label={"name": label}, repository=REPO,
                               sender={"login": "squadhq-ops[bot]", "type": "Bot"})
    assert client.post("/api/v1/webhooks/github", content=body, headers=headers).status_code == 202


def test_merge_approval_end_to_end(client: Any, store: Any, rig: Rig) -> None:
    _label_pr(client, "needs-approver-merge", "d-merge")
    rig.worker.run_once()
    items = client.get("/api/v1/approvals", headers=auth(VIEWER)).json()["approvals"]
    assert len(items) == 1
    a = items[0]
    assert (a["kind"], a["project"], a["subject"], a["url"]) == ("merge", "acme/a", "pr#12", PR["html_url"])
    assert client.get(f"/api/v1/approvals/{a['id']}", headers=auth(VIEWER)).json()["status"] == "pending"

    r = client.post(f"/api/v1/approvals/{a['id']}/decision", json={"decision": "approve", "comment": "LGTM"},
                    headers=auth(APPROVER))
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["approval"]["status"] == "approved" and out["approval"]["decided_by"] == "token:approver"
    assert out["audit_event"]["action"] == "approval.approved"
    # deciding twice is a conflict; unknown ids 404; bad bodies 422
    assert client.post(f"/api/v1/approvals/{a['id']}/decision", json={"decision": "reject"},
                       headers=auth(APPROVER)).status_code == 409
    assert client.post("/api/v1/approvals/nope/decision", json={"decision": "reject"},
                       headers=auth(APPROVER)).status_code == 404
    assert client.post(f"/api/v1/approvals/{a['id']}/decision", json={"decision": "maybe"},
                       headers=auth(APPROVER)).status_code == 422

    # the worker performs the merge through the code host (no model involved)
    job = rig.worker.run_once()
    assert job is not None and job.kind == "approval_action"
    assert rig.integrations.host.merged == [("acme/a", "12", "squash")]
    calls = rig.integrations.tracker("github").calls
    assert ("remove_labels", "acme/a", "12", ["needs-approver-merge"]) in calls
    assert any(c[0] == "comment" and "approved" in c[3] and "LGTM" in c[3] for c in calls)
    assert store.get_job(job.id).status == "done"

    assert client.get("/api/v1/approvals", headers=auth(VIEWER)).json()["approvals"] == []
    assert len(client.get("/api/v1/approvals?status=all", headers=auth(VIEWER)).json()["approvals"]) == 1
    actions = [e.action for e in store.iter_audit()]
    assert actions == ["approval.requested", "approval.approved", "merge.performed"]
    assert verify_store(store).ok


def test_proposal_rejection(client: Any, store: Any, rig: Rig) -> None:
    body, headers = gh_payload("issues", delivery="d-prop", action="labeled",
                               issue={"number": 40, "title": "Proposal: dark mode", "state": "open",
                                      "labels": [{"name": "proposal:pending"}], "user": {"login": "bot"},
                                      "html_url": "https://github.com/acme/a/issues/40"},
                               label={"name": "proposal:pending"}, repository=REPO,
                               sender={"login": "squadhq-ops[bot]", "type": "Bot"})
    client.post("/api/v1/webhooks/github", content=body, headers=headers)
    rig.worker.run_once()
    a = store.list_approvals("pending")[0]
    assert (a.kind, a.subject, a.title) == ("proposal", "issue#40", "Proposal: dark mode")
    client.post(f"/api/v1/approvals/{a.id}/decision", json={"decision": "reject", "comment": "not now"},
                headers=auth(APPROVER))
    rig.worker.run_once()
    calls = rig.integrations.tracker("github").calls
    assert ("add_labels", "acme/a", "40", ["proposal:rejected"]) in calls
    assert rig.integrations.host.merged == []
    assert [e.action for e in store.iter_audit()][-1] == "proposal.rejected"


def test_approved_merge_with_red_checks_is_dead_lettered(client: Any, store: Any, rig: Rig) -> None:
    from squadhq.interfaces import CheckRun

    rig.integrations.host.checks = [CheckRun("ci", "completed", "failure")]
    _label_pr(client, "needs-approver-merge", "d-red")
    rig.worker.run_once()
    a = store.list_approvals("pending")[0]
    client.post(f"/api/v1/approvals/{a.id}/decision", json={"decision": "approve"}, headers=auth(APPROVER))
    rig.worker.run_once()
    assert rig.integrations.host.merged == []
    dead = store.list_jobs("dead")
    assert len(dead) == 1 and "checks failing" in dead[0].last_error
