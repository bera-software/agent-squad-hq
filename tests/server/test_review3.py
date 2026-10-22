"""Security review 3: approval re-pinning, check provenance, run bounds, device takeover, bot-login scope."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from squadhq.codehost.github import CheckDetail, GitHubCodeHost
from squadhq.http import FakeTransport
from squadhq.interfaces import CheckRun, FatalError
from squadhq.server.approvals import ApprovalExecutor, verify_check_provenance
from tests.server.conftest import ADMIN, APPROVER, VIEWER, FakeIntegrations, Rig, auth, gh_payload

REPO = {"full_name": "acme/a", "default_branch": "main"}
BOT = {"login": "squadhq-ops[bot]", "type": "Bot"}
HEAD = {"sha": "a" * 40, "ref": "feat", "repo": {"full_name": "acme/a"}}


def _pr_label(client: Any, delivery: str, sender: dict[str, Any] = BOT, head: dict[str, Any] = HEAD) -> None:
    pr = {"number": 12, "title": "Add search", "html_url": "https://x/12", "head": head}
    body, h = gh_payload("pull_request", delivery=delivery, action="labeled", number=12, pull_request=pr,
                         label={"name": "needs-approver-merge"}, repository=REPO, sender=sender)
    assert client.post("/api/v1/webhooks/github", content=body, headers=h).status_code == 202


# 1 -------------------------------------------------------------------------
def test_comment_on_pr_cannot_create_or_repin_merge_approval(client: Any, store: Any, rig: Rig) -> None:
    issue = {"number": 12, "title": "Add search", "state": "open", "pull_request": {"url": "x"},
             "labels": [{"name": "needs-approver-merge"}], "user": {"login": "mallory"}}
    body, h = gh_payload("issue_comment", delivery="c1", action="created", issue=issue,
                         comment={"body": "hi", "user": {"login": "mallory"}, "author_association": "NONE"},
                         repository=REPO, sender={"login": "mallory"})
    client.post("/api/v1/webhooks/github", content=body, headers=h)
    rig.worker.run_once()
    assert store.list_approvals("pending") == []  # never derived from current labels

    _pr_label(client, "l1")
    rig.worker.run_once()
    [a] = store.list_approvals("pending")
    assert a.subject == "pr#12" and a.data["head_sha"] == "a" * 40
    client.post("/api/v1/webhooks/github", content=body, headers={**h, "X-GitHub-Delivery": "c2"})
    rig.worker.run_once()
    assert [x.id for x in store.list_approvals("pending")] == [a.id]  # no second approval, no re-pin


def test_pr_comment_subject_is_canonical() -> None:
    from squadhq.server import events as ev_mod

    ev = {"source": "github", "action": "comment_created", "key": "12", "raw_subset": {"is_pr": True}}
    assert ev_mod.subject_for(ev) == "pr#12"
    assert ev_mod.subject_for({**ev, "raw_subset": {}}) == "issue#12"
    # merge is never requested from an issue label event, even with the label present
    want, _ = ev_mod.approval_changes({"source": "github", "action": "labels_changed", "key": "3",
                                       "labels_added": ["needs-approver-merge"], "raw_subset": {}})
    assert want == []


def test_untrusted_labeler_cannot_request_approval(client: Any, store: Any, rig: Rig) -> None:
    _pr_label(client, "u1", sender={"login": "mallory"})
    rig.worker.run_once()
    assert store.list_approvals("pending") == []
    assert [e.action for e in store.iter_audit()][-1] == "approval.request_refused"


def test_synchronize_supersedes_pending_merge_approval(client: Any, store: Any, rig: Rig) -> None:
    _pr_label(client, "s1")
    rig.worker.run_once()
    [a] = store.list_approvals("pending")
    pr = {"number": 12, "title": "Add search", "html_url": "https://x/12", "head": {**HEAD, "sha": "b" * 40}}
    body, h = gh_payload("pull_request", delivery="s2", action="synchronize", number=12, pull_request=pr,
                         repository=REPO, sender={"login": "mallory"})
    client.post("/api/v1/webhooks/github", content=body, headers=h)
    rig.worker.run_once()
    assert store.list_approvals("pending") == []
    assert store.get_approval(a.id).status == "resolved"
    ev = [e for e in store.iter_audit() if e.action == "approval.superseded"][0]
    assert ev.data["head_sha"] == "b" * 40 and ev.subject == "acme/a:pr#12"


# 4 -------------------------------------------------------------------------
@pytest.mark.parametrize("patch", [
    {"cost_usd": float("inf")}, {"cost_usd": 1e300}, {"cost_usd": 10_000.01}, {"duration_s": float("nan")},
    {"input_tokens": 10**12}, {"started_at": "yesterday"}, {"extra": {"blob": "x" * 300_000}},
])
def test_run_report_bounds(client: Any, patch: dict[str, Any]) -> None:
    body = {"project": "acme/a", "role": "implement", "outcome": "success", **patch}
    import json as _json

    r = client.post("/api/v1/runs", content=_json.dumps(body, allow_nan=True),
                    headers={**auth(ADMIN), "Content-Type": "application/json"})
    assert r.status_code == 422, (patch, r.status_code)


def test_run_started_at_window(client: Any) -> None:
    now = datetime.now(timezone.utc)
    base = {"project": "acme/a", "role": "implement", "outcome": "success"}
    old = (now - timedelta(days=30)).isoformat()
    assert client.post("/api/v1/runs", json={**base, "started_at": old}, headers=auth(ADMIN)).status_code == 422
    future = (now + timedelta(days=1)).isoformat()
    assert client.post("/api/v1/runs", json={**base, "started_at": future}, headers=auth(ADMIN)).status_code == 422
    ok = client.post("/api/v1/runs", json={**base, "run_id": "w", "started_at": (now - timedelta(hours=2)).isoformat()},
                     headers=auth(ADMIN))
    assert ok.status_code == 201 and ok.json()["started_at"].endswith("Z")
    c = client.get("/api/v1/costs", headers=auth(VIEWER)).json()
    assert isinstance(c["total_usd"], float)


# 5 -------------------------------------------------------------------------
class ProvenanceHost:
    def __init__(self, details: list[CheckDetail], paths: dict[str, str]) -> None:
        self.details, self.paths = details, paths

    def check_runs_detail(self, repo: str, sha: str) -> list[CheckDetail]:
        return self.details

    def workflow_run_path(self, repo: str, run_id: str) -> str:
        return self.paths.get(run_id, "")


def _d(app: str, run: str = "1", app_id: int | None = None) -> CheckDetail:
    return CheckDetail("hub / ci-gate", "completed", "success", app, app_id, run)


def test_required_check_must_come_from_actions_and_workflow() -> None:
    req = ("hub / ci-gate",)
    wf = ".github/workflows/agents.yml"
    with pytest.raises(FatalError, match="not reported by github-actions"):
        verify_check_provenance(ProvenanceHost([_d("evil-app")], {}), "a/b", "s", req, "github-actions", wf, "1")
    with pytest.raises(FatalError, match="did not come from"):
        verify_check_provenance(ProvenanceHost([_d("github-actions", "9")], {"9": ".github/workflows/evil.yml"}),
                                "a/b", "s", req, "github-actions", wf, "1")
    verify_check_provenance(ProvenanceHost([_d("evil-app"), _d("github-actions", "7")], {"7": wf}),
                            "a/b", "s", req, "github-actions", wf, "1")
    verify_check_provenance(ProvenanceHost([_d("", "7", 15368)], {"7": wf}), "a/b", "s", req, "github-actions", wf, "1")
    verify_check_provenance(object(), "a/b", "s", req, "github-actions", wf, "1")  # GitLab etc.: name only


def test_executor_enforces_provenance(store: Any) -> None:
    integ = FakeIntegrations()
    integ.host.check_runs_detail = lambda repo, sha: [_d("evil-app")]  # type: ignore[attr-defined]
    a, _ = store.upsert_approval(kind="merge", project="acme/a", subject="pr#5", title="t", url="u",
                                 data={"head_sha": "a" * 40, "code_host": "github"})
    a, _ = store.decide_approval(a.id, decision="approve", actor="x", comment="", action_job=None)
    integ.host.checks = [CheckRun("hub / ci-gate", "completed", "success")]
    with pytest.raises(FatalError, match="not reported by"):
        ApprovalExecutor(store, integ, required_checks_workflow="").execute(a)
    assert integ.host.merged == []


def test_github_check_runs_detail_and_workflow_path() -> None:
    t = FakeTransport().add("GET /check-runs", (200, {"check_runs": [
        {"name": "hub / ci-gate", "status": "completed", "conclusion": "success",
         "app": {"slug": "github-actions", "id": 15368},
         "details_url": "https://github.com/acme/a/actions/runs/4242/job/1"}]}))
    t.add("GET /actions/runs/4242", (200, {"path": ".github/workflows/agents.yml"}))
    host = GitHubCodeHost("tok", transport=t)
    [d] = host.check_runs_detail("acme/a", "a" * 40)
    assert (d.app_slug, d.app_id, d.workflow_run_id) == ("github-actions", 15368, "4242")
    assert host.workflow_run_path("acme/a", "4242") == ".github/workflows/agents.yml"


# 8 -------------------------------------------------------------------------
def test_device_token_takeover_refused(client: Any, store: Any) -> None:
    body = {"expo_push_token": "ExponentPushToken[victim]", "platform": "ios"}
    first = client.post("/api/v1/devices", json=body, headers=auth(VIEWER))
    assert first.status_code == 201
    r = client.post("/api/v1/devices", json={**body, "topics": ["brief"]}, headers=auth(APPROVER))
    assert r.status_code == 409
    assert store.get_device(first.json()["id"]).owner == "token-id:static:viewer"
    assert client.post("/api/v1/devices", json=body, headers=auth(VIEWER)).status_code == 201  # owner may update
    assert client.post("/api/v1/devices", json=body, headers=auth(ADMIN)).status_code == 201  # admin may reassign


def test_device_ownership_keyed_on_token_id(client: Any) -> None:
    a = client.post("/api/v1/tokens", json={"name": "phone", "role": "viewer"}, headers=auth(ADMIN)).json()["token"]
    b = client.post("/api/v1/tokens", json={"name": "phone", "role": "viewer"}, headers=auth(ADMIN)).json()["token"]
    did = client.post("/api/v1/devices", json={"expo_push_token": "ExponentPushToken[a]", "platform": "ios"},
                      headers=auth(a)).json()["id"]
    assert client.delete(f"/api/v1/devices/{did}", headers=auth(b)).status_code == 403  # same name, other token
    assert client.delete(f"/api/v1/devices/{did}", headers=auth(a)).status_code == 200
