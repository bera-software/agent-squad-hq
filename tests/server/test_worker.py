from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from squadhq.interfaces import CapacityError, CheckRun, ErrorClass, ExecutionResult, FatalError, Ticket
from squadhq.server.integrations import (
    EnvTokenMinter,
    GitHubWorkflowDispatcher,
    SubprocessModuleRunner,
)
from squadhq.server.sandbox import ContainerJob, ContainerOutcome, DockerContainerRunner, FakeContainerRunner
from squadhq.server.worker import Worker, backoff_delay, default_deps, sync_projects
from tests.server.conftest import Rig, make_rig, make_settings


def _event_job(store: Any, ev: dict[str, Any], delivery: str = "d1") -> Any:
    store.enqueue("webhook_event", {"source": ev.get("source", "github"), "delivery_id": delivery, "event": ev})
    return store


def _labeled(label: str, *, actor: str = "alice", assoc: str = "MEMBER", key: str = "7",
             action: str = "labels_changed", source: str = "github", labels: list[str] | None = None) -> dict[str, Any]:
    return {"source": source, "action": action, "project": "acme/a", "key": key, "actor": actor,
            "association": assoc, "raw_type": "issues.labeled", "labels_added": [label], "labels_removed": [],
            "ticket": {"key": key, "project": "acme/a", "title": "Add search", "state": "open",
                       "labels": labels if labels is not None else [label], "url": "https://x/7",
                       "updated_at": "2026-10-02T00:00:00Z"},
            "raw_subset": {}}


def test_webhook_event_dispatch_and_ticket(rig: Rig, store: Any) -> None:
    _event_job(store, _labeled("agent:implement"))
    rig.worker.run_once()
    assert store.list_tickets()[0].key == "7"
    jobs = store.list_jobs("queued")
    assert [j.kind for j in jobs] == ["dispatch_coding"]
    assert jobs[0].payload == {"project": "acme/a", "key": "7", "role": "implement", "tracker": "github"}
    # byo-compute: dispatches the project's workflow
    rig.worker.run_once()
    repo, wf, ref, inputs = rig.dispatcher.calls[0]
    assert (repo, wf, ref) == ("acme/a", "squadhq-agent.yml", "main")
    assert inputs["role"] == "implement" and inputs["ticket"] == "7"
    assert store.count_jobs()["done"] == 2
    actions = [e.action for e in store.iter_audit()]
    assert "job.dispatched" in actions


def test_untrusted_actor_cannot_trigger(rig: Rig, store: Any) -> None:
    _event_job(store, _labeled("agent:implement", actor="mallory", assoc="NONE"))
    rig.worker.run_once()
    assert store.list_jobs("queued") == []
    assert [e.action for e in store.iter_audit()] == ["trigger.refused"]


def test_non_github_tracker_dispatches_on_hub(rig: Rig, store: Any) -> None:
    ev = _labeled("agent:implement", source="jira", actor="jane", assoc="NONE", key="ENG-1")
    rig.worker.deps.trusted_actors = frozenset({"jane"})
    _event_job(store, ev)
    rig.worker.run_once()
    rig.worker.run_once()
    assert rig.dispatcher.calls[0][0] == "acme/agent-squad-hq"
    assert rig.dispatcher.calls[0][3]["tracker"] == "jira"


def test_approval_labels_create_and_resolve(rig: Rig, store: Any) -> None:
    pr_ev = {"source": "github", "action": "pr_updated", "project": "acme/a", "key": "12", "actor": "squadhq-ops[bot]",
             "association": "BOT", "raw_type": "pull_request.labeled", "labels_added": ["needs-approver-merge"],
             "labels_removed": [], "raw_subset": {"title": "Add search", "url": "https://x/pull/12",
                                                  "head_sha": "a" * 40, "head_repo": "acme/a", "is_pr": True,
                                                  "sender_type": "Bot"}}
    _event_job(store, pr_ev, "d1")
    _event_job(store, pr_ev, "d2")  # a re-delivery does not duplicate the approval
    rig.worker.run_once()
    rig.worker.run_once()
    pending = store.list_approvals("pending")
    assert len(pending) == 1
    a = pending[0]
    assert (a.kind, a.subject, a.title, a.url) == ("merge", "pr#12", "Add search", "https://x/pull/12")
    unl = {**pr_ev, "labels_added": [], "labels_removed": ["needs-approver-merge"], "raw_type": "pull_request.unlabeled"}
    _event_job(store, unl, "d3")
    rig.worker.run_once()
    assert store.list_approvals("pending") == []
    assert store.list_approvals("resolved")[0].id == a.id


def test_push_to_default_branch_runs_ticket_transition(rig: Rig, store: Any) -> None:
    ev = {"source": "github", "action": "push", "project": "acme/a", "key": None, "actor": "alice",
          "association": "NONE", "raw_type": "push", "labels_added": [], "labels_removed": [],
          "raw_subset": {"ref": "refs/heads/main", "before": "a1", "after": "b2", "default_branch": "main"}}
    _event_job(store, ev)
    _event_job(store, {**ev, "raw_subset": {**ev["raw_subset"], "ref": "refs/heads/feature"}}, "d2")
    rig.worker.run_once()
    rig.worker.run_once()
    tt = [j for j in store.list_jobs("queued") if j.kind == "ticket_transition"]
    assert len(tt) == 1 and tt[0].payload == {"repo": "acme/a", "before": "a1", "after": "b2"}
    rig.worker.run_once()
    module, args, env = rig.modules.calls[0]
    assert module == "ticket_transition"
    assert args == ["--repo=acme/a", "--before=a1", "--after=b2"]
    assert env["HUB_REPO"] == "acme/agent-squad-hq" and env["GH_TOKEN"] == "test-token"


def test_pr_closed_unmerged_runs_transition(rig: Rig, store: Any) -> None:
    ev = {"source": "github", "action": "pr_closed", "project": "acme/a", "key": "9", "actor": "alice",
          "association": "MEMBER", "raw_type": "pull_request.closed", "labels_added": [], "labels_removed": [],
          "raw_subset": {"merged": False}}
    _event_job(store, ev)
    rig.worker.run_once()
    rig.worker.run_once()
    assert rig.modules.calls[0][1] == ["--repo=acme/a", "--closed-pr=9"]


@pytest.mark.parametrize("kind,payload,module,args", [
    ("sweep", {}, "stuck_sweeper", []),
    ("sweep", {"repo": "acme/a"}, "stuck_sweeper", ["--repo=acme/a"]),
    ("janitor", {"scope": "projects"}, "hub_janitor", ["--scope", "projects"]),
    ("hub_loop", {"mode": "cos"}, "agent_runner", ["cos"]),
])
def test_module_jobs(rig: Rig, store: Any, kind: str, payload: dict[str, Any], module: str, args: list[str]) -> None:
    store.enqueue(kind, payload)
    rig.worker.run_once()
    assert rig.modules.calls == [(module, args, rig.modules.calls[0][2])]
    assert store.count_jobs()["done"] == 1


def test_hub_loop_records_run_and_captures_brief(rig: Rig, store: Any) -> None:
    rig.integrations.tracker("github").tickets = [
        Ticket(key="1", project="acme/agent-squad-hq", title="Daily brief", body="All good", state="open",
               labels=["daily-brief"], author="bot", association="BOT", url="", created_at="2026-10-21", updated_at="")]
    store.enqueue("hub_loop", {"mode": "brief"})
    rig.worker.run_once()
    assert store.list_runs()[0].role == "brief" and store.list_runs()[0].outcome == "success"
    assert store.latest_brief().markdown == "# Daily brief\n\nAll good"


def test_hub_loop_failure_dead_letters_without_retry(rig: Rig, store: Any) -> None:
    rig.modules.returncode = 1
    store.enqueue("hub_loop", {"mode": "cos"})
    rig.worker.run_once()
    assert store.count_jobs()["dead"] == 1
    assert store.list_runs()[0].outcome == "failure"


def test_bad_payloads_are_fatal(rig: Rig, store: Any) -> None:
    store.enqueue("hub_loop", {"mode": "rm"})
    store.enqueue("janitor", {"scope": "everything"})
    store.enqueue("scheduled", {"schedule": "mystery"})
    store.enqueue("dispatch_coding", {"project": "a/b", "key": "1", "role": "../etc"})
    for _ in range(4):
        rig.worker.run_once()
    dead = store.list_jobs("dead")
    assert len(dead) == 4 and all(j.last_error.startswith("fatal:") for j in dead)


def test_retry_backoff_then_dead_letter(store: Any) -> None:
    settings = make_settings(retry_base_s=10, retry_max_s=100)
    r = make_rig(store, settings)
    r.modules.returncode = 2
    jid = store.enqueue("sweep", {}, max_attempts=2).job_id
    r.worker.run_once()
    j = store.get_job(jid)
    assert j.status == "queued" and j.attempts == 1 and "exited 2" in j.last_error
    assert r.worker.run_once() is None  # backoff: not yet due
    with store._tx() as cur:  # make it due
        cur.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (jid,))
    r.worker.run_once()
    assert store.get_job(jid).status == "dead"


def test_capacity_error_waits_max(store: Any) -> None:
    settings = make_settings(retry_max_s=777)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    r = make_rig(store, settings)
    r.worker.clock = lambda: now
    r.worker.handlers["sweep"] = lambda job: (_ for _ in ()).throw(CapacityError("quota"))
    jid = store.enqueue("sweep", {}).job_id
    r.worker.run_once()
    assert store.get_job(jid).run_after == (now + timedelta(seconds=777)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_backoff_delay() -> None:
    assert backoff_delay(1, 10, 1000, lambda: 0.5) == 10
    assert backoff_delay(3, 10, 1000, lambda: 0.5) == 40
    assert backoff_delay(20, 10, 100, lambda: 0.5) == 100
    assert 8 <= backoff_delay(1, 10, 1000, lambda: 0.0) <= 12


# ---------------------------------------------------------------- worker mode

def _worker_rig(store: Any, outcomes: list[ContainerOutcome] | None = None) -> Rig:
    r = make_rig(store, make_settings(mode="worker", executor_image="ghcr.io/acme/exec@sha256:abc"))
    r.worker.deps.containers = r.containers = FakeContainerRunner(outcomes)
    return r


def test_worker_mode_runs_container_and_records_run(store: Any) -> None:
    r = _worker_rig(store)
    store.enqueue("dispatch_coding", {"project": "acme/a", "key": "7", "role": "implement"})
    r.worker.run_once()
    cj = r.containers.jobs[0]
    assert cj.image == "ghcr.io/acme/exec@sha256:abc" and cj.secret_env == {"GH_TOKEN": "ghs_scoped"}
    assert cj.env["SQUADHQ_TICKET"] == "7"
    run = store.list_runs()[0]
    assert (run.outcome, run.model, run.cost_usd) == ("success", "fake-model", 0.01)
    assert store.count_jobs()["done"] == 1


def _outcome(**kw: Any) -> ContainerOutcome:
    base: dict[str, Any] = dict(exit_code=0, timed_out=False, duration_s=1.0, result=None)
    base.update(kw)
    return ContainerOutcome(**base)


def _result(outcome: str, ec: ErrorClass) -> ExecutionResult:
    return ExecutionResult(executor="claude-code", role="implement", model="m", outcome=outcome,  # type: ignore[arg-type]
                           error_class=ec, error_message="boom")


@pytest.mark.parametrize("outcome,status", [
    (_outcome(timed_out=True, exit_code=124), "dead"),
    (_outcome(exit_code=1), "queued"),
    (_outcome(result=_result("failure", ErrorClass.TRANSIENT)), "queued"),
    (_outcome(result=_result("failure", ErrorClass.CAPACITY)), "queued"),
    (_outcome(result=_result("failure", ErrorClass.AGENT)), "done"),
])
def test_worker_mode_failure_policy(store: Any, outcome: ContainerOutcome, status: str) -> None:
    r = _worker_rig(store, [outcome])
    jid = store.enqueue("dispatch_coding", {"project": "acme/a", "key": "7", "role": "implement"}).job_id
    r.worker.run_once()
    assert store.get_job(jid).status == status
    assert len(store.list_runs()) == 1


def test_hub_workflow_both_modes(rig: Rig, store: Any) -> None:
    store.enqueue("hub_workflow", {"workflow": "tech-debt-weekly.yml", "role": "debt_scan"})
    rig.worker.run_once()
    assert rig.dispatcher.calls[0][:2] == ("acme/agent-squad-hq", "tech-debt-weekly.yml")
    w = _worker_rig(store)
    store.enqueue("hub_workflow", {"workflow": "tech-debt-weekly.yml", "role": "debt_scan"})
    w.worker.run_once()
    assert w.containers.jobs[0].role == "debt_scan"


def test_dispatch_error_retries(rig: Rig, store: Any) -> None:
    from squadhq.interfaces import TransientError

    rig.dispatcher.error = TransientError("502")
    jid = store.enqueue("dispatch_coding", {"project": "acme/a", "key": "7", "role": "implement"}).job_id
    rig.worker.run_once()
    assert store.get_job(jid).status == "queued"
    rig.dispatcher.error = FatalError("404 workflow missing")
    with store._tx() as cur:
        cur.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00.000000Z'")
    rig.worker.run_once()
    assert store.get_job(jid).status == "dead"


def test_heartbeat_extends_lease(store: Any) -> None:
    settings = make_settings(worker_lease_s=3.0)  # heartbeat every 1 s
    r = make_rig(store, settings)
    seen: list[str | None] = []

    def slow(job: Any) -> dict[str, Any]:
        import time

        before = store.get_job(job.id).locked_until
        time.sleep(1.3)
        seen.extend([before, store.get_job(job.id).locked_until])
        return {}

    r.worker.handlers["sweep"] = slow
    store.enqueue("sweep", {})
    r.worker.run_once()
    assert seen[1] > seen[0]


def test_unknown_approval_is_fatal(rig: Rig, store: Any) -> None:
    store.enqueue("approval_action", {"approval_id": "missing"})
    rig.worker.run_once()
    assert store.count_jobs()["dead"] == 1


# --------------------------------------------------------- integration seams

def test_docker_argv_is_locked_down() -> None:
    job = ContainerJob(job_id="j" * 32, image="img@sha256:1", role="implement", repo="acme/a",
                       env={"SQUADHQ_ROLE": "implement"}, secret_env={"GH_TOKEN": "ghs_secret"}, command=["run"])
    argv = DockerContainerRunner("docker").build_argv(job, "/tmp/out")
    s = " ".join(argv)
    for flag in ("--read-only", "--security-opt no-new-privileges", "--cap-drop ALL", "--user 10001:10001",
                 "--cpus 2", "--memory 4g", "--pids-limit 512", "--network squadhq-egress", "--rm",
                 "-v /tmp/out:/out:rw"):
        assert flag in s
    assert "ghs_secret" not in s  # secrets are passed by name only
    assert argv[argv.index("GH_TOKEN") - 1] == "-e"
    assert argv[-2:] == ["img@sha256:1", "run"]


def test_docker_runner_reads_result_and_handles_timeout() -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        calls.append({"argv": argv, **kw})
        if argv[1] == "run":
            out = argv[argv.index("-v") + 1].split(":")[0]
            with open(f"{out}/execution-result.json", "w") as fh:
                json.dump({"executor": "claude-code", "role": "implement", "model": "m", "outcome": "success",
                           "cost_usd": 0.3}, fh)
        return subprocess.CompletedProcess(argv, 0, "logs", "")

    r = DockerContainerRunner("docker", run=fake_run)
    job = ContainerJob(job_id="abc", image="img", role="implement", repo="acme/a", secret_env={"GH_TOKEN": "s"})
    out = r.run(job)
    assert out.exit_code == 0 and out.result is not None and out.result.cost_usd == 0.3
    assert calls[0]["env"]["GH_TOKEN"] == "s" and calls[0]["timeout"] == 3600.0

    def timeout_run(argv: list[str], **kw: Any) -> Any:
        if argv[1] == "run":
            raise subprocess.TimeoutExpired(argv, 1)
        calls.append({"argv": argv})
        return subprocess.CompletedProcess(argv, 0, "", "")

    out2 = DockerContainerRunner("podman", run=timeout_run).run(job)
    assert out2.timed_out and out2.exit_code == 124 and out2.result is None
    assert calls[-1]["argv"][:2] == ["podman", "kill"]


def test_github_workflow_dispatcher() -> None:
    from squadhq.http import FakeTransport

    t = FakeTransport().add("POST /actions/workflows/", (204, b""))
    GitHubWorkflowDispatcher("tok", "https://api.example.com", transport=t).dispatch(
        "acme/a", "squadhq-agent.yml", "main", {"role": "implement"})
    call = t.calls[0]
    assert call["url"] == "https://api.example.com/repos/acme/a/actions/workflows/squadhq-agent.yml/dispatches"
    assert call["body"] == {"ref": "main", "inputs": {"role": "implement"}}
    assert call["timeout"] == 30.0
    with pytest.raises(FatalError):
        GitHubWorkflowDispatcher("", transport=t).dispatch("a/b", "w", "main", {})


def test_subprocess_module_runner() -> None:
    r = SubprocessModuleRunner(python=sys.executable)
    with pytest.raises(FatalError):
        r.run("os", [], env={}, timeout_s=5)
    res = r.run("hub_janitor", ["--help"], env={"PYTHONPATH": "src"}, timeout_s=60)
    assert res.returncode == 0 and "--scope" in res.stdout_tail


def test_env_token_minter() -> None:
    m = EnvTokenMinter({"SQUADHQ_JOB_GITHUB_TOKEN": "g", "OPENAI_API_KEY": "o"})
    assert {(t.env_name, t.value) for t in m.mint("a/b", "implement")} == {("GH_TOKEN", "g"), ("OPENAI_API_KEY", "o")}


def test_default_deps_and_sync_projects(store: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("SQUADHQ_TRUSTED_ASSOCIATIONS", raising=False)
    monkeypatch.setenv("SQUADHQ_ALLOWED_ASSOCIATIONS", "owner, member")  # deprecated alias still honoured
    monkeypatch.setenv("SQUADHQ_TRUSTED_ACTORS", "Jane,bob")
    d = default_deps(make_settings())
    assert d.allowed_associations == frozenset({"OWNER", "MEMBER"})
    assert d.trusted_actors == frozenset({"jane", "bob"})
    monkeypatch.setenv("SQUADHQ_TRUSTED_ASSOCIATIONS", "OWNER")  # the shared name wins
    assert default_deps(make_settings()).allowed_associations == frozenset({"OWNER"})
    Worker(store, make_settings())  # default wiring constructs without network
    assert sync_projects(store, [{"name": "A", "repo": "acme/a", "packs": ["x"]}, {"name": "no-repo"}]) == 1
    assert store.list_projects()[0].data == {"packs": ["x"]}


def _merge_approval(store: Any, head_sha: str = "a" * 40) -> Any:
    a, _ = store.upsert_approval(kind="merge", project="acme/a", subject="pr#5", title="t", url="u",
                                 data={"head_sha": head_sha, "head_repo": "acme/a", "code_host": "github"})
    return a


def test_approval_executor_gate(store: Any) -> None:
    from squadhq.interfaces import TransientError
    from squadhq.server.approvals import ApprovalExecutor
    from tests.server.conftest import FakeIntegrations

    integ = FakeIntegrations()
    ex = ApprovalExecutor(store, integ)
    a = _merge_approval(store)
    with pytest.raises(FatalError):
        ex.execute(a)  # still pending
    a, _ = store.decide_approval(a.id, decision="approve", actor="token:lead", comment="ship it", action_job=None)
    host = integ.host
    host.checks = [CheckRun("hub / ci-gate", "in_progress", None)]
    with pytest.raises(TransientError):
        ex.execute(a)
    host.checks = [CheckRun("hub / ci-gate", "completed", "failure")]
    with pytest.raises(FatalError, match="failing"):
        ex.execute(a)
    host.checks = []
    with pytest.raises(FatalError, match="no checks"):
        ex.execute(a)  # zero checks is never green
    host.checks = [CheckRun("lint", "completed", "success")]
    with pytest.raises(FatalError, match="required checks missing"):
        ex.execute(a)
    host.checks = [CheckRun("hub / ci-gate", "completed", "success"), CheckRun("lint", "completed", "skipped")]
    host.draft = True
    with pytest.raises(FatalError, match="draft"):
        ex.execute(a)
    host.draft, host.pr_state = False, "closed"
    with pytest.raises(FatalError, match="closed"):
        ex.execute(a)
    host.pr_state = "merged"
    assert ex.execute(a).performed == "merge.noop"
    host.pr_state = "open"
    assert ex.execute(a).performed == "merge.performed"
    assert host.merged == [("acme/a", "5", "squash")]
    assert host.merged_sha == ["a" * 40]  # the host rejects a moved head
    assert host.checks_ref[-1] == "a" * 40  # checks are read by commit SHA, never by branch name


def test_merge_refuses_moved_head_and_forks(store: Any) -> None:
    from squadhq.server.approvals import ApprovalExecutor
    from tests.server.conftest import FakeIntegrations

    integ = FakeIntegrations()
    a = _merge_approval(store)
    a, _ = store.decide_approval(a.id, decision="approve", actor="token:lead", comment="", action_job=None)
    integ.host.head_sha = "b" * 40
    with pytest.raises(FatalError, match="head moved"):
        ApprovalExecutor(store, integ).execute(a)
    integ.host.head_sha, integ.host.fork = "a" * 40, True
    with pytest.raises(FatalError, match="fork"):
        ApprovalExecutor(store, integ).execute(a)
    assert ApprovalExecutor(store, integ, allow_fork_merges=True).execute(a).performed == "merge.performed"
    b, _ = store.upsert_approval(kind="merge", project="acme/a", subject="pr#6", title="t", url="u", data={})
    b, _ = store.decide_approval(b.id, decision="approve", actor="x", comment="", action_job=None)
    with pytest.raises(FatalError, match="no pinned head"):
        ApprovalExecutor(store, integ).execute(b)
    assert integ.host.merged == [("acme/a", "5", "squash")]


def test_merge_requires_head_extension(store: Any) -> None:
    from squadhq.server.approvals import current_head

    with pytest.raises(FatalError, match="cannot report"):
        current_head(object(), "a/b", "1")


def test_merge_approval_without_payload_head_is_refused(rig: Rig, store: Any) -> None:
    ev = {"source": "github", "action": "pr_updated", "project": "acme/a", "key": "12", "actor": "squadhq-ops[bot]",
          "association": "BOT", "raw_type": "pull_request.labeled", "labels_added": ["needs-approver-merge"],
          "labels_removed": [], "raw_subset": {"title": "t", "is_pr": True, "sender_type": "Bot"}}
    _event_job(store, ev)
    rig.worker.run_once()
    assert store.list_approvals("pending") == []  # never pinned to "whatever the head is now"
    assert [e.action for e in store.iter_audit()] == ["approval.request_refused"]


def test_unregistered_project_is_dropped(rig: Rig, store: Any) -> None:
    ev = _labeled("agent:implement")
    ev["project"] = "evil/repo"
    ev["ticket"]["project"] = "evil/repo"
    _event_job(store, {**ev, "labels_added": ["agent:implement", "needs-approver-merge"]})
    rig.worker.run_once()
    assert store.list_jobs("queued") == [] and store.list_approvals("pending") == [] and store.list_tickets() == []
    [e] = list(store.iter_audit())
    assert e.action == "webhook.dropped_unregistered" and e.subject == "github:evil/repo"


def test_job_errors_are_redacted(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("CUSTOM_API_TOKEN", "super-secret-value-123")
    r = make_rig(store, make_settings(github_token="plain-gh-token-value"))
    leak = ("auth failed ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 sk-ant-api03-abcdefghijkl "
            "Bearer eyJhbGciOiJIUzI1NiJ9.x.y AKIAABCDEFGHIJKLMNOP xoxb-1234567890-abc "
            "plain-gh-token-value super-secret-value-123 sqh_abcdefghijklmnopqrstuv")

    def boom(job: Any) -> dict[str, Any]:
        raise FatalError(leak)

    r.worker.handlers["sweep"] = boom
    jid = store.enqueue("sweep", {}).job_id
    r.worker.run_once()
    err = store.get_job(jid).last_error
    for secret in ("ghp_", "sk-ant-", "eyJhbG", "AKIA", "xoxb-", "plain-gh-token", "super-secret", "sqh_abc"):
        assert secret not in err, secret
    assert "[REDACTED]" in err


def test_container_log_tail_redacted() -> None:
    def fake_run(argv: list[str], **kw: Any) -> Any:
        return subprocess.CompletedProcess(argv, 1, "token=ghs_scoped_secret_value_long and ghs_ABCDEFGHIJKLMNOPQRST12", "")

    out = DockerContainerRunner("docker", run=fake_run).run(
        ContainerJob(job_id="abc", image="img", role="implement", repo="acme/a",
                     secret_env={"GH_TOKEN": "ghs_scoped_secret_value_long"}))
    assert "ghs_" not in out.logs_tail and "[REDACTED]" in out.logs_tail


def test_sandbox_out_dir_is_private(tmp_path: Any) -> None:
    import os
    import stat

    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> Any:
        out = argv[argv.index("-v") + 1].split(":")[0]
        seen["out"], seen["argv"] = out, argv
        seen["out_mode"] = stat.S_IMODE(os.stat(out).st_mode)
        seen["parent_mode"] = stat.S_IMODE(os.stat(os.path.dirname(out)).st_mode)
        return subprocess.CompletedProcess(argv, 0, "", "")

    job = ContainerJob(job_id="abc", image="img", role="implement", repo="acme/a")
    DockerContainerRunner("docker", workdir=str(tmp_path), run=fake_run).run(job)
    assert seen["out_mode"] == 0o700 and seen["parent_mode"] == 0o700
    user = seen["argv"][seen["argv"].index("--user") + 1]
    if os.geteuid() not in (0, 10001):  # not root: the container runs as the dir's owner
        assert user == f"{os.geteuid()}:{os.getegid()}"
    assert not os.path.exists(seen["out"])  # removed after the run
