"""Run one coding job in an ephemeral, locked-down container (worker mode).

:class:`DockerContainerRunner` builds a ``docker run`` (or ``podman run``)
command with:

* ``--read-only`` root filesystem, ``tmpfs`` for ``/tmp`` and ``/work``;
* ``--security-opt no-new-privileges``, ``--cap-drop ALL``, non-root ``--user``;
* ``--cpus`` / ``--memory`` / ``--pids-limit`` resource limits;
* ``--network <name>``: a network whose egress is restricted to an allowlist
  (LLM provider + code host). Docker itself cannot filter egress by host;
  attach the network to an egress proxy or firewall — see
  docs/deploy/security-hardening.md;
* per-job, short-lived credentials passed by *name* (``-e NAME``) so their
  values never appear on the command line / in ``ps``;
* a wall-clock timeout after which the container is killed.

The executor writes ``execution-result.json`` (the neutral
:class:`~squadhq.interfaces.ExecutionResult`) to ``/out``, a per-job
host directory mounted read-write.

Tests use :class:`FakeContainerRunner`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from squadhq.interfaces import ErrorClass, ExecutionResult
from squadhq.server.redact import redact, secret_values

log = logging.getLogger("squadhq.sandbox")

RESULT_FILE = "execution-result.json"


@dataclass
class ContainerJob:
    job_id: str
    image: str
    role: str
    repo: str
    key: str = ""
    command: list[str] = field(default_factory=list)  # empty = image entrypoint
    env: dict[str, str] = field(default_factory=dict)  # non-secret settings
    secret_env: dict[str, str] = field(default_factory=dict)  # values never on the command line
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: int = 512
    network: str = "squadhq-egress"
    timeout_s: float = 3600.0
    user: str = "10001:10001"


@dataclass
class ContainerOutcome:
    exit_code: int
    timed_out: bool
    duration_s: float
    result: ExecutionResult | None
    logs_tail: str = ""


class ContainerRunner(Protocol):
    def run(self, job: ContainerJob) -> ContainerOutcome: ...


def _result_from_json(data: dict[str, Any], job: ContainerJob) -> ExecutionResult:
    try:
        from squadhq.executors.result import from_dict

        return from_dict(data)
    except ImportError:  # pragma: no cover - executors package ships with the core
        return ExecutionResult(executor=str(data.get("executor", "unknown")), role=job.role,
                               model=str(data.get("model", "")), outcome=data.get("outcome", "failure"))


MAX_RESULT_BYTES = 1_048_576


def _read_result_file(path: str) -> bytes | None:
    """Read the executor's result without trusting it: no symlinks, regular file only, size-capped.

    The file is written by the (untrusted) container into a directory the
    worker later reads; a symlink there could point at a host file or a device.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("execution result is not a regular file")
        if st.st_size > MAX_RESULT_BYTES:
            raise ValueError(f"execution result larger than {MAX_RESULT_BYTES} bytes")
        return os.read(fd, MAX_RESULT_BYTES + 1)[:MAX_RESULT_BYTES]
    finally:
        os.close(fd)


class DockerContainerRunner:
    def __init__(self, runtime: str = "docker", *, workdir: str | None = None,
                 run: Any = subprocess.run) -> None:
        self.runtime = runtime
        self.workdir = workdir
        self._run = run

    def build_argv(self, job: ContainerJob, out_dir: str) -> list[str]:
        name = f"squadhq-job-{job.job_id[:24]}"
        argv = [
            self.runtime, "run", "--rm", "--name", name,
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",  # noqa: S108 - container path
            "--tmpfs", "/work:rw,nosuid,size=4g",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--user", job.user,
            "--cpus", job.cpus,
            "--memory", job.memory,
            "--memory-swap", job.memory,
            "--pids-limit", str(job.pids_limit),
            "--network", job.network,
            "--label", f"squadhq.job={job.job_id}",
            "--label", f"squadhq.role={job.role}",
            "-v", f"{out_dir}:/out:rw",
            "-w", "/work",
        ]
        for k, v in sorted(job.env.items()):
            argv += ["-e", f"{k}={v}"]
        for k in sorted(job.secret_env):
            argv += ["-e", k]  # value inherited from the runner's environment, not argv
        argv.append(job.image)
        argv.extend(job.command)
        return argv

    def _prepare_out_dir(self, job: ContainerJob) -> tuple[str, str]:
        """Private per-job result dir: a 0700 parent, a 0700 child owned by the container user.

        As root the child is chowned to ``job.user``; otherwise the container is
        run as the worker's own uid:gid so it can write the 0700 dir it owns.
        """
        parent = tempfile.mkdtemp(prefix=f"squadhq-{job.job_id[:12]}-", dir=self.workdir)  # mkdtemp => 0700
        out_dir = os.path.join(parent, "out")
        os.mkdir(out_dir, 0o700)
        uid_s, _, gid_s = job.user.partition(":")
        uid, gid = int(uid_s), int(gid_s or uid_s)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(out_dir, uid, gid)
        elif hasattr(os, "geteuid") and os.geteuid() != uid:
            job.user = f"{os.geteuid()}:{os.getegid()}"
        # the daemon bind-mounts out_dir itself, so the parent stays 0700 (never traversed by the container)
        return parent, out_dir

    def run(self, job: ContainerJob) -> ContainerOutcome:
        parent, out_dir = self._prepare_out_dir(job)
        argv = self.build_argv(job, out_dir)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **job.secret_env}
        for passthrough in ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "HOME"):
            if passthrough in os.environ:
                env.setdefault(passthrough, os.environ[passthrough])
        start = time.monotonic()
        timed_out = False
        try:
            proc = self._run(argv, env=env, capture_output=True, text=True, timeout=job.timeout_s, check=False)
            code, logs = int(proc.returncode), (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as e:
            timed_out, code = True, 124
            logs = str(e.output or "")
            self._run([self.runtime, "kill", f"squadhq-job-{job.job_id[:24]}"], capture_output=True,
                      text=True, timeout=30, check=False)
        duration = time.monotonic() - start
        result = None
        path = os.path.join(out_dir, RESULT_FILE)
        try:
            raw = _read_result_file(path)
            if raw is not None:
                result = _result_from_json(json.loads(raw), job)
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.warning("unreadable execution result", extra={"error": str(e)})
        finally:
            shutil.rmtree(parent, ignore_errors=True)
        known = secret_values(extra=job.secret_env.values())
        return ContainerOutcome(exit_code=code, timed_out=timed_out, duration_s=duration, result=result,
                                logs_tail=redact(logs[-4000:], known))


class FakeContainerRunner:
    """Records jobs and returns canned outcomes (tests, dry runs)."""

    def __init__(self, outcomes: list[ContainerOutcome] | None = None) -> None:
        self.jobs: list[ContainerJob] = []
        self.outcomes = list(outcomes or [])

    def run(self, job: ContainerJob) -> ContainerOutcome:
        self.jobs.append(job)
        if self.outcomes:
            return self.outcomes.pop(0)
        return ContainerOutcome(
            exit_code=0, timed_out=False, duration_s=1.0,
            result=ExecutionResult(executor="fake", role=job.role, model="fake-model", outcome="success",
                                   error_class=ErrorClass.NONE, cost_usd=0.01, input_tokens=10, output_tokens=5),
        )
