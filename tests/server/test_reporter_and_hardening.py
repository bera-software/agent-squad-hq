"""C6: project-scoped reporter tokens; worker/sandbox input hardening."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import pytest

from squadhq.interfaces import FatalError
from squadhq.server import auth
from squadhq.server.__main__ import main
from squadhq.server.sandbox import ContainerJob, DockerContainerRunner, _read_result_file
from tests.server.conftest import ADMIN, APPROVER, VIEWER, Rig, auth as bearer, make_settings

RUN = {"project": "acme/storefront", "role": "implement", "outcome": "success", "cost_usd": 0.5, "run_id": "123-1"}


def _reporter(client: Any, project: str = "acme/storefront") -> str:
    r = client.post("/api/v1/tokens", json={"name": "storefront-ci", "role": "reporter", "project": project},
                    headers=bearer(ADMIN))
    assert r.status_code == 201, r.text
    assert r.json()["project"] == project
    return str(r.json()["token"])


def test_reporter_reports_only_its_project(client: Any, store: Any) -> None:
    tok = _reporter(client)
    r = client.post("/api/v1/runs", json=RUN, headers=bearer(tok))
    assert r.status_code == 201
    assert r.json()["id"] == "acme/storefront:123-1"  # namespaced: cannot pre-claim another project's run ids
    other = client.post("/api/v1/runs", json={**RUN, "project": "acme/billing"}, headers=bearer(tok))
    assert other.status_code == 403
    assert client.post("/api/v1/runs", json={**RUN, "project": "ACME/Storefront", "run_id": "2"},
                       headers=bearer(tok)).status_code == 201  # case-insensitive match


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/v1/projects", None), ("get", "/api/v1/runs", None), ("get", "/api/v1/approvals", None),
    ("get", "/api/v1/audit", None), ("get", "/api/v1/jobs", None), ("get", "/api/v1/tokens", None),
    ("get", "/api/v1/whoami", None), ("get", "/api/v1/costs", None),
    ("post", "/api/v1/jobs", {"kind": "sweep"}), ("post", "/api/v1/briefs", {"markdown": "x"}),
    ("post", "/api/v1/requests", {"project": "acme/storefront", "title": "t"}),
    ("post", "/api/v1/devices", {"expo_push_token": "ExponentPushToken[x]", "platform": "ios"}),
    ("post", "/api/v1/tokens", {"name": "x", "role": "admin"}),
    ("get", "/metrics", None),
])
def test_reporter_cannot_do_anything_else(client: Any, method: str, path: str, body: Any) -> None:
    tok = _reporter(client)
    r = getattr(client, method)(path, headers=bearer(tok), **({"json": body} if body is not None else {}))
    assert r.status_code == 403, (path, r.status_code)


def test_reporter_cannot_attach_another_projects_job(client: Any, store: Any) -> None:
    tok = _reporter(client)
    jid = store.enqueue("dispatch_coding", {"project": "acme/billing", "key": "1", "role": "implement"}).job_id
    assert client.post("/api/v1/runs", json={**RUN, "job_id": jid}, headers=bearer(tok)).status_code == 403
    own = store.enqueue("dispatch_coding", {"project": "acme/storefront", "key": "2", "role": "implement"}).job_id
    assert client.post("/api/v1/runs", json={**RUN, "job_id": own}, headers=bearer(tok)).status_code == 201


def test_admin_and_others_on_runs(client: Any) -> None:
    assert client.post("/api/v1/runs", json=RUN, headers=bearer(ADMIN)).json()["id"] == "123-1"
    assert client.post("/api/v1/runs", json=RUN, headers=bearer(APPROVER)).status_code == 403
    assert client.post("/api/v1/runs", json=RUN, headers=bearer(VIEWER)).status_code == 403


def test_reporter_token_scope_validation(client: Any) -> None:
    h = bearer(ADMIN)
    assert client.post("/api/v1/tokens", json={"name": "r", "role": "reporter"}, headers=h).status_code == 422
    assert client.post("/api/v1/tokens", json={"name": "r", "role": "reporter", "project": "evil/unknown"},
                       headers=h).status_code == 422
    assert client.post("/api/v1/tokens", json={"name": "v", "role": "viewer", "project": "acme/storefront"},
                       headers=h).status_code == 422
    assert client.post("/api/v1/tokens", json={"name": "r", "role": "reporter", "project": "acme/storefront"},
                       headers=bearer(APPROVER)).status_code == 403


def test_static_reporter_tokens() -> None:
    h = auth.hash_token("t")
    [t] = auth.parse_static_tokens(f"ci:reporter:{h}:acme/a")
    assert (t.role, t.project) == ("reporter", "acme/a")
    for bad in (f"ci:reporter:{h}", f"ci:viewer:{h}:acme/a", f"ci:reporter:{h}:not-a-repo"):
        with pytest.raises(ValueError):
            auth.parse_static_tokens(bad)
    p = auth.match("t", [("1", "ci", "reporter", h, "acme/a")])
    assert p is not None and p.is_reporter and not p.has("viewer") and p.may_report_for("acme/a")
    assert not p.may_report_for("acme/b")
    assert auth.Principal("a", "admin").may_report_for("anything/else")


def test_cli_token_create_reporter(tmp_path: Any, capsys: Any) -> None:
    s = make_settings(db_url=f"sqlite:///{tmp_path}/hq.db")
    assert main(["token", "create", "--role", "reporter", "--project", "acme/a", "--name", "a-ci"], s) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["role"] == "reporter" and out["project"] == "acme/a"
    assert main(["create-token", "--role", "reporter"], s) == 2  # project required
    assert main(["create-token", "--role", "viewer", "--project", "acme/a"], s) == 2
    assert main(["token", "create", "--role", "reporter", "--project", "acme/a", "--print-only"], s) == 0
    entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["env_entry"]
    assert entry.endswith(":acme/a") and auth.parse_static_tokens(entry)[0].project == "acme/a"


# ----------------------------------------------------------- worker / sandbox

@pytest.mark.parametrize("kind,payload", [
    ("sweep", {"repo": "--dry-run"}),
    ("sweep", {"repo": "acme/../etc"}),
    ("ticket_transition", {"repo": "acme/a", "before": "--closed-pr", "after": "b"}),
    ("ticket_transition", {"repo": "acme/a", "closed_pr": "1 --dry-run"}),
    ("hub_workflow", {"workflow": "../../../user/repos.yml"}),
    ("hub_workflow", {"workflow": "ci.yml", "role": "Bad Role"}),
    ("dispatch_coding", {"project": "acme/a", "key": "--rm", "role": "implement"}),
    ("dispatch_coding", {"project": "acme/a", "key": "7\nX=1", "role": "implement"}),
    ("dispatch_coding", {"project": "-acme/a", "key": "7", "role": "implement"}),
    ("dispatch_coding", {"project": "acme/a", "key": "7", "role": "implement", "tracker": "evil"}),
])
def test_untrusted_shapes_are_refused(rig: Rig, store: Any, kind: str, payload: dict[str, Any]) -> None:
    jid = store.enqueue(kind, payload).job_id
    rig.worker.run_once()
    j = store.get_job(jid)
    assert j.status == "dead" and "refusing job" in j.last_error
    assert rig.modules.calls == [] and rig.dispatcher.calls == []


def test_valid_jira_key_still_dispatches(rig: Rig, store: Any) -> None:
    store.enqueue("dispatch_coding", {"project": "ENG", "key": "ENG-42", "role": "implement", "tracker": "jira"})
    rig.worker.run_once()
    assert rig.dispatcher.calls[0][3]["ticket"] == "ENG-42"


def test_result_file_symlink_and_size_are_refused(tmp_path: Any) -> None:
    secret = tmp_path / "host-secret"
    secret.write_text('{"outcome": "success", "model": "leaked"}')
    link = tmp_path / "execution-result.json"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        _read_result_file(str(link))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1_048_576 + 10))
    with pytest.raises(ValueError, match="larger"):
        _read_result_file(str(big))
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ValueError, match="regular"):
        _read_result_file(str(tmp_path / "fifo"))
    assert _read_result_file(str(tmp_path / "missing")) is None


def test_runner_ignores_symlinked_result(tmp_path: Any) -> None:
    target = tmp_path / "host.json"
    target.write_text('{"executor": "x", "role": "implement", "model": "leaked", "outcome": "success"}')

    def fake_run(argv: list[str], **kw: Any) -> Any:
        if argv[1] == "run":
            out = argv[argv.index("-v") + 1].split(":")[0]
            os.symlink(target, os.path.join(out, "execution-result.json"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    out = DockerContainerRunner("docker", workdir=str(tmp_path), run=fake_run).run(
        ContainerJob(job_id="abc", image="img", role="implement", repo="acme/a"))
    assert out.result is None


def test_unknown_container_env_is_not_forwarded(monkeypatch: Any) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-only-secret-value")
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> Any:
        seen.update(kw["env"])
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    DockerContainerRunner("docker", run=fake_run).run(
        ContainerJob(job_id="abc", image="img", role="implement", repo="acme/a", secret_env={"GH_TOKEN": "ghs_x"}))
    assert "AWS_SECRET_ACCESS_KEY" not in seen and seen["GH_TOKEN"] == "ghs_x"
    assert "host-only-secret-value" not in " ".join(seen["argv"])


def test_refusal_is_fatal_type() -> None:
    from squadhq.server.worker import _REPO_RE, _valid

    with pytest.raises(FatalError):
        _valid("repo", "a/b/c", _REPO_RE)
    assert _valid("repo", "a/b", _REPO_RE) == "a/b"
