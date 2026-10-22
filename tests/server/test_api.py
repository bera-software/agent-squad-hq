from __future__ import annotations

from typing import Any

import pytest

from squadhq.store.base import Run, TicketRow
from tests.server.conftest import ADMIN, APPROVER, VIEWER, auth, make_settings


def test_healthz_is_public(client: Any) -> None:
    for path in ("/healthz", "/api/v1/healthz"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_readyz(client: Any) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["schema_version"] >= 1


def test_readyz_unavailable(client: Any, store: Any) -> None:
    store.close()
    assert client.get("/api/v1/readyz").status_code == 503


def test_security_headers_and_request_id(client: Any) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    bad = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert bad.headers["x-request-id"] != "bad id with spaces"
    assert len(bad.headers["x-request-id"]) == 32


@pytest.mark.parametrize("path", ["/api/v1/projects", "/api/v1/tickets", "/api/v1/runs", "/api/v1/approvals",
                                  "/api/v1/costs", "/api/v1/audit", "/api/v1/briefs/latest", "/api/v1/jobs",
                                  "/api/v1/whoami", "/api/v1/schedules"])
def test_auth_required(client: Any, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert client.get(path, headers=auth("sqh_wrong")).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 401


def test_rbac(client: Any) -> None:
    assert client.get("/api/v1/whoami", headers=auth(VIEWER)).json() == {"name": "viewer", "role": "viewer"}
    # viewer cannot read audit or decide; approver can read audit; only admin manages tokens/jobs
    assert client.get("/api/v1/audit", headers=auth(VIEWER)).status_code == 403
    assert client.get("/api/v1/audit", headers=auth(APPROVER)).status_code == 200
    assert client.post("/api/v1/approvals/x/decision", json={"decision": "approve"},
                       headers=auth(VIEWER)).status_code == 403
    assert client.get("/api/v1/tokens", headers=auth(APPROVER)).status_code == 403
    assert client.post("/api/v1/jobs", json={"kind": "sweep"}, headers=auth(APPROVER)).status_code == 403
    assert client.get("/api/v1/tokens", headers=auth(ADMIN)).status_code == 200


def test_projects_synced_from_config(client: Any) -> None:
    r = client.get("/api/v1/projects", headers=auth(VIEWER))
    assert r.status_code == 200
    assert {p["repo"] for p in r.json()["projects"]} >= {"acme/storefront", "acme/billing"}


def test_tickets_filter(client: Any, store: Any) -> None:
    store.upsert_ticket(TicketRow("acme/a", "1", "one", "open", "shaped", ["triaged:ready"], "u", "github", "2026-01-01"))
    store.upsert_ticket(TicketRow("acme/b", "2", "two", "closed", "done", [], "u", "github", "2026-01-02"))
    allt = client.get("/api/v1/tickets", headers=auth(VIEWER)).json()["tickets"]
    assert {t["key"] for t in allt} == {"1", "2"}
    assert [t["key"] for t in client.get("/api/v1/tickets?project=acme/a", headers=auth(VIEWER)).json()["tickets"]] == ["1"]
    assert [t["key"] for t in client.get("/api/v1/tickets?state=shaped", headers=auth(VIEWER)).json()["tickets"]] == ["1"]
    assert [t["key"] for t in client.get("/api/v1/tickets?state=closed", headers=auth(VIEWER)).json()["tickets"]] == ["2"]


def test_runs_and_costs(client: Any, store: Any) -> None:
    store.record_run(Run(id="r1", project="acme/a", role="implement", outcome="success",
                         started_at="2026-10-02T10:00:00.000000Z", model="m1", cost_usd=1.5))
    store.record_run(Run(id="r2", project="acme/b", role="review", outcome="failure",
                         started_at="2026-10-03T10:00:00.000000Z", model="m2", cost_usd=0.25))
    store.record_run(Run(id="r3", project="acme/a", role="review", outcome="success",
                         started_at="2026-09-30T10:00:00.000000Z", model="m2", cost_usd=9))
    runs = client.get("/api/v1/runs?project=acme/a&limit=1", headers=auth(VIEWER)).json()["runs"]
    assert [r["id"] for r in runs] == ["r1"]
    c = client.get("/api/v1/costs?month=2026-10", headers=auth(VIEWER)).json()
    assert c["total_usd"] == 1.75 and c["runs"] == 2
    assert c["by_project"] == {"acme/a": 1.5, "acme/b": 0.25}
    assert c["by_role"] == {"implement": 1.5, "review": 0.25}
    assert c["by_model"] == {"m1": 1.5, "m2": 0.25}
    assert client.get("/api/v1/costs?month=2026-13", headers=auth(VIEWER)).status_code == 422
    assert client.get("/api/v1/costs", headers=auth(VIEWER)).status_code == 200


def test_report_run(client: Any, store: Any) -> None:
    body = {"project": "acme/a", "role": "implement", "outcome": "success", "cost_usd": 0.5, "run_id": "gh-1"}
    assert client.post("/api/v1/runs", json=body, headers=auth(APPROVER)).status_code == 403
    r = client.post("/api/v1/runs", json=body, headers=auth(ADMIN))
    assert r.status_code == 201
    assert store.list_runs()[0].id == "gh-1"
    assert client.post("/api/v1/runs", json={**body, "cost_usd": -1}, headers=auth(ADMIN)).status_code == 422


def test_briefs(client: Any) -> None:
    assert client.get("/api/v1/briefs/latest", headers=auth(VIEWER)).status_code == 404
    assert client.post("/api/v1/briefs", json={"markdown": "# old"}, headers=auth(ADMIN)).status_code == 201
    assert client.post("/api/v1/briefs", json={"markdown": "# Brief\nhello"}, headers=auth(ADMIN)).status_code == 201
    r = client.get("/api/v1/briefs/latest", headers=auth(VIEWER))
    assert r.json()["markdown"] == "# Brief\nhello"
    md = client.get("/api/v1/briefs/latest?format=markdown", headers=auth(VIEWER))
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.text == "# Brief\nhello"


def test_schedules_endpoint(client: Any) -> None:
    r = client.get("/api/v1/schedules", headers=auth(VIEWER)).json()
    assert r["timezone"] == "Europe/London"
    assert r["schedules"][0]["key"] == "daily_brief"


def test_jobs_endpoints(client: Any, store: Any) -> None:
    r = client.post("/api/v1/jobs", json={"kind": "sweep", "payload": {"repo": "acme/a"}, "idempotency_key": "k1"},
                    headers=auth(ADMIN))
    assert r.status_code == 202 and r.json()["created"] is True
    again = client.post("/api/v1/jobs", json={"kind": "sweep", "idempotency_key": "k1"}, headers=auth(ADMIN))
    assert again.json() == {"job_id": r.json()["job_id"], "created": False}
    assert client.post("/api/v1/jobs", json={"kind": "rm -rf"}, headers=auth(ADMIN)).status_code == 422
    listing = client.get("/api/v1/jobs?status=queued", headers=auth(VIEWER)).json()
    assert listing["counts"]["queued"] == 1
    jid = r.json()["job_id"]
    assert client.get(f"/api/v1/jobs/{jid}", headers=auth(VIEWER)).json()["kind"] == "sweep"
    assert client.get("/api/v1/jobs/nope", headers=auth(VIEWER)).status_code == 404
    assert client.post(f"/api/v1/jobs/{jid}/retry", headers=auth(ADMIN)).status_code == 409
    j = store.claim("w")
    store.fail(j.id, "w", "x", retry_at=None)
    assert client.post(f"/api/v1/jobs/{jid}/retry", headers=auth(ADMIN)).json()["status"] == "queued"


def test_tokens_lifecycle(client: Any) -> None:
    r = client.post("/api/v1/tokens", json={"name": "ci", "role": "viewer"}, headers=auth(ADMIN))
    assert r.status_code == 201
    tok = r.json()["token"]
    assert tok.startswith("sqh_")
    assert client.get("/api/v1/whoami", headers=auth(tok)).json()["role"] == "viewer"
    listed = client.get("/api/v1/tokens", headers=auth(ADMIN)).json()["tokens"]
    assert any(t["name"] == "ci" for t in listed)
    assert all("token_hash" not in t and "token" not in t for t in listed)
    assert client.delete(f"/api/v1/tokens/{r.json()['id']}", headers=auth(ADMIN)).status_code == 200
    assert client.get("/api/v1/whoami", headers=auth(tok)).status_code == 401
    assert client.delete(f"/api/v1/tokens/{r.json()['id']}", headers=auth(ADMIN)).status_code == 404
    assert client.post("/api/v1/tokens", json={"name": "bad name", "role": "viewer"},
                       headers=auth(ADMIN)).status_code == 422
    assert client.post("/api/v1/tokens", json={"name": "x", "role": "root"}, headers=auth(ADMIN)).status_code == 422


def test_audit_endpoints(client: Any, store: Any) -> None:
    for i in range(3):
        store.append_audit("t", "x", f"s{i}", {"i": i})
    ev = client.get("/api/v1/audit", headers=auth(APPROVER)).json()
    assert [e["seq"] for e in ev["events"]] == [1, 2, 3]
    assert ev["next_since"] == 3
    assert [e["seq"] for e in client.get("/api/v1/audit?since=2", headers=auth(APPROVER)).json()["events"]] == [3]
    by_ts = client.get("/api/v1/audit?since=2000-01-01T00:00:00Z", headers=auth(APPROVER)).json()["events"]
    assert len(by_ts) == 3
    assert client.get("/api/v1/audit?since=garbage", headers=auth(APPROVER)).status_code == 422
    assert client.get("/api/v1/audit/verify", headers=auth(ADMIN)).json()["ok"] is True
    exp = client.get("/api/v1/audit/export?since=1", headers=auth(ADMIN))
    assert exp.headers["content-type"].startswith("application/x-ndjson")
    assert len(exp.text.strip().splitlines()) == 2


def test_metrics_requires_auth_by_default(client: Any) -> None:
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=auth("bad")).status_code == 401
    client.get("/healthz")
    r = client.get("/metrics", headers=auth(VIEWER))
    assert r.status_code == 200
    assert "# TYPE squadhq_http_requests_total counter" in r.text
    assert 'squadhq_http_requests_total{method="GET",route="/healthz",status="200"}' in r.text
    assert 'squadhq_jobs{status="queued"} 0' in r.text
    assert "squadhq_up 1" in r.text


def test_metrics_public(store: Any) -> None:
    from fastapi.testclient import TestClient

    from squadhq.server.app import create_app

    c = TestClient(create_app(make_settings(metrics_public=True), store=store))
    assert c.get("/metrics").status_code == 200


def test_openapi_and_docs_toggle(client: Any, store: Any) -> None:
    assert client.get("/openapi.json").status_code == 404  # off by default (SQUADHQ_API_DOCS)
    assert client.get("/docs").status_code == 404
    from fastapi.testclient import TestClient

    from squadhq.server.app import create_app

    spec = TestClient(create_app(make_settings(docs_enabled=True), store=store)).get("/openapi.json").json()
    for p in ("/api/v1/approvals/{approval_id}/decision", "/api/v1/webhooks/{source}", "/api/v1/costs",
              "/api/v1/audit", "/api/v1/briefs/latest", "/api/v1/tickets", "/api/v1/runs", "/api/v1/projects"):
        assert p in spec["paths"]
    assert "HTTPBearer" in spec["components"]["securitySchemes"]


def test_body_size_limit(store: Any) -> None:
    from fastapi.testclient import TestClient

    from squadhq.server.app import create_app

    c = TestClient(create_app(make_settings(max_body_bytes=100), store=store))
    big = {"markdown": "x" * 500}
    r = c.post("/api/v1/briefs", json=big, headers=auth(ADMIN))
    assert r.status_code == 413
    # chunked body without content-length is counted as it streams
    def gen() -> Any:
        yield b'{"markdown": "'
        yield b"y" * 300
        yield b'"}'

    r2 = c.post("/api/v1/briefs", content=gen(), headers={**auth(ADMIN), "Content-Type": "application/json"})
    assert r2.status_code == 413
    ok = c.post("/api/v1/briefs", json={"markdown": "small"}, headers=auth(ADMIN))
    assert ok.status_code == 201


def test_bad_content_length(client: Any) -> None:
    r = client.post("/api/v1/briefs", content=b"{}", headers={**auth(ADMIN), "Content-Length": "abc",
                                                                 "Content-Type": "application/json"})
    assert r.status_code == 400


def test_create_app_from_env(monkeypatch: Any, tmp_path: Any) -> None:
    from fastapi.testclient import TestClient

    from squadhq.server.app import create_app

    monkeypatch.setenv("SQUADHQ_DB_URL", f"sqlite:///{tmp_path}/hq.db")
    monkeypatch.setenv("SQUADHQ_CONFIG", str(tmp_path / "missing.yml"))
    c = TestClient(create_app())
    assert c.get("/readyz").json()["store"] == "sqlite"
    assert (tmp_path / "hq.db").exists()
