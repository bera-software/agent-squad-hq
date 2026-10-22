"""Queue worker: maps jobs onto control-plane actions.

Delivery is at-least-once: a job is leased (``locked_until``), the lease is
renewed by a heartbeat while the handler runs, and a crashed worker's job is
re-claimed when the lease expires. Handlers are therefore idempotent, and
jobs they fan out carry idempotency keys.

Failure policy:

* :class:`~squadhq.interfaces.FatalError` -> dead-letter immediately;
* :class:`~squadhq.interfaces.CapacityError` -> retry after ``retry_max_s``;
* anything else -> exponential backoff with jitter, dead-letter after
  ``max_attempts``. Dead jobs are listed at ``GET /api/v1/jobs?status=dead``
  and re-queued with ``POST /api/v1/jobs/{id}/retry``.

Job kinds:

=================  ==========================================================
``webhook_event``  update tickets/approvals, fan out transitions + dispatches
``sweep``          ``python -m squadhq.stuck_sweeper [--repo R]``
``ticket_transition`` ``python -m squadhq.ticket_transition --repo R ...``
``janitor``        ``python -m squadhq.hub_janitor --scope hub|projects``
``hub_loop``       ``python -m squadhq.agent_runner <mode>`` (cos, brief, ...)
``hub_workflow``   a scheduled hub workflow (dispatch, or a container in worker mode)
``dispatch_coding`` coding job: ``workflow_dispatch`` (byo-compute) or container (worker)
``approval_action`` carry out a human decision (:mod:`squadhq.server.approvals`)
``intake_ticket``  create the tracker ticket for a ``POST /requests`` intake
``push_notify``    Expo push to registered devices (:mod:`squadhq.notify.expo_push`)
=================  ==========================================================
"""
from __future__ import annotations

import logging
import os
import random
import re
import socket
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from squadhq import account_pool
from squadhq.interfaces import CapacityError, ErrorClass, FatalError, Notification, TransientError
from squadhq.server import accounts as pool_mod
from squadhq.server import events as ev_mod
from squadhq.server.approvals import ApprovalExecutor
from squadhq.server.integrations import (
    DefaultIntegrations,
    EnvTokenMinter,
    GitHubWorkflowDispatcher,
    Integrations,
    ModuleRunner,
    SubprocessModuleRunner,
    TokenMinter,
    WorkflowDispatcher,
)
from squadhq.server.observability import job_id_var, tracing
from squadhq.server.push import enqueue_push, notification_from
from squadhq.server.redact import redact, secret_values
from squadhq.server.sandbox import ContainerJob, ContainerRunner, DockerContainerRunner
from squadhq.server.settings import ServerSettings
from squadhq.store.base import Job, Run, StateStore, TicketRow, ts, utcnow

log = logging.getLogger("squadhq.worker")

Handler = Callable[[Job], dict[str, Any]]


def backoff_delay(attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    """``base * 2**(attempt-1)`` capped, with +/-20 % jitter."""
    raw = min(cap, base * (2.0 ** max(0, attempt - 1)))
    return max(0.0, raw * (0.8 + 0.4 * rng()))


def _bot_logins() -> set[str]:
    # Ops and agent logins: a comment from either never triggers a job.
    try:
        from squadhq.settings import automation_logins

        return automation_logins()
    except Exception:  # pragma: no cover - defensive
        return set()


@dataclass
class WorkerDeps:
    integrations: Integrations
    modules: ModuleRunner
    dispatcher: WorkflowDispatcher
    containers: ContainerRunner
    tokens: TokenMinter
    allowed_associations: frozenset[str] = field(default_factory=lambda: ev_mod.DEFAULT_ALLOWED_ASSOCIATIONS)
    bot_logins: set[str] = field(default_factory=_bot_logins)
    trusted_actors: frozenset[str] = frozenset()
    push_transport: Any = None  # squadhq.http transport for Expo (tests inject FakeTransport)


def _token_minter() -> TokenMinter:
    """``SQUADHQ_TOKEN_MINTER=env`` (default) or ``app`` (per-repo GitHub App installation tokens)."""
    if (os.environ.get("SQUADHQ_TOKEN_MINTER") or "env").strip().lower() == "env":
        return EnvTokenMinter()
    from squadhq.codehost.github_app import select_token_minter

    minter: TokenMinter = select_token_minter()
    return minter


def _allowed_associations() -> frozenset[str]:
    """``SQUADHQ_TRUSTED_ASSOCIATIONS`` (shared with Actions mode, via :func:`squadhq.untrusted.trusted_associations`).

    ``SQUADHQ_ALLOWED_ASSOCIATIONS`` is a deprecated alias, used only when the
    new name is unset.
    """
    from squadhq.untrusted import trusted_associations

    if not os.environ.get("SQUADHQ_TRUSTED_ASSOCIATIONS") and os.environ.get("SQUADHQ_ALLOWED_ASSOCIATIONS"):
        log.warning("SQUADHQ_ALLOWED_ASSOCIATIONS is deprecated; use SQUADHQ_TRUSTED_ASSOCIATIONS")
        legacy = frozenset(a.strip().upper() for a in os.environ["SQUADHQ_ALLOWED_ASSOCIATIONS"].split(",")
                           if a.strip())
        if legacy:
            return legacy
    return trusted_associations()


def default_deps(settings: ServerSettings) -> WorkerDeps:
    allowed = _allowed_associations()
    return WorkerDeps(
        integrations=DefaultIntegrations(),
        modules=SubprocessModuleRunner(),
        dispatcher=GitHubWorkflowDispatcher(settings.github_token, settings.github_api_url),
        containers=DockerContainerRunner(settings.container_runtime, workdir=settings.sandbox_workdir or None),
        # SQUADHQ_TOKEN_MINTER=app|env (default env): app mints per-repo GitHub App installation tokens.
        tokens=_token_minter(),
        allowed_associations=allowed or ev_mod.DEFAULT_ALLOWED_ASSOCIATIONS,
        trusted_actors=frozenset(a.strip().lower() for a in os.environ.get("SQUADHQ_TRUSTED_ACTORS", "").split(",")
                                 if a.strip()),
    )


class Worker:
    def __init__(self, store: StateStore, settings: ServerSettings, deps: WorkerDeps | None = None, *,
                 worker_id: str | None = None, clock: Callable[[], datetime] = utcnow,
                 rng: Callable[[], float] = random.random) -> None:
        self.store = store
        self.settings = settings
        self.deps = deps or default_deps(settings)
        self.worker_id = worker_id or settings.worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.clock = clock
        self.rng = rng
        # Where pool credentials are read from (ANTHROPIC_API_KEY_n / CLAUDE_CODE_OAUTH_TOKEN_n); tests override.
        self.pool_env: Mapping[str, str] = os.environ
        self._stop = threading.Event()
        self.approvals = ApprovalExecutor(store, self.deps.integrations, required_checks=settings.required_checks,
                                          allow_fork_merges=settings.allow_fork_merges,
                                          required_checks_app=settings.required_checks_app,
                                          required_checks_workflow=settings.required_checks_workflow)
        self._secrets = secret_values(extra=[settings.github_token, *settings.webhook_secrets.values()])
        self.handlers: dict[str, Handler] = {
            "webhook_event": self.h_webhook_event,
            "sweep": self.h_sweep,
            "ticket_transition": self.h_ticket_transition,
            "janitor": self.h_janitor,
            "hub_loop": self.h_hub_loop,
            "hub_workflow": self.h_hub_workflow,
            "dispatch_coding": self.h_dispatch_coding,
            "approval_action": self.h_approval_action,
            "intake_ticket": self.h_intake_ticket,
            "push_notify": self.h_push_notify,
        }

    # ------------------------------------------------------------------ loop
    def run_once(self) -> Job | None:
        job = self.store.claim(self.worker_id, lease_s=self.settings.worker_lease_s)
        if job is None:
            return None
        self.process(job)
        return job

    def process(self, job: Job) -> None:
        token = job_id_var.set(job.id)
        stop_hb = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(job.id, stop_hb), daemon=True)
        hb.start()
        try:
            handler = self.handlers.get(job.kind)
            with tracing.span(f"job.{job.kind}", job_id=job.id, attempt=job.attempts):
                if handler is None:
                    raise FatalError(f"no handler for job kind {job.kind!r}")
                result = handler(job)
            self.store.complete(job.id, self.worker_id, self._scrub(result))
            log.info("job done", extra={"kind": job.kind, "attempt": job.attempts})
        except FatalError as e:
            err = redact(f"fatal: {e}", self._secrets)
            self.store.fail(job.id, self.worker_id, err, retry_at=None)
            log.error("job dead-lettered", extra={"kind": job.kind, "error": err})
        except Exception as e:
            self._retry_or_dead(job, e)
        finally:
            stop_hb.set()
            hb.join(timeout=5)
            job_id_var.reset(token)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact(value, self._secrets)
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def _retry_or_dead(self, job: Job, e: Exception) -> None:
        err = redact(f"{type(e).__name__}: {e}", self._secrets)
        if job.attempts >= job.max_attempts:
            self.store.fail(job.id, self.worker_id, err, retry_at=None)
            log.error("job dead-lettered after retries", extra={"kind": job.kind, "error": err})
            return
        if isinstance(e, CapacityError):
            delay = self.settings.retry_max_s
        else:
            delay = backoff_delay(job.attempts, self.settings.retry_base_s, self.settings.retry_max_s, self.rng)
        self.store.fail(job.id, self.worker_id, err, retry_at=self.clock() + timedelta(seconds=delay))
        log.warning("job failed, will retry", extra={"kind": job.kind, "error": err, "retry_in_s": round(delay, 1)})

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        interval = max(1.0, self.settings.worker_lease_s / 3)
        while not stop.wait(interval):
            if not self.store.extend_lease(job_id, self.worker_id, self.settings.worker_lease_s):
                log.warning("lost lease", extra={"job": job_id})
                return

    def run_forever(self) -> None:  # pragma: no cover - loop wrapper
        log.info("worker started", extra={"worker_id": self.worker_id, "mode": self.settings.mode})
        while not self._stop.is_set():
            try:
                job = self.run_once()
            except Exception:
                log.exception("worker loop error")
                job = None
            if job is None:
                self._stop.wait(self.settings.worker_poll_s)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- helpers
    def _hub(self) -> str:
        if self.settings.hub_repo:
            return self.settings.hub_repo
        from squadhq.settings import hub_repo

        return hub_repo()

    def _module_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        try:
            hub = self._hub()
        except RuntimeError:
            hub = ""
        if hub:
            env.update({"SQUADHQ_HUB_REPO": hub, "HUB_REPO": hub, "HQ_REPO": hub})
        if self.settings.github_token:
            env["GH_TOKEN"] = self.settings.github_token
        return env

    def _run_module(self, module: str, args: list[str], *, fatal_on_error: bool = False) -> dict[str, Any]:
        res = self.deps.modules.run(module, args, env=self._module_env(), timeout_s=self.settings.module_timeout_s)
        out = {"module": module, "args": args, "returncode": res.returncode, "duration_s": round(res.duration_s, 3),
               "stdout_tail": res.stdout_tail[-1000:]}
        if res.returncode != 0:
            msg = f"squadhq.{module} exited {res.returncode}: {res.stderr_tail[-500:]}"
            if fatal_on_error:
                raise FatalError(msg)
            raise RuntimeError(msg)
        return out

    # ------------------------------------------------------------ handlers
    def h_webhook_event(self, job: Job) -> dict[str, Any]:
        ev: dict[str, Any] = dict(job.payload.get("event") or {})
        source = str(ev.get("source") or job.payload.get("source") or "")
        project = str(ev.get("project") or "")
        out: dict[str, Any] = {"action": ev.get("action"), "fanout": []}
        known = ev_mod.registered_projects(self.store.list_projects(), self._safe_hub())
        if project.lower() not in known:
            # unregistered repo/project: nothing (tickets, approvals, dispatch) may happen for it
            self.store.append_audit("squadhq-worker", "webhook.dropped_unregistered", f"{source}:{project or '?'}",
                                    {"delivery_id": job.payload.get("delivery_id"), "action": ev.get("action"),
                                     "actor": ev.get("actor")})
            return {**out, "dropped": "unregistered project"}
        row = ev_mod.ticket_row(ev)
        if row:
            self.store.upsert_ticket(row)
        if project and ev.get("key"):
            subject = ev_mod.subject_for(ev)
            want, drop = ev_mod.approval_changes(ev)
            t = ev.get("ticket") if isinstance(ev.get("ticket"), dict) else None
            raw_s = ev.get("raw_subset") or {}
            if want and not ev_mod.may_trigger(ev, self.deps.allowed_associations, self.deps.bot_logins,
                                                self.deps.trusted_actors):
                self.store.append_audit("squadhq-worker", "approval.request_refused", f"{project}:{subject}",
                                        {"kinds": want, "actor": ev.get("actor"), "association": ev.get("association")})
                out["refused_approvals"] = want
                want = []
            if ev_mod.is_head_push(ev) and subject.startswith("pr#"):
                n = self.store.resolve_approvals(kind="merge", project=project, subject=subject,
                                                 actor=str(ev.get("actor") or source), action="approval.superseded",
                                                 data={"reason": "new commits pushed to the PR head",
                                                       "head_sha": raw_s.get("head_sha")})
                if n:
                    out["fanout"].append(f"superseded:{n}")
            for kind in want:
                data: dict[str, Any] = {"tracker": source,
                                        "code_host": source if source in ("github", "gitlab") else "github",
                                        "requested_by": ev.get("actor")}
                if kind == "merge":
                    pin = self._pin_head(raw_s)
                    if not pin:
                        self.store.append_audit("squadhq-worker", "approval.request_refused", f"{project}:{subject}",
                                                {"kinds": ["merge"], "reason": "label event carries no head commit"})
                        continue
                    data.update(pin)
                appr, created = self.store.upsert_approval(
                    kind=kind, project=project, subject=subject,
                    title=str((t or {}).get("title") or raw_s.get("title") or ev.get("raw_type") or subject),
                    url=str((t or {}).get("url") or raw_s.get("url") or ""), data=data)
                if created:
                    out["fanout"].append(f"approval:{kind}")
                    self._push_approval(appr)
            for kind in drop:
                self.store.resolve_approvals(kind=kind, project=project, subject=subject,
                                             actor=str(ev.get("actor") or source))
        raw = ev.get("raw_subset") or {}
        action = ev.get("action")
        if source == "github" and project:
            if action == "push" and raw.get("default_branch") and raw.get("ref") == f"refs/heads/{raw['default_branch']}":
                r = self.store.enqueue("ticket_transition", {"repo": project, "before": raw.get("before") or "",
                                                             "after": raw.get("after") or ""},
                                       idempotency_key=f"tt:{project}:{raw.get('after')}")
                out["fanout"].append(f"ticket_transition:{r.job_id}")
            if action == "pr_closed" and not raw.get("merged") and ev.get("key"):
                r = self.store.enqueue("ticket_transition", {"repo": project, "closed_pr": str(ev["key"])},
                                       idempotency_key=f"tt:{project}:closed:{ev['key']}")
                out["fanout"].append(f"ticket_transition:{r.job_id}")
        roles = ev_mod.agent_roles(ev)
        if roles and project and ev.get("key"):
            if ev_mod.may_trigger(ev, self.deps.allowed_associations, self.deps.bot_logins, self.deps.trusted_actors):
                for role in roles:
                    r = self.store.enqueue(
                        "dispatch_coding", {"project": project, "key": str(ev["key"]), "role": role,
                                            "tracker": source},
                        idempotency_key=f"dispatch:{source}:{project}:{ev['key']}:{role}:{job.payload.get('delivery_id', job.id)}")
                    out["fanout"].append(f"dispatch_coding:{role}:{r.job_id}")
            else:
                self.store.append_audit("squadhq-worker", "trigger.refused", f"{project}:{ev.get('key')}",
                                        {"actor": ev.get("actor"), "association": ev.get("association"),
                                         "roles": roles})
                out["refused"] = roles
        return out

    def h_sweep(self, job: Job) -> dict[str, Any]:
        repo = job.payload.get("repo")
        return self._run_module("stuck_sweeper", [f"--repo={_valid('repo', str(repo), _REPO_RE)}"] if repo else [])

    def h_ticket_transition(self, job: Job) -> dict[str, Any]:
        p = job.payload
        args = [f"--repo={_valid('repo', str(p['repo']), _REPO_RE)}"]
        if p.get("closed_pr"):
            pr = str(p["closed_pr"])
            if not pr.isdigit():
                raise FatalError(f"refusing job: bad PR number {pr[:20]!r}")
            args += [f"--closed-pr={pr}"]
        else:
            before = _valid("sha", str(p.get("before") or ""), _SHA_RE) if p.get("before") else ""
            after = _valid("sha", str(p.get("after") or ""), _SHA_RE) if p.get("after") else ""
            args += [f"--before={before}", f"--after={after}"]
        return self._run_module("ticket_transition", args)

    def h_janitor(self, job: Job) -> dict[str, Any]:
        scope = str(job.payload.get("scope") or "hub")
        if scope not in ("hub", "projects"):
            raise FatalError(f"bad janitor scope {scope!r}")
        return self._run_module("hub_janitor", ["--scope", scope])

    def h_hub_loop(self, job: Job) -> dict[str, Any]:
        mode = str(job.payload.get("mode") or "")
        if mode not in {"cos", "brief", "growth", "seo", "legal", "finance", "rollup"}:
            raise FatalError(f"unknown hub loop mode {mode!r}")
        started = ts()
        outcome = "failure"
        try:
            out = self._run_module("agent_runner", [mode], fatal_on_error=True)  # no blind re-runs: may post
            outcome = "success"
        finally:
            self.store.record_run(Run(id=f"{job.id}:{job.attempts}", project=self._safe_hub(), role=mode,
                                      outcome=outcome, started_at=started, executor="agent_runner", job_id=job.id))
        if mode == "brief":
            out["brief_captured"] = self._capture_brief()
            if out["brief_captured"]:
                b = self.store.latest_brief()
                if b is not None:
                    push_brief(self.store, b.id, b.markdown)
        return out

    def _safe_hub(self) -> str:
        try:
            return self._hub()
        except RuntimeError:
            return ""

    def _capture_brief(self) -> bool:
        """Copy the newest ``daily-brief`` hub issue into the store for ``/briefs/latest``."""
        hub = self._safe_hub()
        if not hub:
            return False
        try:
            tickets = self.deps.integrations.tracker("github").list_tickets(
                hub, state="all", labels=["daily-brief"], limit=1)
        except Exception as e:
            log.warning("could not fetch the daily brief", extra={"error": str(e)})
            return False
        if not tickets:
            return False
        t = max(tickets, key=lambda x: x.created_at)
        self.store.add_brief(f"# {t.title}\n\n{t.body}", project="")
        return True

    def h_hub_workflow(self, job: Job) -> dict[str, Any]:
        workflow = _valid("workflow", str(job.payload.get("workflow") or ""), _WORKFLOW_RE)
        role = _valid("role", str(job.payload.get("role") or workflow.rsplit(".", 1)[0]).lower(), _ROLE_RE)
        if self.settings.mode == "byo-compute":
            hub = self._hub()
            self.deps.dispatcher.dispatch(hub, workflow, self.settings.dispatch_ref, {})
            self.store.append_audit("squadhq-worker", "workflow.dispatched", hub, {"workflow": workflow})
            return {"dispatched": workflow, "repo": hub}
        return self._run_container(job, project=self._hub(), key="", role=role, tracker="github")

    def h_dispatch_coding(self, job: Job) -> dict[str, Any]:
        p = job.payload
        project, key, role = str(p["project"]), str(p["key"]), str(p["role"])
        tracker = str(p.get("tracker") or "github")
        _valid("role", role, _ROLE_RE)
        _valid("ticket key", key, _KEY_RE)
        if tracker not in ("github", "gitlab", "jira", "linear", "trello"):
            raise FatalError(f"refusing job: bad tracker {tracker[:20]!r}")
        if tracker in ("github", "gitlab"):
            _valid("repo", project, _REPO_RE)
        else:
            _valid("project", project, re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,199}$"))
        if self.settings.mode == "byo-compute":
            repo = project if tracker == "github" else self._hub()
            inputs = {"role": role, "ticket": key, "tracker": tracker, "project": project, "squadhq_job": job.id}
            self.deps.dispatcher.dispatch(repo, self.settings.dispatch_workflow, self.settings.dispatch_ref, inputs)
            self.store.append_audit("squadhq-worker", "job.dispatched", f"{project}:{key}",
                                    {"role": role, "repo": repo, "workflow": self.settings.dispatch_workflow,
                                     "job_id": job.id})
            return {"dispatched": True, "repo": repo, "workflow": self.settings.dispatch_workflow}
        return self._run_container(job, project=project, key=key, role=role, tracker=tracker)

    def _pool_credentials(self, job: Job, secret_env: dict[str, str]) -> tuple[str, dict[str, str]]:
        """Pick the pool account for this job: ``(account id, secret env)``.

        No pool: the minted credentials unchanged (``""``). Every account
        tripped: metered ``ANTHROPIC_API_KEY`` with fallback_on_capacity_trip,
        else :class:`CapacityError` (retried after ``retry_max_s``).
        """
        pool = pool_mod.load_pool(self.pool_env)
        if not pool.configured:
            return "", secret_env
        now = self.clock()
        acct = pool_mod.select(self.store, pool, now, account_pool.rotation_seed(job.id, job.attempts))
        llm = set(account_pool.EXECUTOR_ENV.values())
        base = {k: v for k, v in secret_env.items() if k not in llm}
        if acct is not None:
            return acct.id, {**base, **account_pool.credential_from_env(acct, self.pool_env)}
        state, _ = pool_mod.read_state(self.store)
        until = account_pool.earliest_recovery(pool.accounts, state, now)
        if pool.fallback and secret_env.get("ANTHROPIC_API_KEY"):
            log.warning("every pool account is tripped: failing over to metered API", extra={"job": job.id})
            return "", {**base, "ANTHROPIC_API_KEY": secret_env["ANTHROPIC_API_KEY"]}
        raise CapacityError(f"every pool account is tripped until {until.isoformat() if until else '?'}")

    def _run_container(self, job: Job, *, project: str, key: str, role: str, tracker: str) -> dict[str, Any]:
        s = self.settings
        tokens = self.deps.tokens.mint(project, role)
        account, secret_env = self._pool_credentials(job, {t.env_name: t.value for t in tokens})
        env = {"SQUADHQ_ROLE": role, "SQUADHQ_REPO": project, "SQUADHQ_TICKET": key, "SQUADHQ_TRACKER": tracker,
               "SQUADHQ_JOB_ID": job.id, "SQUADHQ_RESULT_PATH": "/out/execution-result.json"}
        if account:
            env["SQUADHQ_ACCOUNT"] = account
        cjob = ContainerJob(
            job_id=job.id, image=s.executor_image, role=role, repo=project, key=key,
            env=env, secret_env=secret_env,
            cpus=s.job_cpus, memory=s.job_memory, pids_limit=s.job_pids_limit, network=s.job_network,
            timeout_s=s.job_timeout_s,
        )
        started = ts()
        outcome = self.deps.containers.run(cjob)
        res = outcome.result
        if res is not None and account:
            res.account = account  # the worker chose it; never taken from the container's file
        run = Run(id=f"{job.id}:{job.attempts}", project=project, role=role,
                  outcome=(res.outcome if res else ("timeout" if outcome.timed_out else "failure")),
                  started_at=started, model=res.model if res else "", executor=res.executor if res else "container",
                  cost_usd=float(res.cost_usd or 0) if res else 0.0, duration_s=outcome.duration_s,
                  input_tokens=int(res.input_tokens or 0) if res else 0,
                  output_tokens=int(res.output_tokens or 0) if res else 0,
                  error_class=(res.error_class.value if res and isinstance(res.error_class, ErrorClass)
                               else str(res.error_class) if res else "infra"),
                  job_id=job.id, data={"key": key, "exit_code": outcome.exit_code, "tracker": tracker,
                                       **({"account": account} if account else {})})
        self.store.record_run(run)
        self.store.append_audit("squadhq-worker", "job.executed", f"{project}:{key}",
                                {"role": role, "outcome": run.outcome, "exit_code": outcome.exit_code,
                                 "cost_usd": run.cost_usd, "job_id": job.id})
        if outcome.timed_out:
            raise FatalError(f"container timed out after {s.job_timeout_s:.0f}s")  # no retry: avoid cost burn
        if res is None:
            raise TransientError(f"container exited {outcome.exit_code} without a result: {outcome.logs_tail[-300:]}")
        if res.outcome == "failure" and run.error_class in ("transient", "infra"):
            raise TransientError(res.error_message or "executor reported a transient failure")
        if res.outcome == "failure" and (run.error_class == "capacity" or res.capacity_tripped) and account:
            state = pool_mod.trip(self.store, account, self.clock(), by=f"job:{job.id}")
            pool = pool_mod.load_pool(self.pool_env)
            left = [a.id for a in account_pool.available(pool.accounts, state, self.clock())]
            self.store.append_audit("squadhq-worker", "capacity.account_tripped", account,
                                    {"job_id": job.id, "accounts_left": left})
            if left:  # the retry picks the next account: back off normally, not for the whole window
                raise TransientError(f"pool account {account} hit its capacity limit; retrying on another account")
            raise CapacityError(f"pool account {account} hit its capacity limit; every pool account is tripped")
        if res.outcome == "failure" and run.error_class == "capacity":
            raise CapacityError(res.error_message or "capacity exhausted")
        return {"outcome": res.outcome, "cost_usd": run.cost_usd, "exit_code": outcome.exit_code}

    @staticmethod
    def _pin_head(raw: dict[str, Any]) -> dict[str, Any]:
        """Head commit the labelling event saw. Never looked up "now": a later request must not re-pin."""
        sha = str(raw.get("head_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return {}
        return {"head_sha": sha, "head_repo": str(raw.get("head_repo") or "")}

    def _push_approval(self, a: Any) -> None:
        kind: Literal["escalation", "approval"] = "escalation" if a.kind == "escalation" else "approval"
        label = {"merge": "Merge approval needed", "proposal": "Proposal awaiting decision",
                 "deploy": "Deploy approval needed", "escalation": "Escalation"}.get(a.kind, "Approval needed")
        n = Notification(title=f"{label}: {a.title}", body_markdown=f"{a.project} {a.subject}", kind=kind,
                         severity="warning" if kind == "escalation" else "info", url=a.url or None,
                         dedupe_key=f"approval:{a.id}", project=a.project)
        try:
            enqueue_push(self.store, n, f"approval:{a.id}")
        except Exception as e:  # a push must never fail the webhook job
            log.warning("could not queue push", extra={"error": str(e)})

    def h_intake_ticket(self, job: Job) -> dict[str, Any]:
        iid = str(job.payload.get("intake_id") or "")
        req = self.store.get_intake(iid)
        if req is None:
            raise FatalError(f"intake request {iid} not found")
        if req.ticket_key:  # at-least-once: a re-delivered job must not open a second ticket
            return {"ticket_key": req.ticket_key, "duplicate": True}
        tracker = self.deps.integrations.tracker(req.tracker)
        body = intake_body(req.body, req.requested_by, req.source)
        try:
            t = tracker.create_ticket(req.project, single_line(req.title, 256), body)
        except FatalError as e:
            self.store.update_intake(iid, status="failed", error=str(e))
            raise
        self.store.update_intake(iid, status="created", ticket_key=t.key, ticket_url=t.url)
        self.store.append_audit("squadhq-worker", "intake.ticket_created", f"{req.project}:{t.key}",
                                {"intake_id": iid, "requested_by": req.requested_by, "source": req.source,
                                 "url": t.url})
        return {"ticket_key": t.key, "ticket_url": t.url}

    def h_push_notify(self, job: Job) -> dict[str, Any]:
        from squadhq.notify.expo_push import ExpoPushNotifier, PushDevice

        n = notification_from(dict(job.payload.get("notification") or {}))
        notifier = ExpoPushNotifier(
            devices=lambda: [PushDevice(d.expo_push_token, tuple(d.topics)) for d in self.store.list_devices()],
            transport=self.deps.push_transport, on_invalid=self.store.delete_devices_by_token)
        notifier.send(n)
        r = notifier.last_result
        return {"sent": r.sent, "invalid": len(r.invalid_tokens), "errors": r.errors, "skipped": r.skipped_reason}

    def h_approval_action(self, job: Job) -> dict[str, Any]:
        aid = str(job.payload.get("approval_id") or "")
        approval = self.store.get_approval(aid)
        if approval is None:
            raise FatalError(f"approval {aid} not found")
        res = self.approvals.execute(approval)
        return {"performed": res.performed, **res.detail}


# Values from webhooks/jobs that end up in argv (argparse), REST paths or executor env.
# argv is a list (no shell), but a value such as "--dry-run" would still be parsed as an option,
# and "../" in a workflow name would walk the REST path. Validate shapes; refuse the job otherwise.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.#-]{0,63}$")  # "42", "ENG-42", Trello card ids
_SHA_RE = re.compile(r"^[0-9a-f]{0,40}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}\.ya?ml$")


def _valid(kind: str, value: str, rx: re.Pattern[str]) -> str:
    if not rx.match(value) or value.startswith("-") or ".." in value:
        raise FatalError(f"refusing job: bad {kind} {value[:80]!r}")
    return value


_MARKER_LINE = re.compile(r"^\s*originated-by\s*:.*$", re.I | re.M)
_HTML_COMMENT = re.compile(r"<!--.*?(-->|$)", re.S)


def single_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def intake_body(text: str, requested_by: str, source: str) -> str:
    """Ticket body for an intake request.

    The requester's text is untrusted like any issue body (agents fence it
    when prompting). It may not forge our provenance line or hidden
    automation markers (``<!-- stuck-sweeper ... -->``), so both are removed
    before the server appends the real ``Originated-by`` line.
    """
    clean = text
    while True:  # nested input ("<!-<!-- -->- x -->") can rebuild a marker after one pass
        nxt = _MARKER_LINE.sub("", _HTML_COMMENT.sub("", clean))
        if nxt == clean:
            break
        clean = nxt
    clean = clean.replace("<!--", "&lt;!--").strip()
    who = requested_by.removeprefix("token:")
    return f"{clean}\n\n---\nOriginated-by: {who} via {source}\n".lstrip()


def push_brief(store: StateStore, brief_id: str, markdown: str) -> str | None:
    n = Notification(title="Daily brief is ready", body_markdown=markdown, kind="digest",
                     dedupe_key=f"brief:{brief_id}")
    try:
        return enqueue_push(store, n, f"brief:{brief_id}")
    except Exception as e:
        log.warning("could not queue push", extra={"error": str(e)})
        return None


def sync_projects(store: StateStore, projects: list[dict[str, Any]]) -> int:
    """Register projects from ``squadhq.yml`` in the store (``/projects``)."""
    from squadhq.store.base import Project

    n = 0
    for p in projects:
        repo = str(p.get("repo") or "")
        if not repo:
            continue
        store.upsert_project(Project(name=str(p.get("name") or repo), repo=repo,
                                     tracker=str(p.get("tracker") or "github"),
                                     data={k: v for k, v in p.items() if k in (
                                         "branch", "packs", "merge_mode", "tracker_project", "jira_project",
                                         "linear_team", "trello_board", "gitlab_project")}))
        n += 1
    return n


def sync_tickets_from_tracker(store: StateStore, integrations: Integrations, project: str,
                              tracker: str = "github") -> int:  # pragma: no cover - thin adapter glue
    n = 0
    for t in integrations.tracker(tracker).list_tickets(project, state="open"):
        store.upsert_ticket(TicketRow(project=project, key=t.key, title=t.title, state=t.state,
                                      flow_state=ev_mod.flow_state(t.labels, t.native_state or t.state),
                                      labels=t.labels, url=t.url, tracker=tracker, updated_at=t.updated_at))
        n += 1
    return n

