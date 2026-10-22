"""Carry out a human approval decision — deterministic code only.

A merge is performed here, after policy checks, through
:meth:`CodeHost.merge`. No model is consulted at any step
(docs/adr/0005-agents-never-merge-or-deploy.md):

* the approval pins the PR's head commit (``data.head_sha``) when it is
  requested; the PR's *current* head must still be that commit — a push
  after the request invalidates the approval;
* PRs whose head lives in a fork are refused unless explicitly allowed
  (``SQUADHQ_ALLOW_FORK_MERGES``);
* the PR must still be open and not a draft;
* checks are read **by commit SHA** (never by branch name, which a fork
  could reuse); there must be at least one, every configured required
  check (``SQUADHQ_REQUIRED_CHECKS``) must be present, and all must be
  completed and green (``success``/``neutral``/``skipped``). Pending ->
  retry later (``TransientError``), failing/missing -> refuse (``FatalError``);
* the merge call carries ``sha=`` so the host itself rejects a moved head;
* the approval label is removed and a comment records who decided.

Proposal, deploy and escalation decisions swap labels and comment.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from squadhq.interfaces import FatalError, TransientError
from squadhq.server.integrations import Integrations
from squadhq.store.base import Approval, StateStore

GREEN = {"success", "neutral", "skipped"}

# kind -> (labels to remove, label added on approve, label added on reject)
LABELS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "merge": (("needs-approver-merge",), "", "merge:rejected"),
    "proposal": (("proposal:pending",), "proposal:approved", "proposal:rejected"),
    "deploy": (("deploy:pending", "needs-approver-deploy"), "deploy:approved", "deploy:rejected"),
    "escalation": (("escalation", "needs-human"), "", ""),
}


@dataclass
class ActionOutcome:
    performed: str
    detail: dict[str, Any]


def _number(subject: str) -> str:
    return subject.split("#", 1)[1] if "#" in subject else subject


DEFAULT_REQUIRED_CHECKS = ("hub / ci-gate",)


def current_head(host: Any, repo: str, number: str) -> Any:
    """``PullHead`` via the optional ``pull_request_head`` extension; fail closed without it."""
    fn = getattr(host, "pull_request_head", None)
    if not callable(fn):
        raise FatalError(f"code host {getattr(host, 'name', '?')} cannot report a PR head SHA; refusing to merge")
    return fn(repo, number)


def check_gate(checks: list[Any], required: tuple[str, ...], key: str) -> None:
    """Raise unless the checks on the pinned commit allow a merge."""
    if not checks:
        raise FatalError(f"no checks reported for the head commit of PR {key}; refusing to merge")
    names = {c.name for c in checks}
    missing = [r for r in required if r not in names]
    pending = [c.name for c in checks if c.status != "completed"]
    failing = [c.name for c in checks if c.status == "completed" and (c.conclusion or "") not in GREEN]
    if failing:
        raise FatalError(f"checks failing on PR {key}: {', '.join(sorted(failing))}")
    if pending:
        raise TransientError(f"checks still running on PR {key}: {', '.join(sorted(pending))}")
    if missing:
        raise FatalError(f"required checks missing on PR {key}: {', '.join(sorted(missing))}")


GITHUB_ACTIONS_APP_ID = 15368


def verify_check_provenance(host: Any, repo: str, sha: str, required: tuple[str, ...], app_slug: str,
                            workflow: str, key: str) -> None:
    """Each required check must come from ``app_slug`` (GitHub Actions) and, if set, from ``workflow``.

    Any GitHub App can create a check run with any name, so a name match alone
    proves nothing. Hosts without the ``check_runs_detail`` extension (GitLab)
    are matched by name only — protect those with branch rules.
    """
    fn = getattr(host, "check_runs_detail", None)
    if not required or not callable(fn) or not app_slug:
        return
    details = fn(repo, sha)
    for name in required:
        own = [d for d in details if d.name == name and (
            d.app_slug == app_slug or (app_slug == "github-actions" and d.app_id == GITHUB_ACTIONS_APP_ID))]
        if not own:
            raise FatalError(f"required check {name!r} on PR {key} was not reported by {app_slug}; refusing to merge")
        if workflow:
            path_fn = getattr(host, "workflow_run_path", None)
            if not callable(path_fn):
                raise FatalError("code host cannot report the workflow of a check; refusing to merge")
            paths = {path_fn(repo, d.workflow_run_id) for d in own if d.workflow_run_id}
            if workflow not in paths:
                raise FatalError(f"required check {name!r} on PR {key} did not come from {workflow}; refusing to merge")


class ApprovalExecutor:
    def __init__(self, store: StateStore, integrations: Integrations, *, merge_method: str = "squash",
                 required_checks: tuple[str, ...] = DEFAULT_REQUIRED_CHECKS, allow_fork_merges: bool = False,
                 required_checks_app: str = "github-actions", required_checks_workflow: str = "") -> None:
        self.store = store
        self.integrations = integrations
        self.merge_method = merge_method
        self.required_checks = required_checks
        self.allow_fork_merges = allow_fork_merges
        self.required_checks_app = required_checks_app
        self.required_checks_workflow = required_checks_workflow

    def verify_head(self, approval: Approval) -> Any:
        """The PR's current head, if it still matches the pinned commit and is not a disallowed fork."""
        host = self.integrations.code_host(str(approval.data.get("code_host") or "github"))
        key = _number(approval.subject)
        pinned = str(approval.data.get("head_sha") or "")
        if not pinned:
            raise FatalError(f"approval {approval.id} has no pinned head commit; re-request the approval")
        head = current_head(host, approval.project, key)
        if head.sha != pinned:
            raise FatalError(f"PR {key} head moved ({pinned[:12]} -> {head.sha[:12]}) after the approval was "
                             "requested; re-request the approval")
        if head.fork and not self.allow_fork_merges:
            raise FatalError(f"PR {key} comes from a fork ({head.repo or 'deleted'}); fork merges are disabled")
        return head

    def execute(self, approval: Approval) -> ActionOutcome:
        tracker_name = str(approval.data.get("tracker") or "github")
        host_name = str(approval.data.get("code_host") or "github")
        key = _number(approval.subject)
        decision = "approve" if approval.status == "approved" else "reject"
        if approval.status not in ("approved", "rejected"):
            raise FatalError(f"approval {approval.id} is {approval.status}, nothing to do")
        remove, on_approve, on_reject = LABELS.get(approval.kind, ((), "", ""))
        detail: dict[str, Any] = {"kind": approval.kind, "decision": decision, "project": approval.project,
                                  "key": key}

        if approval.kind == "merge" and decision == "approve":
            host = self.integrations.code_host(host_name)
            pr = host.get_pull_request(approval.project, key)
            if pr.state == "merged":
                detail["note"] = "already merged"
                performed = "merge.noop"
            else:
                if pr.state != "open":
                    raise FatalError(f"PR {key} is {pr.state}; refusing to merge")
                if pr.draft:
                    raise FatalError(f"PR {key} is a draft; refusing to merge")
                head = self.verify_head(approval)
                checks = host.list_checks(approval.project, head.sha)
                check_gate(checks, self.required_checks, key)
                verify_check_provenance(host, approval.project, head.sha, self.required_checks,
                                        self.required_checks_app, self.required_checks_workflow, key)
                merge: Any = host.merge
                merge(approval.project, key, method=self.merge_method, sha=head.sha)
                performed = "merge.performed"
                detail["checks"] = len(checks)
                detail["head_sha"] = head.sha
        else:
            performed = f"{approval.kind}.{'approved' if decision == 'approve' else 'rejected'}"

        tracker = self.integrations.tracker(tracker_name)
        if remove:
            tracker.remove_labels(approval.project, key, list(remove))
        add = on_approve if decision == "approve" else on_reject
        if add:
            tracker.add_labels(approval.project, key, [add])
        note = f"\n\n> {approval.decision_comment}" if approval.decision_comment else ""
        verb = {"approve": "approved", "reject": "rejected"}[decision]
        tracker.comment(approval.project, key,
                        f"**{approval.kind.capitalize()} {verb}** by `{approval.decided_by}` via Agent Squad HQ."
                        f"{note}")
        self.store.append_audit("squadhq-worker", performed, f"{approval.project}:{approval.subject}",
                                {**detail, "approval_id": approval.id, "decided_by": approval.decided_by})
        return ActionOutcome(performed=performed, detail=detail)
